"""FIO -- Financial Intake & Orchestrator.

Flask application with all API routes for document intake, parsing,
classification, approval, and ledger posting.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, send_file

load_dotenv()

import config
from services import db
from services.parser import parse_document
from services.classifier import classify_document, add_rule, check_expense_policy
from services.ledger import post_to_actuals
from services.bt4you_sync import (
    load_departments,
    load_people,
    load_profit_center_departments,
)
from services import roles as roles_svc

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = config.MAX_FILE_SIZE_MB * 1024 * 1024

# ───────────────────────────────────────────────────────────────
# DEMO MODE — Basic Auth + audit + kill-switch + owner bypass
# Inserted via Product Builder hardening, May 2026.
# ───────────────────────────────────────────────────────────────
import hmac as _hmac
from flask import Response as _Resp

_FIO_USER = os.getenv("FIO_USER", "tester")
_FIO_PASS = os.getenv("FIO_PASS", "")
_FIO_DISABLED = os.getenv("FIO_DISABLED", "0") == "1"
_AUDIT_LOG = os.path.join(os.path.dirname(__file__), "data", "audit.jsonl")

def _audit(event: str, **fields):
    try:
        rec = {
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "event": event,
            "ip": request.headers.get("Cf-Connecting-Ip") or request.remote_addr,
            "ua": (request.headers.get("User-Agent") or "")[:160],
            "path": request.path,
            **fields,
        }
        os.makedirs(os.path.dirname(_AUDIT_LOG), exist_ok=True)
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _check_auth(u, p):
    if not _FIO_PASS:
        return False
    return _hmac.compare_digest(u or "", _FIO_USER) and _hmac.compare_digest(p or "", _FIO_PASS)

def _is_localhost_direct():
    """True if request hit loopback WITHOUT proxy headers.
    Cloudflared/Fly-proxy always inject Cf-Connecting-Ip/X-Forwarded-For,
    so this bypass never applies to internet traffic — only local SSH-tunnel."""
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1"):
        return False
    if request.headers.get("Cf-Connecting-Ip") or request.headers.get("X-Forwarded-For"):
        return False
    return True

@app.before_request
def _demo_gate():
    if request.path == "/health":
        return None
    if _FIO_DISABLED:
        _audit("blocked_disabled")
        return _Resp("FIO demo is currently disabled.", 503)
    if _is_localhost_direct():
        _audit("local_bypass", method=request.method)
        return None
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        _audit("auth_fail", user=(auth.username if auth else None))
        return _Resp(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="FIO MVP - please sign in"'},
        )
    _audit("hit", user=auth.username, method=request.method)
    return None

@app.get("/health")
def _health():
    return jsonify({"ok": True, "service": "fio-mvp", "mode": "demo"})
# ───────────────────────────────────────────────────────────────


def _seed_data_volume() -> None:
    """Copy seed config files into data/ if missing.

    On Fly.io the persistent volume mounts at /app/data and masks any files
    baked into the image at that path. A /app/seed/ directory in the image
    (populated by Dockerfile) holds the canonical ledger_schema.json and
    accounting_rules.json; we replicate them into the live volume on first
    boot so that local dev (no volume) and prod behave identically.
    """
    import shutil
    seed_dir = os.path.join(os.path.dirname(__file__), "seed")
    if not os.path.isdir(seed_dir):
        return
    target_dir = os.path.dirname(config.LEDGER_FILE)
    os.makedirs(target_dir, exist_ok=True)
    for name in ("ledger_schema.json", "accounting_rules.json", "legal_entities.json"):
        seed = os.path.join(seed_dir, name)
        target = os.path.join(target_dir, name)
        if os.path.exists(seed) and not os.path.exists(target):
            shutil.copy2(seed, target)
            logger.info("Seeded %s into data/", name)


_seed_data_volume()

# Initialise SQLite at import time so gunicorn workers have tables.
# init_db() is idempotent (CREATE TABLE IF NOT EXISTS + ALTER for migrations).
db.init_db()

# Seed user_roles.json on the volume if first boot (idempotent).
try:
    roles_svc.seed_if_missing()
except Exception as exc:
    logger.warning("Roles seed failed: %s", exc)

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "csv", "xlsx"}


def _apply_governance_suggestions(parsed: Dict[str, Any], classification: Dict[str, Any], uploaded_by: Optional[str] = None) -> Dict[str, Any]:
    """Phase 3 — enrich classification with BT4YOU-aware suggestions.

    Two signals (highest confidence wins):
      1. UPLOADER → uploader_name in BT4YOU people-map → their stream's PC
      2. VENDOR   → vendor name contains brand keyword → that brand's PC

    Also surfaces governance category hits as warnings (e.g. "this looks
    like Legal & Compliance OPEX — owner Ilona Istomina"). Non-destructive:
    only fills classification.codes[0].profit_center if it was empty.
    """
    try:
        from services import bt4you_sync as bts
    except Exception:
        return classification

    suggestions: List[Dict[str, Any]] = []
    # 1. From uploader
    if uploaded_by and uploaded_by not in ("User", ""):
        s = bts.suggest_pc_for_uploader(uploaded_by)
        if s:
            suggestions.append(s)
    # 2. From vendor
    vendor = parsed.get("vendor")
    if isinstance(vendor, dict):
        v_name = vendor.get("name") or ""
        v_addr = vendor.get("address") or ""
        s = bts.suggest_pc_for_vendor(v_name, v_addr)
        if s:
            suggestions.append(s)

    # Pick best suggestion (highest confidence)
    best = max(suggestions, key=lambda s: s.get("confidence", 0)) if suggestions else None
    if best:
        # Annotate classification — non-destructive
        classification.setdefault("governance", {})["auto_suggested_pc"] = best
        # If top code has no profit_center, backfill with the suggestion
        codes = classification.get("codes") or []
        if codes and not codes[0].get("profit_center"):
            codes[0]["profit_center"] = best["profit_center"]
            codes[0].setdefault("reasoning", "")
            codes[0]["reasoning"] += f" · Auto-PC from {best['source']}: {best['reason']}"
        # All other suggestions stay in classification.governance.alternatives
        if len(suggestions) > 1:
            classification["governance"]["alternatives"] = [s for s in suggestions if s is not best]

    # 3. Governance OPEX category match → soft warning
    try:
        gov = bts.build_governance_index()
        cats = gov.get("categories", [])
        vendor_blob = " ".join([
            (vendor.get("name") or "") if isinstance(vendor, dict) else "",
            (vendor.get("address") or "") if isinstance(vendor, dict) else "",
        ]).lower()
        items_blob = " ".join((li.get("description") or "") for li in (parsed.get("line_items") or [])).lower()
        full_blob = vendor_blob + " " + items_blob
        for c in cats:
            name_tokens = [t for t in (c.get("name") or "").lower().split() if len(t) >= 5]
            for tok in name_tokens:
                if tok in full_blob:
                    classification.setdefault("governance", {}).setdefault("opex_matches", []).append({
                        "category": c.get("name"),
                        "owner": c.get("owner"),
                        "annual_eur": c.get("annual_eur"),
                        "matched_token": tok,
                    })
                    break
    except Exception:
        pass

    return classification


def _build_doc_update(parsed: Dict[str, Any], classification: Dict[str, Any]) -> Dict[str, Any]:
    """Centralised mapper: parsed+classification → DB update fields.

    Used by upload(), parse(), and re-classify endpoints. Ensures FX/payment/
    money-breakdown fields are written consistently to DB columns (so analytics
    + Accounting queries work without re-parsing classification_json).

    Phase 2 fixes folded in here:
      • 2.1 FX  — amount = EUR equivalent, amount_orig = original, plus fx_*
      • 2.2 PC bridge — top-level profit_center never NULL when codes[0] or per_line has one
      • 2.4 payment_method — surfaced from parsed.payment_method
      • 2.5 money breakdown — subtotal/discount/credits surfaced as DB columns
    """
    money = (parsed.get("money") or {})
    vendor_data = parsed.get("vendor") or ""
    vendor = (vendor_data.get("name") or "") if isinstance(vendor_data, dict) else str(vendor_data or "")
    top_code = (classification.get("codes") or [{}])[0]

    # Phase 2.2 — harvest PC from per_line if top is empty
    pc = top_code.get("profit_center")
    if not pc:
        for line in classification.get("per_line", []):
            if line.get("profit_center"):
                pc = line["profit_center"]
                break

    # Phase 2.1 — prefer EUR-normalised amount for downstream analytics
    amount_eur = money.get("amount_eur")
    amount_orig = money.get("amount_orig")
    if amount_eur is None and money.get("total_amount") is not None:
        amount_eur = money["total_amount"]
    if amount_orig is None and money.get("total_amount") is not None:
        amount_orig = money["total_amount"]

    fields: Dict[str, Any] = {
        "status": "classified",
        "parsed_json": json.dumps(parsed, default=str),
        "classification_json": json.dumps(classification, default=str),
        "confidence": top_code.get("confidence", 0),
        "ledger_code": top_code.get("code"),
        "profit_center": pc,
        "vendor": vendor,
        # Phase 2.1 — EUR normalised primary, original secondary
        "amount": amount_eur,
        "currency": "EUR",
        "amount_orig": amount_orig,
        "currency_orig": money.get("currency_orig") or money.get("currency") or "EUR",
        "fx_rate": money.get("fx_rate"),
        "fx_date": money.get("fx_date"),
        "fx_source": money.get("fx_source"),
        # Phase 2.4 — payment method
        "payment_method": parsed.get("payment_method"),
        # Phase 2.5 — money breakdown
        "subtotal": money.get("subtotal"),
        "discount": money.get("discount"),
        "credits": money.get("credits"),
    }

    # 2026-06-09 Top-10 P2.1 — Legal Entity auto-extract from parser.
    # parser.our_entity returns one of the legal_entities.json codes (or null).
    # Bookkeeper can still override but won't have to start from "— pick payer —".
    # We accept any non-empty string; downstream UI shows it as-is if it
    # doesn't match a known entity (a warning will surface in the modal).
    our_entity = parsed.get("our_entity")
    if isinstance(our_entity, str) and our_entity.strip():
        fields["legal_entity"] = our_entity.strip()

    # Period from document date (fallback now)
    doc_date = (parsed.get("dates") or {}).get("document_date")
    fields["period"] = (doc_date[:7] if doc_date else datetime.utcnow().strftime("%Y-%m"))
    return fields


def _allowed_file(filename: str) -> bool:
    """Check if a filename has an allowed extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Static / Frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the single-page frontend."""
    return send_from_directory("static", "index.html")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def upload():
    """Upload one or more documents to the intake queue.

    Accepts multipart/form-data with field name 'files'.
    Creates a DB entry for each file and triggers parsing automatically.
    """
    if "files" not in request.files:
        return jsonify({"error": "No files provided"}), 400

    files = request.files.getlist("files")
    uploaded_by = request.form.get("uploaded_by", "User")
    results = []

    # 2026-06-09 Top-10 P3.1 — auto-route bank statements to Card Audit.
    # If the uploaded file is CSV/XLSX and its first row matches a known
    # bank-statement signature (Revolut/Mercury/Stripe/Airwallex), we
    # hand it off to card_audit.import_statement instead of running it
    # through Claude vision. Saves $$ and avoids "AI tries to parse a
    # bank statement as an invoice" mis-classifications.
    from services import card_audit as _ca

    def _looks_like_bank_statement(file_storage) -> bool:
        name = (file_storage.filename or "").lower()
        if not (name.endswith(".csv") or name.endswith(".xlsx")):
            return False
        try:
            file_storage.stream.seek(0)
            head = file_storage.stream.read(2048)
            file_storage.stream.seek(0)
        except Exception:
            return False
        try:
            text = head.decode("utf-8", errors="ignore")
        except Exception:
            return False
        # Pull the first header-line; check against signature sets
        first_line = text.split("\n", 1)[0].lower()
        if not first_line:
            return False
        headers = [h.strip().strip('"') for h in first_line.split(",")]
        spec = _ca.detect_format(headers)
        # detect_format always returns SOMETHING (generic fallback).
        # Only route if a NAMED format matched (not 'generic').
        return spec.get("id") not in (None, "generic")

    for f in files:
        if not f.filename or not _allowed_file(f.filename):
            results.append({"filename": f.filename, "error": "Invalid file type"})
            continue

        if _looks_like_bank_statement(f):
            try:
                raw = f.read()
                ca_result = _ca.import_statement(
                    raw, f.filename, imported_by=uploaded_by)
                # Reconcile any period(s) touched by this batch
                try:
                    conn = db.get_connection()
                    periods = [r[0] for r in conn.execute(
                        "SELECT DISTINCT period FROM card_transactions WHERE batch_id = ?",
                        (ca_result["batch_id"],)).fetchall()]
                    conn.close()
                    for p in periods:
                        _ca.reconcile_period(p)
                except Exception:
                    pass
                results.append({
                    "filename": f.filename,
                    "routed_to": "card_audit",
                    "batch_id": ca_result["batch_id"],
                    "rows": ca_result.get("total_rows", 0),
                    "inserted": ca_result.get("inserted", 0),
                })
                continue
            except Exception as exc:
                logger.exception("auto-route to card_audit failed for %s", f.filename)
                results.append({"filename": f.filename,
                                "error": "card-audit import failed: " + str(exc)})
                continue

        doc_id = str(uuid.uuid4())[:12]
        ext = f.filename.rsplit(".", 1)[1].lower()
        safe_name = "%s.%s" % (doc_id, ext)
        save_path = os.path.join(config.UPLOAD_FOLDER, safe_name)

        os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
        f.save(save_path)

        file_size = os.path.getsize(save_path)
        now = datetime.utcnow().isoformat()

        doc_row = {
            "id": doc_id,
            "filename": safe_name,
            "original_name": f.filename,
            "file_type": ext,
            "file_size": file_size,
            "uploaded_at": now,
            "uploaded_by": uploaded_by,
            "status": "pending",
        }
        db.insert_document(doc_row)
        db.insert_audit_log(doc_id, "uploaded", {"original_name": f.filename})

        # Auto-parse
        try:
            parsed = parse_document(save_path, ext)

            # Defensive: empty list from LLM means nothing extractable
            if isinstance(parsed, list) and not parsed:
                raise ValueError("Parser returned empty result -- document may be blank, scanned without OCR, or unreadable")

            # Handle multi-receipt response (list of parsed documents)
            if isinstance(parsed, list) and len(parsed) > 1:
                logger.info("Auto-parse multi-receipt: %d documents in %s", len(parsed), doc_id)
                for i, single_doc in enumerate(parsed):
                    classification = classify_document(single_doc)
                    classification = _apply_governance_suggestions(single_doc, classification, uploaded_by)
                    top_code = classification["codes"][0] if classification["codes"] else {}
                    vendor_data = single_doc.get("vendor") or ""
                    vendor = vendor_data.get("name") or "" if isinstance(vendor_data, dict) else str(vendor_data or "")
                    amount = None
                    if single_doc.get("money") and single_doc["money"].get("total_amount"):
                        amount = single_doc["money"]["total_amount"]

                    if i == 0:
                        # Update the original document with first receipt
                        update_fields_multi: Dict[str, Any] = {
                            "status": "classified",
                            "parsed_json": json.dumps(single_doc),
                            "classification_json": json.dumps(classification),
                            "confidence": top_code.get("confidence", 0),
                            "ledger_code": top_code.get("code"),
                            "profit_center": top_code.get("profit_center"),
                            "amount": amount,
                            "currency": single_doc.get("money", {}).get("currency", "EUR"),
                            "vendor": vendor,
                        }
                        doc_date = single_doc.get("dates", {}).get("document_date")
                        if doc_date:
                            update_fields_multi["period"] = doc_date[:7]
                        else:
                            update_fields_multi["period"] = datetime.utcnow().strftime("%Y-%m")
                        db.update_document(doc_id, update_fields_multi)
                        db.insert_audit_log(doc_id, "parsed", {"vendor": vendor, "multi_receipt": True, "receipt_index": 1})
                        db.insert_audit_log(doc_id, "classified", {"codes": classification["codes"], "auto_post": classification["auto_post"]})

                        if classification["auto_post"]:
                            doc_for_post = db.get_document(doc_id)
                            if doc_for_post:
                                db.update_document(doc_id, {"status": "approved", "approved_by": "auto", "approved_at": now})
                                doc_for_post = db.get_document(doc_id)
                                if doc_for_post:
                                    post_to_actuals(doc_for_post)
                    else:
                        # Create new document entries for additional receipts
                        new_id = str(uuid.uuid4())[:12]
                        new_filename = safe_name  # Same file — multiple receipts from one image
                        db.insert_document({
                            "id": new_id,
                            "filename": new_filename,
                            "original_name": f.filename + " (receipt %d)" % (i + 1),
                            "file_type": ext,
                            "file_size": file_size,
                            "uploaded_at": now,
                            "uploaded_by": uploaded_by,
                            "status": "pending",
                        })
                        update_fields_split: Dict[str, Any] = {
                            "status": "classified",
                            "parsed_json": json.dumps(single_doc),
                            "classification_json": json.dumps(classification),
                            "confidence": top_code.get("confidence", 0),
                            "ledger_code": top_code.get("code"),
                            "profit_center": top_code.get("profit_center"),
                            "amount": amount,
                            "currency": single_doc.get("money", {}).get("currency", "EUR"),
                            "vendor": vendor,
                        }
                        doc_date = single_doc.get("dates", {}).get("document_date")
                        if doc_date:
                            update_fields_split["period"] = doc_date[:7]
                        else:
                            update_fields_split["period"] = datetime.utcnow().strftime("%Y-%m")
                        db.update_document(new_id, update_fields_split)
                        db.insert_audit_log(new_id, "parsed", {"vendor": vendor, "multi_receipt": True, "receipt_index": i + 1, "source_doc": doc_id})
                        db.insert_audit_log(new_id, "classified", {"codes": classification["codes"], "auto_post": classification["auto_post"]})

                        if classification["auto_post"]:
                            doc_for_post = db.get_document(new_id)
                            if doc_for_post:
                                db.update_document(new_id, {"status": "approved", "approved_by": "auto", "approved_at": now})
                                doc_for_post = db.get_document(new_id)
                                if doc_for_post:
                                    post_to_actuals(doc_for_post)

                results.append({"id": doc_id, "filename": f.filename, "status": "classified", "multi_receipt": True, "receipt_count": len(parsed)})

            else:
                # Single document (or list with 1 element)
                if isinstance(parsed, list):
                    parsed = parsed[0]

                classification = classify_document(parsed)
                classification = _apply_governance_suggestions(parsed, classification, uploaded_by)
                update_fields = _build_doc_update(parsed, classification)
                db.update_document(doc_id, update_fields)
                db.insert_audit_log(doc_id, "parsed", {"vendor": update_fields.get("vendor", "")})
                db.insert_audit_log(
                    doc_id,
                    "classified",
                    {"codes": classification["codes"], "auto_post": classification["auto_post"]},
                )

                # Auto-post if high confidence
                if classification["auto_post"]:
                    doc_for_post = db.get_document(doc_id)
                    if doc_for_post:
                        db.update_document(doc_id, {"status": "approved", "approved_by": "auto", "approved_at": now})
                        doc_for_post = db.get_document(doc_id)
                        if doc_for_post:
                            post_to_actuals(doc_for_post)

                results.append({"id": doc_id, "filename": f.filename, "status": "classified"})

        except Exception as exc:  # noqa: BLE001 — batch boundary: one bad upload must not abort the rest
            logger.exception("Auto-parse failed for %s", doc_id)
            db.update_document(doc_id, {"error": str(exc)})
            results.append({"id": doc_id, "filename": f.filename, "status": "pending", "error": str(exc)})

    return jsonify({"uploaded": results}), 201


# ---------------------------------------------------------------------------
# Document CRUD
# ---------------------------------------------------------------------------

@app.route("/api/documents", methods=["GET"])
def list_documents():
    """List documents, optionally filtered by status / profit_center / ledger_code.

    When PC + ledger_code are both passed, also matches split-allocated docs
    where any allocation row carries that PC+code combination. In that case
    we attach `display_pc_amount` / `display_pc_share` so the drill-down can
    render the slice instead of the document's total.
    """
    status = request.args.get("status")
    profit_center = request.args.get("profit_center")
    ledger_code = request.args.get("ledger_code")

    if profit_center:
        docs = db.get_documents_by_profit_center(profit_center, status=status)
    else:
        docs = db.get_documents(status=status)

    # Helper: pull the allocation row for (pc, code) from a doc, if any
    def _alloc_match(doc, pc, code):
        raw = doc.get("allocations_json")
        if not raw:
            return None
        try:
            rows = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(rows, list):
            return None
        for r in rows:
            if not isinstance(r, dict):
                continue
            if r.get("profit_center") != pc:
                continue
            if code and r.get("ledger_code") and r.get("ledger_code") != code:
                continue
            return r
        return None

    # Apply (ledger_code) filter -- a doc matches if EITHER its main
    # ledger_code is the requested one OR a split-allocation row carries
    # that PC + code combo.
    if ledger_code:
        filtered = []
        for d in docs:
            if d.get("ledger_code") == ledger_code:
                filtered.append(d)
                continue
            if profit_center:
                row = _alloc_match(d, profit_center, ledger_code)
                if row:
                    # Surface the slice so the UI shows the proper amount/share
                    d_copy = dict(d)
                    d_copy["display_pc_amount"] = row.get("amount")
                    d_copy["display_pc_share"] = row.get("percentage")
                    d_copy["display_pc_note"] = row.get("note") or ""
                    d_copy["display_pc_origin"] = "split_allocation"
                    filtered.append(d_copy)
        docs = filtered
    elif profit_center:
        # No ledger filter but PC requested -- enrich split docs with their share
        for d in docs:
            if d.get("profit_center") == profit_center:
                continue
            row = _alloc_match(d, profit_center, None)
            if row:
                d["display_pc_amount"] = row.get("amount")
                d["display_pc_share"] = row.get("percentage")
                d["display_pc_note"] = row.get("note") or ""
                d["display_pc_origin"] = "split_allocation"

    for doc in docs:
        _enrich_document(doc)
    return jsonify(docs)


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def delete_doc(doc_id: str):
    """Delete a document by ID, removing from DB, disk, AND actuals."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    # If document was posted, subtract from actuals (handles split allocations too)
    if doc.get("status") == "posted":
        _subtract_from_actuals(doc)

    # Delete from DB
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()
    # Delete file from disk (only if no other docs reference it)
    file_path = os.path.join(config.UPLOAD_FOLDER, doc["filename"])
    other_refs = [d for d in db.get_documents() if d.get("filename") == doc["filename"]]
    if not other_refs and os.path.exists(file_path):
        os.remove(file_path)
    db.insert_audit_log(doc_id, "deleted", {
        "reason": "user_request",
        "was_posted": doc.get("status") == "posted",
        "vendor": doc.get("vendor"),
        "amount": doc.get("amount"),
        "ledger_code": doc.get("ledger_code"),
        "profit_center": doc.get("profit_center"),
        "period": doc.get("period"),
    })
    return jsonify({"status": "deleted"})


def _subtract_from_actuals(doc: Dict[str, Any]) -> None:
    """Remove a deleted document's amount from accounting_actuals.json.

    Handles both single-PC postings and split-allocation postings.
    """
    try:
        with open(config.ACTUALS_FILE, "r") as f:
            actuals = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    mapping = {"AG": "amitours_group", "AA": "alps2alps", "RR": "rock2rock",
               "BK": "skibookers", "SR": "skipasser", "MT": "mountly",
               "AH": "mountly", "PK": "mypeak_finance", "CF": "mypeak_finance",
               "AL": "alveda"}
    period = doc.get("period", "") or ""
    streams = actuals.get("streams", {})

    # Build the list of (stream, code, amount) entries that were posted
    entries: List[Dict[str, Any]] = []
    allocations_raw = doc.get("allocations_json")
    parsed_allocs: List[Dict[str, Any]] = []
    if allocations_raw:
        try:
            decoded = json.loads(allocations_raw) if isinstance(allocations_raw, str) else allocations_raw
            if isinstance(decoded, list):
                parsed_allocs = [r for r in decoded if isinstance(r, dict)]
        except (json.JSONDecodeError, TypeError):
            parsed_allocs = []
    total_amount = float(doc.get("amount") or 0)
    if parsed_allocs:
        for row in parsed_allocs:
            row_pc = row.get("profit_center") or "AG"
            row_code = row.get("ledger_code") or doc.get("ledger_code", "")
            row_amount = row.get("amount")
            if row_amount is None and row.get("percentage") is not None and total_amount:
                row_amount = round(total_amount * float(row["percentage"]) / 100.0, 2)
            entries.append({
                "stream": mapping.get(row_pc, row_pc.lower()),
                "code": row_code,
                "amount": float(row_amount or 0),
            })
    else:
        # Legacy single-PC posting
        pc = doc.get("profit_center", "AG")
        entries.append({
            "stream": mapping.get(pc, pc.lower()),
            "code": doc.get("ledger_code", ""),
            "amount": float(doc.get("amount") or 0),
        })

    for entry in entries:
        stream, code, amount = entry["stream"], entry["code"], entry["amount"]
        if not stream or not code or amount <= 0:
            continue
        if stream in streams and period in streams[stream] and code in streams[stream][period]:
            streams[stream][period][code] = round(streams[stream][period][code] - amount, 2)
            if streams[stream][period][code] <= 0:
                del streams[stream][period][code]
            if not streams[stream][period]:
                del streams[stream][period]
            if not streams[stream]:
                del streams[stream]
        logger.info("Subtracted EUR %.2f (%s/%s/%s) from actuals after delete", amount, stream, period, code)

    with open(config.ACTUALS_FILE, "w") as f:
        json.dump(actuals, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# BT4YOU SYNC
# ---------------------------------------------------------------------------

@app.route("/api/sync-bt4you", methods=["POST"])
def sync_to_bt4you():
    """Push FIO actuals to BT4YOU Executive Bot.

    ── Contract (Phase 5b — Dmitri's ask) ──
    Endpoint:    POST  /api/sync-bt4you
    Purpose:     forward FIO's approved+posted documents to the BT4YOU
                 Executive Bot, so cashflow aggregation has accrual-basis
                 numbers ready for the bi-monthly bookkeeping cycle.
    Request:     (empty body) — server-side picks up data/accounting_actuals.json
    Target:      POST http://127.0.0.1:8765/api/holding/cashflow/fact/generic
    Payload:     {
                   "source":    "FIO",
                   "type":      "fio_actuals_sync",
                   "data":      <accounting_actuals.json content>,
                   "synced_at": "<ISO timestamp>"
                 }
    Response:    200 {"status": "synced", "streams": [...]}
                 404 {"error": "No actuals data"}        — no posted docs yet
                 503 {"status": "sync_failed", "hint":…} — BT4YOU offline

    The payload's `data` mirrors the on-disk shape:
      {
        "streams": {"AA": {...P&L...}, "BK": {...}, …},
        "lines":   [{document_id, period, ledger_code, profit_center,
                     amount_eur, allocation_split, ...}, …]
      }
    BT4YOU reads `streams` for the dashboard cards and `lines` to drill
    into individual invoices. Allocations are pre-exploded server-side
    (one CSV-line per profit_center) so BT4YOU never has to know about
    the split logic.

    Failure mode: if BT4YOU is unreachable (port 8765 closed) we mark the
    sync as failed without losing data — actuals stay on disk and the
    next POST will retry the same payload.
    """
    import urllib.request

    try:
        with open(config.ACTUALS_FILE, "r") as f:
            actuals = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({"error": "No actuals data"}), 404

    bt4you_url = "http://127.0.0.1:8765/api/holding/cashflow/fact/generic"

    # Prepare payload for BT4YOU
    payload = json.dumps({
        "source": "FIO",
        "type": "fio_actuals_sync",
        "data": actuals,
        "synced_at": datetime.utcnow().isoformat(),
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            bt4you_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode("utf-8"))

        db.insert_audit_log("system", "bt4you_sync", {
            "streams_synced": list(actuals.get("streams", {}).keys()),
            "bt4you_response": str(result)[:200],
        })

        return jsonify({"status": "synced", "streams": list(actuals.get("streams", {}).keys())})

    except Exception as exc:
        logger.warning("BT4YOU sync failed (server may be offline): %s", exc)
        return jsonify({
            "status": "sync_failed",
            "error": str(exc),
            "hint": "BT4YOU Executive Bot must be running on port 8765",
        }), 503


@app.route("/api/documents/<doc_id>/vendor-verify", methods=["POST"])
def vendor_verify(doc_id: str):
    """Phase 5c — accountant marks vendor as manually verified.

    For non-EU vendors where VIES doesn't apply and we don't have an
    OpenCorporates lookup result, the bookkeeper can attest "yes, I verified
    this company exists" (via their own channel — phone, registry visit,
    invoice receipt confirmation, etc).

    Body:
      {"verified_by": "Rita Petukhova", "note": "Called Yerevan office, confirmed"}

    Toggle off: pass {"verified_by": null}
    """
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    who = (body.get("verified_by") or "").strip() or None
    note = (body.get("note") or "").strip() or None

    if who:
        db.update_document(doc_id, {
            "vendor_verified_by":   who,
            "vendor_verified_at":   datetime.utcnow().isoformat(),
            "vendor_verified_note": note,
        })
        db.insert_audit_log(doc_id, "vendor_verified", {"verified_by": who, "note": note})
        return jsonify({"status": "verified", "verified_by": who})
    else:
        db.update_document(doc_id, {
            "vendor_verified_by":   None,
            "vendor_verified_at":   None,
            "vendor_verified_note": None,
        })
        db.insert_audit_log(doc_id, "vendor_unverified", {"unverified_by": body.get("acting_user", "user")})
        return jsonify({"status": "unverified"})


# ════════════════════════════════════════════════════════════════
# Phase 6 — Card Audit endpoints live in routes/card_audit.py
# (registered as `card_audit_bp` at the bottom of this file).
# ════════════════════════════════════════════════════════════════


@app.route("/api/sync-bt4you/status", methods=["GET"])
def sync_bt4you_status():
    """Liveness probe for the sync pipeline (Phase 5b).

    Reports:
      - bt4you_reachable: ping http://127.0.0.1:8765/health
      - last_sync:        most recent successful sync timestamp (from audit_log)
      - actuals_present:  whether data/accounting_actuals.json exists
      - actuals_streams:  list of PC codes ready to ship
      - actuals_lines:    line count
      - pending_docs:     approved/posted docs awaiting next sync
    """
    import urllib.request

    out: Dict[str, Any] = {
        "bt4you_reachable": False,
        "bt4you_url": "http://127.0.0.1:8765/health",
        "last_sync": None,
        "actuals_present": False,
        "actuals_streams": [],
        "actuals_lines": 0,
        "pending_docs": 0,
    }

    # BT4YOU reachability
    try:
        req = urllib.request.Request(out["bt4you_url"], method="GET")
        with urllib.request.urlopen(req, timeout=4) as r:
            out["bt4you_reachable"] = r.status == 200
    except Exception:
        out["bt4you_reachable"] = False

    # Last successful sync from audit_log
    try:
        conn = db.get_connection()
        row = conn.execute(
            """SELECT performed_at, details FROM audit_log
               WHERE action = 'bt4you_sync' ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if row:
            out["last_sync"] = row["performed_at"]
        # pending docs (approved/posted without sync trace)
        cur = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE status IN ('approved', 'posted')"
        )
        out["pending_docs"] = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass

    # Actuals file
    try:
        if os.path.isfile(config.ACTUALS_FILE):
            out["actuals_present"] = True
            with open(config.ACTUALS_FILE, "r") as f:
                a = json.load(f)
            out["actuals_streams"] = list((a.get("streams") or {}).keys())
            out["actuals_lines"] = len(a.get("lines", []) or [])
    except Exception:
        pass

    return jsonify(out)


@app.route("/api/documents/<doc_id>", methods=["GET"])
def get_document(doc_id: str):
    """Get a single document by ID."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    _enrich_document(doc)
    return jsonify(doc)


@app.route("/api/documents/<doc_id>/parse", methods=["POST"])
def parse_doc(doc_id: str):
    """Trigger (re-)parsing for a document."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    file_path = os.path.join(config.UPLOAD_FOLDER, doc["filename"])
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found on disk"}), 404

    try:
        parsed = parse_document(file_path, doc["file_type"])

        # Defensive: empty list from LLM means nothing extractable
        if isinstance(parsed, list) and not parsed:
            raise ValueError("Parser returned empty result -- document may be blank, scanned without OCR, or unreadable")

        # Handle multi-document response (multiple receipts in one image)
        if isinstance(parsed, list) and len(parsed) > 1:
            logger.info("Multi-receipt detected: %d documents in %s", len(parsed), doc_id)
            created_docs = []
            for i, single_doc in enumerate(parsed):
                classification = classify_document(single_doc)
                classification = _apply_governance_suggestions(single_doc, classification, doc.get("uploaded_by"))
                top_code = classification["codes"][0] if classification["codes"] else {}
                vendor_data = single_doc.get("vendor", "")
                vendor = vendor_data.get("name", str(vendor_data)) if isinstance(vendor_data, dict) else str(vendor_data)
                amount = None
                if single_doc.get("money") and single_doc["money"].get("total_amount"):
                    amount = single_doc["money"]["total_amount"]

                if i == 0:
                    # Update the original document with first receipt
                    update_fields = {
                        "status": "classified",
                        "parsed_json": json.dumps(single_doc),
                        "classification_json": json.dumps(classification),
                        "confidence": top_code.get("confidence", 0),
                        "ledger_code": top_code.get("code"),
                        "profit_center": top_code.get("profit_center"),
                        "amount": amount,
                        "currency": single_doc.get("money", {}).get("currency", "EUR"),
                        "vendor": vendor,
                    }
                    doc_date = single_doc.get("dates", {}).get("document_date")
                    if doc_date:
                        update_fields["period"] = doc_date[:7]
                    db.update_document(doc_id, update_fields)
                    db.insert_audit_log(doc_id, "parsed", {"vendor": vendor, "multi_receipt": True, "receipt_index": 1})
                    created_docs.append({"id": doc_id, "vendor": vendor, "amount": amount})
                else:
                    # Create new document entries for additional receipts
                    import uuid
                    new_id = uuid.uuid4().hex[:12]
                    new_filename = doc["filename"]  # Same file — multiple receipts from one image
                    from datetime import datetime as _dt
                    db.insert_document({
                        "id": new_id,
                        "filename": new_filename,
                        "original_name": doc["original_name"] + f" (receipt {i+1})",
                        "file_type": doc["file_type"],
                        "file_size": doc.get("file_size", 0),
                        "uploaded_at": _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                        "uploaded_by": doc.get("uploaded_by", "User"),
                        "status": "pending",
                    })
                    update_fields = {
                        "status": "classified",
                        "parsed_json": json.dumps(single_doc),
                        "classification_json": json.dumps(classification),
                        "confidence": top_code.get("confidence", 0),
                        "ledger_code": top_code.get("code"),
                        "profit_center": top_code.get("profit_center"),
                        "amount": amount,
                        "currency": single_doc.get("money", {}).get("currency", "EUR"),
                        "vendor": vendor,
                    }
                    doc_date = single_doc.get("dates", {}).get("document_date")
                    if doc_date:
                        update_fields["period"] = doc_date[:7]
                    db.update_document(new_id, update_fields)
                    db.insert_audit_log(new_id, "parsed", {"vendor": vendor, "multi_receipt": True, "receipt_index": i + 1, "source_doc": doc_id})
                    created_docs.append({"id": new_id, "vendor": vendor, "amount": amount})

            return jsonify({"status": "classified", "multi_receipt": True, "documents": created_docs})

        # Single document (or list with 1 element)
        if isinstance(parsed, list):
            parsed = parsed[0]

        classification = classify_document(parsed)
        classification = _apply_governance_suggestions(parsed, classification, doc.get("uploaded_by"))
        top_code = classification["codes"][0] if classification["codes"] else {}
        vendor_data = parsed.get("vendor", "")
        vendor = vendor_data.get("name", str(vendor_data)) if isinstance(vendor_data, dict) else str(vendor_data)
        amount = None
        if parsed.get("money") and parsed["money"].get("total_amount"):
            amount = parsed["money"]["total_amount"]

        update_fields: Dict[str, Any] = {
            "status": "classified",
            "parsed_json": json.dumps(parsed),
            "classification_json": json.dumps(classification),
            "confidence": top_code.get("confidence", 0),
            "ledger_code": top_code.get("code"),
            "profit_center": top_code.get("profit_center"),
            "amount": amount,
            "currency": parsed.get("money", {}).get("currency", "EUR"),
            "vendor": vendor,
        }

        doc_date = parsed.get("dates", {}).get("document_date")
        if doc_date:
            update_fields["period"] = doc_date[:7]

        db.update_document(doc_id, update_fields)
        db.insert_audit_log(doc_id, "parsed", {"vendor": vendor})

        return jsonify({"status": "classified", "parsed": parsed, "classification": classification})

    except Exception as exc:
        logger.exception("Parse failed for %s", doc_id)
        db.update_document(doc_id, {"error": str(exc)})
        return jsonify({"error": str(exc)}), 500


@app.route("/api/documents/<doc_id>/save", methods=["POST"])
def save_doc(doc_id: str):
    """Save partial edits to a document WITHOUT approving or posting.

    Lets users iterate on metadata (vendor, amount, ledger code, profit center,
    department, cost reason, period) before they're ready to approve. Status
    stays as 'classified' / 'pending' / whatever it was. No actuals updated.
    """
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    # Two allow-lists:
    # (1) Metadata fields editable BEFORE approval — vendor, amount, etc.
    # (2) Workflow-stage fields editable AFTER approval too — legal_entity
    #     and payment_comment are filled in by the bookkeeper at the
    #     Awaiting-CEO / Awaiting-Payment stages, when status='approved'
    #     or 'posted'. They never change the financial truth, just routing.
    pre_approval = ("ledger_code", "profit_center", "department", "cost_reason",
                    "amount", "period", "vendor", "currency")
    post_approval_workflow = (
        "legal_entity", "payment_comment",
        # 2026-06-07 P2 — bookkeeper can set these at any stage
        "desired_payment_date", "payment_state",
    )

    if doc.get("status") in ("posted", "approved", "rejected"):
        # Only the workflow fields are still editable
        allowed = post_approval_workflow
    else:
        allowed = pre_approval + post_approval_workflow
    update_fields: Dict[str, Any] = {k: body[k] for k in allowed if k in body}

    # Phase 2.5 — per-line ledger split (when user reassigns lines to different codes)
    per_line_overrides = body.get("per_line") or []
    if per_line_overrides:
        try:
            existing = json.loads(doc.get("classification_json") or "{}")
        except Exception:
            existing = {}
        existing_per = existing.get("per_line", [])
        # Merge user overrides into existing per_line records
        idx_to_existing = {p.get("line_index"): p for p in existing_per if isinstance(p, dict)}
        for ovr in per_line_overrides:
            li = ovr.get("line_index")
            if li is None:
                continue
            base = idx_to_existing.get(li, {"line_index": li})
            if ovr.get("code"):
                base["code"] = ovr["code"]
                base["source"] = "user_override"
                base["confidence"] = 100
            if ovr.get("profit_center"):
                base["profit_center"] = ovr["profit_center"]
            idx_to_existing[li] = base
        existing["per_line"] = list(idx_to_existing.values())
        update_fields["classification_json"] = json.dumps(existing, default=str)

    if not update_fields:
        return jsonify({"status": "no_changes"})

    db.update_document(doc_id, update_fields)
    db.insert_audit_log(doc_id, "saved", {
        "fields_changed": list(update_fields.keys()),
        "per_line_overrides": len(per_line_overrides),
        "saved_by": body.get("saved_by", "user"),
    })
    return jsonify({"status": "saved", "document": db.get_document(doc_id)})


@app.route("/api/documents/<doc_id>/vat-verify", methods=["POST"])
def manually_verify_vat(doc_id: str):
    """Mark a document's VAT as manually verified after the user checked it
    against an external registry (Lursoft / Äriregister / FR INSEE / etc.).

    This persists the override into parsed_json.vendor so that VIES failure
    no longer blocks the user. Audit log records who verified and when.
    """
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    verified_by = body.get("verified_by") or "user"
    note = body.get("note", "")
    verification_url = (body.get("verification_url") or "").strip()

    # Merge override into parsed_json.vendor so /api/documents/<id>
    # surfaces it on next fetch.
    try:
        parsed = json.loads(doc.get("parsed_json") or "{}")
    except Exception:
        parsed = {}
    vendor = parsed.get("vendor") or {}
    if not isinstance(vendor, dict):
        vendor = {"name": str(vendor)}
    vendor["vat_manually_verified"] = True
    vendor["vat_verified_by"] = verified_by
    vendor["vat_verified_at"] = datetime.utcnow().isoformat()
    if verification_url:
        vendor["vat_verification_url"] = verification_url
    if note:
        vendor["vat_verification_note"] = note
    parsed["vendor"] = vendor

    db.update_document(doc_id, {"parsed_json": json.dumps(parsed)})
    db.insert_audit_log(doc_id, "vat_manually_verified", {
        "verified_by": verified_by,
        "vat_number": doc.get("vat_number"),
        "verification_url": verification_url,
        "note": note,
    })
    return jsonify({"status": "verified", "document": db.get_document(doc_id)})


@app.route("/api/documents/<doc_id>/approve", methods=["POST"])
def approve_doc(doc_id: str):
    """Approve a document with optional edits to classification."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    now = datetime.utcnow().isoformat()

    update_fields: Dict[str, Any] = {
        "status": "approved",
        "approved_by": body.get("approved_by", "user"),
        "approved_at": now,
    }

    # Allow overrides
    if "ledger_code" in body:
        update_fields["ledger_code"] = body["ledger_code"]
    if "profit_center" in body:
        update_fields["profit_center"] = body["profit_center"]
    if "period" in body:
        update_fields["period"] = body["period"]
    if "amount" in body:
        update_fields["amount"] = body["amount"]
    if "department" in body:
        update_fields["department"] = body["department"]
    if "cost_reason" in body:
        update_fields["cost_reason"] = body["cost_reason"]

    db.update_document(doc_id, update_fields)
    db.insert_audit_log(doc_id, "approved", {"overrides": body})

    # Post to actuals
    doc = db.get_document(doc_id)
    if doc:
        try:
            post_to_actuals(doc)
            return jsonify({"status": "posted", "document": db.get_document(doc_id)})
        except Exception as exc:
            logger.exception("Post failed for %s", doc_id)
            return jsonify({"error": str(exc)}), 500

    return jsonify({"status": "approved"})


# ════════════════════════════════════════════════════════════════
# Phase 3.5 — Multi-stream cost allocation (Katia case)
# ════════════════════════════════════════════════════════════════

@app.route("/api/documents/<doc_id>/allocations", methods=["POST"])
def save_allocations(doc_id: str):
    """Set multi-stream allocation for a document.

    Body:
      {
        "allocations": [
          {"profit_center": "AA", "percentage": 60, "ledger_code": "BT00", "note": "ops side"},
          {"profit_center": "BK", "percentage": 40, "ledger_code": "BT00", "note": "Skibookers benefit"}
        ],
        "saved_by": "Rita"
      }

    Validation:
      - Sum of percentages must be 100 (± 0.5 tolerance for rounding)
      - OR all entries may use 'amount' instead; sum-of-amount must equal document total (±0.01)
      - Each entry must have profit_center (ledger_code + note optional)
    """
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    allocations = body.get("allocations") or []
    if not isinstance(allocations, list):
        return jsonify({"error": "allocations must be list"}), 400

    if not allocations:
        # Clear allocations
        db.update_document(doc_id, {"allocations_json": None})
        db.insert_audit_log(doc_id, "allocations_cleared", {"by": body.get("saved_by", "user")})
        return jsonify({"status": "cleared"})

    # Validate
    total_doc = float(doc.get("amount") or 0)
    pct_total = 0.0
    amt_total = 0.0
    has_pct = has_amt = False
    cleaned: List[Dict[str, Any]] = []
    for a in allocations:
        if not isinstance(a, dict):
            return jsonify({"error": "each allocation must be a dict"}), 400
        pc = (a.get("profit_center") or "").strip()
        if not pc:
            return jsonify({"error": "each allocation needs profit_center"}), 400
        pct = a.get("percentage")
        amt = a.get("amount")
        if pct is not None:
            has_pct = True
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                return jsonify({"error": "percentage must be numeric"}), 400
            pct_total += pct
        if amt is not None:
            has_amt = True
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                return jsonify({"error": "amount must be numeric"}), 400
            amt_total += amt
        cleaned.append({
            "profit_center": pc,
            "percentage": round(pct, 4) if pct is not None else None,
            "amount":     round(amt, 2) if amt is not None else None,
            "ledger_code": a.get("ledger_code") or doc.get("ledger_code"),
            "note":        (a.get("note") or "").strip(),
        })

    if has_pct and abs(pct_total - 100.0) > 0.5:
        return jsonify({"error": f"percentages sum to {pct_total:.2f}, expected 100"}), 400
    if has_amt and total_doc and abs(amt_total - total_doc) > 0.01:
        return jsonify({"error": f"amounts sum to {amt_total:.2f}, expected {total_doc:.2f}"}), 400

    # If percentages given, compute amount per row (denormalised for P&L queries)
    if has_pct and total_doc:
        for r in cleaned:
            if r["percentage"] is not None and r["amount"] is None:
                r["amount"] = round(total_doc * r["percentage"] / 100.0, 2)

    # ---- BUG-FIX: when allocations are saved on a doc that was already posted
    # (e.g. auto-posted on upload due to high confidence), the previous single-PC
    # posting is stale -- it only updated one stream's actuals. We must subtract
    # the old posting and re-post using the new allocations so every PC's P&L
    # gets its proportional share.
    was_posted = doc.get("status") == "posted"
    doc_before = dict(doc)  # snapshot BEFORE we update allocations_json

    db.update_document(doc_id, {"allocations_json": json.dumps(cleaned, ensure_ascii=False)})
    db.insert_audit_log(doc_id, "allocations_set", {
        "splits":    len(cleaned),
        "summary":   [{"pc": c["profit_center"], "pct": c["percentage"], "amt": c["amount"]} for c in cleaned],
        "saved_by":  body.get("saved_by", "user"),
        "was_posted_repost": was_posted,
    })

    if was_posted:
        # Roll back the previous posting (uses doc_before's state: either old
        # allocations_json or the single-PC fallback) then re-post with the
        # new allocations_json that lives in the freshly-fetched doc.
        try:
            _subtract_from_actuals(doc_before)
        except Exception:
            logger.exception("Failed to subtract old posting for %s during alloc update", doc_id)
        # Re-post with the new split. post_to_actuals reads allocations_json
        # from the doc itself, so we refresh it first.
        refreshed = db.get_document(doc_id)
        if refreshed:
            try:
                # post_to_actuals also sets status=posted + posted_at. Since the
                # doc is already 'posted', this is idempotent w.r.t. status.
                post_to_actuals(refreshed)
            except Exception:
                logger.exception("Failed to re-post %s after allocation update", doc_id)
                return jsonify({
                    "status": "saved",
                    "warning": "Allocations saved but re-posting to actuals failed -- check logs",
                    "allocations": cleaned,
                }), 200

    return jsonify({"status": "saved", "allocations": cleaned, "reposted": was_posted})


# ════════════════════════════════════════════════════════════════
# Phase 4.1 — Reassign (post-approval edit with full audit trail)
# ════════════════════════════════════════════════════════════════

@app.route("/api/documents/<doc_id>/reassign", methods=["POST"])
def reassign_doc(doc_id: str):
    """Re-classify an already-approved document (bookkeeper correction).

    Unlike /save which only works on pending docs, /reassign works on any
    status. Writes a full before/after diff to the audit log for compliance.

    Body: { ledger_code, profit_center, department, cost_reason, period,
            reason: "<why we reassigned>", changed_by }
    """
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    allowed = ("ledger_code", "profit_center", "department", "cost_reason", "period")
    new_values = {k: body[k] for k in allowed if k in body}
    if not new_values:
        return jsonify({"error": "no fields to reassign"}), 400

    before = {k: doc.get(k) for k in allowed}
    diff = {k: (before.get(k), new_values[k]) for k in new_values if before.get(k) != new_values[k]}
    if not diff:
        return jsonify({"status": "no_changes"})

    db.update_document(doc_id, new_values)
    db.insert_audit_log(doc_id, "reassigned", {
        "before":      before,
        "after":       new_values,
        "diff":        diff,
        "reason":      body.get("reason", ""),
        "changed_by":  body.get("changed_by", "user"),
        "previous_status": doc.get("status"),
    })
    return jsonify({"status": "reassigned", "diff": diff, "document": db.get_document(doc_id)})


# ════════════════════════════════════════════════════════════════
# Phase 7 — Confirm-for-Payment workflow
# Posted → Holding CEO ticks → confirmed_to_pay → Bookkeeper pays → paid
# ════════════════════════════════════════════════════════════════

@app.route("/api/payment-queue", methods=["GET"])
def payment_queue():
    """Return documents in a specific payment stage.

    Query: ?stage=awaiting | approved | paid
      awaiting  -- status=posted, no confirm yet
      approved  -- status=confirmed_to_pay, no payment yet
      paid      -- status=paid

    Visible to admin / holding_ceo / bookkeeper.
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_HOLDING_CEO, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err

    stage = (request.args.get("stage") or "budget_check").strip()
    # 4-stage workflow (per spec — bookkeeper validates budget first, then CEO,
    # then bookkeeper pays):
    #
    #   posted/approved → [Bookkeeper: Budget OK] → budget_validated
    #                  → [Holding CEO: Approve to pay] → confirmed_to_pay
    #                  → [Bookkeeper: Mark paid] → paid
    if stage in ("budget_check", "awaiting"):  # 'awaiting' kept for back-compat
        target_statuses = ("approved", "posted")
    elif stage == "awaiting_ceo":
        target_statuses = ("budget_validated",)
    elif stage in ("awaiting_payment", "approved"):  # 'approved' kept for back-compat
        target_statuses = ("confirmed_to_pay",)
    elif stage == "paid":
        target_statuses = ("paid",)
    else:
        return jsonify({"error": "Invalid stage",
                        "allowed": ["budget_check", "awaiting_ceo", "awaiting_payment", "paid"]}), 400

    placeholders = ",".join("?" for _ in target_statuses)
    conn = db.get_connection()
    try:
        # Use _row_to_dict so parsed_json / classification_json are decoded
        # into real dicts -- _enrich_document expects this shape.
        rows = [db._row_to_dict(r) for r in conn.execute(
            "SELECT * FROM documents WHERE status IN (%s) ORDER BY uploaded_at DESC" % placeholders,
            target_statuses
        ).fetchall()]
        # Diagnostic count of every status (helps when 'awaiting' looks empty)
        status_counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
            ).fetchall()
        }
    finally:
        conn.close()
    for doc in rows:
        _enrich_document(doc)
    return jsonify({
        "stage": stage,
        "count": len(rows),
        "documents": rows,
        "status_counts_all": status_counts,
    })


@app.route("/api/documents/<doc_id>/budget-validate", methods=["POST"])
def budget_validate(doc_id: str):
    """Bookkeeper (or admin) confirms the cost fits the budget.

    Status transitions: approved | posted -> budget_validated.
    First step in the 4-stage payment workflow.
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err

    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    if doc.get("status") not in ("approved", "posted"):
        return jsonify({"error": "Only 'approved' or 'posted' docs can be budget-validated",
                        "current_status": doc.get("status")}), 400

    body = request.get_json(silent=True) or {}
    validated_by = body.get("validated_by") or _current_user_name() or "user"
    now = datetime.utcnow().isoformat()
    db.update_document(doc_id, {
        "status": "budget_validated",
        "budget_validated_at": now,
        "budget_validated_by": validated_by,
        "budget_validated_note": body.get("note") or None,
    })
    db.insert_audit_log(doc_id, "budget_validated", {
        "by": validated_by, "at": now, "note": body.get("note") or "",
    })
    return jsonify({"status": "budget_validated", "document": db.get_document(doc_id)})


@app.route("/api/documents/<doc_id>/confirm-payment", methods=["POST"])
def confirm_payment(doc_id: str):
    """Holding CEO (or admin) ticks 'approve to pay this week'.

    Status transitions: budget_validated -> confirmed_to_pay.
    (Legacy 'posted'/'approved' also accepted for back-compat when the
    bookkeeper step was skipped.)
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_HOLDING_CEO)
    if err:
        return err

    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    if doc.get("status") not in ("budget_validated", "approved", "posted"):
        return jsonify({"error": "Doc must be budget-validated (or at least posted/approved) before CEO can confirm",
                        "current_status": doc.get("status")}), 400

    body = request.get_json(silent=True) or {}
    confirmed_by = body.get("confirmed_by") or _current_user_name() or "user"
    now = datetime.utcnow().isoformat()
    db.update_document(doc_id, {
        "status": "confirmed_to_pay",
        "confirmed_to_pay_at": now,
        "confirmed_to_pay_by": confirmed_by,
        "confirmed_to_pay_note": body.get("note") or None,
    })
    db.insert_audit_log(doc_id, "payment_confirmed", {
        "by": confirmed_by,
        "at": now,
        "note": body.get("note") or "",
    })
    return jsonify({"status": "confirmed_to_pay", "document": db.get_document(doc_id)})


@app.route("/api/documents/<doc_id>/mark-paid", methods=["POST"])
def mark_paid(doc_id: str):
    """Bookkeeper records that payment was executed.

    Status transitions: confirmed_to_pay -> paid.
    Captures who + when + paying account + reference.

    2026-06-07 P2.5 — extended with optional payment-in-original-currency:
        body.payment_currency        — 'EUR' (default) or invoice's source currency
        body.payment_amount_orig     — REAL, amount paid in original currency
        body.payment_fx_rate         — REAL, FX used at payment moment
    EUR amount stays the accounting truth (already on the doc); the orig
    fields are surfaced in audit log + reports for the bookkeeper.
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err

    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    if doc.get("status") not in ("confirmed_to_pay",):
        return jsonify({"error": "Only 'confirmed_to_pay' docs can be marked paid",
                        "current_status": doc.get("status")}), 400

    body = request.get_json(silent=True) or {}
    paid_by = body.get("paid_by") or _current_user_name() or "user"
    now = datetime.utcnow().isoformat()

    update = {
        "status": "paid",
        "payment_state": "paid",     # P2.2 — keep state machine in sync
        "payment_executed_at": now,
        "payment_executed_by": paid_by,
        "payment_account": body.get("payment_account") or None,
        "payment_paying_entity": body.get("paying_entity") or None,
        "payment_reference": body.get("payment_reference") or None,
    }
    # P2.5 — pay-in-original-currency capture
    pay_ccy = (body.get("payment_currency") or "").strip().upper() or None
    if pay_ccy:
        update["payment_currency"] = pay_ccy
        if body.get("payment_amount_orig") is not None:
            try:
                update["payment_amount_orig"] = float(body["payment_amount_orig"])
            except (TypeError, ValueError):
                pass
        if body.get("payment_fx_rate") is not None:
            try:
                update["payment_fx_rate"] = float(body["payment_fx_rate"])
            except (TypeError, ValueError):
                pass

    db.update_document(doc_id, update)
    db.insert_audit_log(doc_id, "marked_paid", {
        "by": paid_by,
        "at": now,
        "account": body.get("payment_account"),
        "reference": body.get("payment_reference"),
        "payment_currency": update.get("payment_currency"),
        "payment_amount_orig": update.get("payment_amount_orig"),
        "payment_fx_rate": update.get("payment_fx_rate"),
    })
    return jsonify({"status": "paid", "document": db.get_document(doc_id)})


# ════════════════════════════════════════════════════════════════
# 2026-06-07 P2.1 — Already-paid-by-card flag (skip Rita workflow)
# ════════════════════════════════════════════════════════════════

@app.route("/api/documents/<doc_id>/mark-already-paid-by-card", methods=["POST"])
def mark_already_paid_by_card(doc_id: str):
    """Mark a document as already paid via corporate card.

    Skips the Awaiting CEO → Awaiting Payment stages entirely. The doc
    transitions straight to status='paid' with payment_state='paid'
    and a note explaining who paid. This is for invoices like LinkedIn,
    Fireflies, Hetzner etc. paid by corporate card before the invoice
    even reached Rita.

    Body:
      card_holder    — required, who paid (free text or name from roster)
      paid_at        — optional ISO date; defaults to "today"
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER,
                        roles_svc.ROLE_STREAM_OWNER)
    if err:
        return err

    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    if doc.get("status") in ("paid", "rejected"):
        return jsonify({"error": "Document is already %s" % doc.get("status")}), 400

    body = request.get_json(silent=True) or {}
    card_holder = (body.get("card_holder") or "").strip()
    if not card_holder:
        return jsonify({"error": "card_holder is required"}), 400

    now = datetime.utcnow().isoformat()
    paid_at = body.get("paid_at") or now
    actor = _current_user_name() or "user"

    db.update_document(doc_id, {
        "status": "paid",
        "payment_state": "paid",
        "already_paid_by_card": 1,
        "paid_card_holder": card_holder,
        "payment_executed_at": paid_at,
        "payment_executed_by": card_holder,
        "payment_reference": "card payment (no wire)",
    })
    db.insert_audit_log(doc_id, "marked_already_paid_by_card", {
        "card_holder": card_holder, "paid_at": paid_at, "by": actor,
    })
    return jsonify({"status": "paid", "document": db.get_document(doc_id)})


@app.route("/api/documents/<doc_id>/undo-already-paid-by-card", methods=["POST"])
def undo_already_paid_by_card(doc_id: str):
    """Revert the already-paid-by-card flag, send the doc back to budget_check.
    Useful if bookkeeper clicked the flag by mistake."""
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err
    doc = db.get_document(doc_id)
    if not doc or not doc.get("already_paid_by_card"):
        return jsonify({"error": "Not in already-paid state"}), 400
    db.update_document(doc_id, {
        "status": "approved",
        "payment_state": "in_progress",  # P2.3 (2026-06-09)
        "already_paid_by_card": 0,
        "paid_card_holder": None,
        "payment_executed_at": None,
        "payment_executed_by": None,
        "payment_reference": None,
    })
    db.insert_audit_log(doc_id, "undo_already_paid_by_card",
                        {"by": _current_user_name() or "user"})
    return jsonify({"status": "approved", "document": db.get_document(doc_id)})


# ════════════════════════════════════════════════════════════════
# 2026-06-07 P2.2 — desired_payment_date + payment_state transitions
# ════════════════════════════════════════════════════════════════

@app.route("/api/documents/<doc_id>/payment-state", methods=["POST"])
def set_payment_state(doc_id: str):
    """Update payment workflow state independently of the main `status`.

    Valid transitions:
        needs_to_pay -> in_progress
        in_progress  -> paid (use mark-paid for the full record)
        in_progress  -> needs_to_pay (back off)
        needs_to_pay -> on_hold
        on_hold      -> needs_to_pay

    Body:
        state             — required, one of {needs_to_pay,in_progress,paid,on_hold}
        desired_payment_date — optional ISO date (YYYY-MM-DD)
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err

    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    state = (body.get("state") or "").strip()
    # 2026-06-09 Top-10 P2.3 — dropped "needs_to_pay" from the user-facing
    # enum (tester feedback: "if it's in the system, it needs paying by
    # definition"). Mapped to in_progress on read. Backend still accepts
    # the legacy string for backward-compat but normalises it.
    if state == "needs_to_pay":
        state = "in_progress"
    VALID = {"in_progress", "paid", "on_hold"}
    if state and state not in VALID:
        return jsonify({"error": "state must be one of %s" % sorted(VALID)}), 400

    update: Dict[str, Any] = {}
    if state:
        update["payment_state"] = state
    if "desired_payment_date" in body:
        update["desired_payment_date"] = (body.get("desired_payment_date") or "").strip() or None
    if not update:
        return jsonify({"status": "no_changes"})

    db.update_document(doc_id, update)
    db.insert_audit_log(doc_id, "payment_state_changed", {
        "fields": list(update.keys()),
        "by": _current_user_name() or "user",
    })
    return jsonify({"status": "saved", "document": db.get_document(doc_id)})


# ════════════════════════════════════════════════════════════════
# Phase 4.2 + 4.3 — CSV / Google Sheets export per business stream
# ════════════════════════════════════════════════════════════════

@app.route("/api/accounting/export", methods=["GET"])
def accounting_export():
    """Export documents filtered by profit_center / period as CSV.

    Query params:
      profit_center=AA           (optional — empty = all PCs)
      period=2026-05             (optional — empty = all periods)
      include_allocations=1      (optional — split-allocated docs into per-PC rows)
      format=csv                 (default; xlsx via openpyxl planned)
    """
    import csv
    import io as _io

    pc_filter = (request.args.get("profit_center") or "").strip().upper() or None
    period_filter = (request.args.get("period") or "").strip() or None
    include_allocs = request.args.get("include_allocations", "1") == "1"

    conn = db.get_connection()
    try:
        sql = "SELECT * FROM documents WHERE status IN ('approved', 'posted', 'classified')"
        params: List[Any] = []
        if pc_filter:
            sql += " AND (profit_center = ? OR allocations_json LIKE ?)"
            params.append(pc_filter)
            params.append(f'%"profit_center": "{pc_filter}"%')
        if period_filter:
            sql += " AND period = ?"
            params.append(period_filter)
        sql += " ORDER BY period DESC, approved_at DESC, uploaded_at DESC"
        rows = [db._row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    # Flatten: docs with allocations split into multiple CSV rows.
    # _row_to_dict already decodes allocations_json → list/dict, but legacy
    # rows may carry a raw string — handle both shapes.
    csv_rows: List[Dict[str, Any]] = []
    for d in rows:
        allocs = []
        raw_alloc = d.get("allocations_json")
        if include_allocs and raw_alloc:
            if isinstance(raw_alloc, list):
                allocs = raw_alloc
            elif isinstance(raw_alloc, str):
                try:
                    allocs = json.loads(raw_alloc)
                except (json.JSONDecodeError, TypeError):
                    allocs = []
        # Build base row from doc
        base = {
            "document_id":       d.get("id"),
            "original_filename": d.get("original_name"),
            "uploaded_at":       d.get("uploaded_at"),
            "uploaded_by":       d.get("uploaded_by"),
            "vendor":            d.get("vendor"),
            "period":            d.get("period"),
            "status":            d.get("status"),
            "approved_by":       d.get("approved_by"),
            "approved_at":       d.get("approved_at"),
            "currency_orig":     d.get("currency_orig") or d.get("currency"),
            "amount_orig":       d.get("amount_orig"),
            "fx_rate":           d.get("fx_rate"),
            "fx_date":           d.get("fx_date"),
            "fx_source":         d.get("fx_source"),
            "payment_method":    d.get("payment_method"),
            "subtotal_eur":      d.get("subtotal"),
            "discount_eur":      d.get("discount"),
            "credits_eur":       d.get("credits"),
            "amount_eur":        d.get("amount"),
        }
        if allocs:
            for a in allocs:
                row = dict(base)
                row["allocation_split"]  = "yes"
                row["ledger_code"]       = a.get("ledger_code") or d.get("ledger_code")
                row["profit_center"]     = a.get("profit_center")
                row["allocation_pct"]    = a.get("percentage")
                row["allocation_amount"] = a.get("amount")
                row["allocation_note"]   = a.get("note", "")
                # If PC filter was set, only emit matching split rows
                if pc_filter and row["profit_center"] != pc_filter:
                    continue
                csv_rows.append(row)
        else:
            base["allocation_split"]  = "no"
            base["ledger_code"]       = d.get("ledger_code")
            base["profit_center"]     = d.get("profit_center")
            base["allocation_pct"]    = None
            base["allocation_amount"] = d.get("amount")
            base["allocation_note"]   = ""
            csv_rows.append(base)

    fmt = (request.args.get("format") or "csv").lower()
    fieldnames = [
        "document_id", "original_filename", "uploaded_at", "uploaded_by",
        "vendor", "period", "status", "approved_by", "approved_at",
        "ledger_code", "profit_center",
        "currency_orig", "amount_orig", "fx_rate", "fx_date", "fx_source",
        "subtotal_eur", "discount_eur", "credits_eur", "amount_eur",
        "allocation_split", "allocation_pct", "allocation_amount", "allocation_note",
        "payment_method",
    ]
    if fmt == "json":
        return jsonify({"rows": csv_rows, "count": len(csv_rows)})

    buf = _io.StringIO()

    # ── Professional CSV header — corporate metadata so files are self-describing
    # when bookkeeper hands them to an auditor / fund manager / accountant.
    # Uses CSV "comment" convention (lines start with #) plus a blank-row separator;
    # Excel / Sheets ignore #-prefixed lines as data rows.
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    pc_label = pc_filter or "ALL"
    period_label = period_filter or "ALL"
    # Look up the human-readable PC name from BT4YOU streams if available
    pc_human = pc_label
    if pc_filter:
        try:
            from services.bt4you_sync import load_business_streams
            for s in load_business_streams():
                if (s.get("profit_center") or "").upper() == pc_filter:
                    pc_human = f'{pc_filter} — {s.get("name", "")}'.strip(" —")
                    break
        except Exception:
            pass

    meta_lines = [
        f"# Amitours Holding — FIO Accounting Export",
        f"# Business stream / Profit Center: {pc_human}",
        f"# Period filter: {period_label}",
        f"# Allocations exploded: {'yes (split docs appear as multiple rows)' if include_allocs else 'no'}",
        f"# Generated at: {generated_at}",
        f"# Total rows in export: {len(csv_rows)}",
        f"# Document statuses included: approved · posted · classified",
        f"# Source system: FIO Accounting Bot (BT4YOU Business Bots family)",
        "",
    ]
    for line in meta_lines:
        buf.write(line + "\n")

    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in csv_rows:
        writer.writerow(r)
    csv_text = buf.getvalue()

    fname_parts = ["fio_accounting"]
    if pc_filter:    fname_parts.append(pc_filter)
    if period_filter:fname_parts.append(period_filter)
    fname = "_".join(fname_parts) + ".csv"

    from flask import Response as _Resp
    return _Resp(
        csv_text, mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.route("/api/accounting/export-sheets", methods=["POST"])
def accounting_export_sheets():
    """Forward export to user-provided Google Apps Script webhook.

    User configures GOOGLE_SHEETS_WEBHOOK env (a Web-App-deployed Apps Script
    that accepts JSON POST and appends rows). Body:
      { profit_center, period, sheet_name }
    """
    import urllib.request as _ureq
    import urllib.error as _uerr

    webhook = os.getenv("GOOGLE_SHEETS_WEBHOOK", "").strip()
    if not webhook:
        return jsonify({
            "error": "GOOGLE_SHEETS_WEBHOOK not configured",
            "hint": "Add a Google Apps Script web-app URL to .env to enable this.",
            "fallback": "Use /api/accounting/export?format=csv and upload to Sheets manually.",
        }), 400

    body = request.get_json(silent=True) or {}
    # Re-use the export endpoint internally to build rows
    with app.test_request_context(
        "/api/accounting/export",
        query_string={
            "profit_center": body.get("profit_center", ""),
            "period":        body.get("period", ""),
            "format":        "json",
        },
    ):
        rows_payload = accounting_export()
        try:
            data = rows_payload.get_json()
        except Exception:
            return jsonify({"error": "internal export failed"}), 500

    payload = json.dumps({
        "sheet_name":   body.get("sheet_name") or f"fio_export_{datetime.utcnow().strftime('%Y%m%d_%H%M')}",
        "rows":         data.get("rows", []),
        "profit_center": body.get("profit_center", ""),
        "period":       body.get("period", ""),
    }).encode("utf-8")
    try:
        req = _ureq.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        with _ureq.urlopen(req, timeout=15) as r:
            return jsonify({"ok": True, "rows_sent": len(data.get("rows", [])), "response_code": r.status})
    except _uerr.URLError as exc:
        return jsonify({"error": f"webhook unreachable: {exc}"}), 502
    except Exception as exc:
        logger.exception("sheets webhook failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/documents/<doc_id>/reject", methods=["POST"])
def reject_doc(doc_id: str):
    """Reject a document with a reason."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    reason = body.get("reason", "No reason given")

    db.update_document(doc_id, {
        "status": "rejected",
        "reject_reason": reason,
    })
    db.insert_audit_log(doc_id, "rejected", {"reason": reason})

    return jsonify({"status": "rejected"})


@app.route("/api/documents/<doc_id>/rule", methods=["POST"])
def add_rule_route(doc_id: str):
    """Create a new classification rule from this document."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    vendor = body.get("vendor") or doc.get("vendor")
    code = body.get("ledger_code") or doc.get("ledger_code")
    profit_center = body.get("profit_center")

    if not vendor or not code:
        return jsonify({"error": "vendor and ledger_code are required"}), 400

    add_rule(vendor, code, profit_center)
    db.insert_audit_log(doc_id, "rule_added", {"vendor": vendor, "code": code})

    return jsonify({"status": "rule_added", "vendor": vendor, "code": code})


@app.route("/api/documents/<doc_id>/feedback", methods=["POST"])
def document_feedback(doc_id: str):
    """Submit ML feedback (thumbs up/down) for a document.

    Body: {is_correct: bool, wrong_fields: [...], comment: "..."}
    """
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    body = request.get_json(silent=True) or {}
    is_correct = body.get("is_correct", True)
    wrong_fields = body.get("wrong_fields", [])
    comment = body.get("comment", "")

    db.insert_ml_feedback(
        document_id=doc_id,
        is_correct=is_correct,
        wrong_fields=wrong_fields if wrong_fields else None,
        comment=comment if comment else None,
    )
    db.insert_audit_log(doc_id, "ml_feedback", {
        "is_correct": is_correct,
        "wrong_fields": wrong_fields,
    })

    return jsonify({"status": "feedback_saved", "is_correct": is_correct})


@app.route("/api/documents/<doc_id>/file", methods=["GET"])
def serve_file(doc_id: str):
    """Serve the original uploaded file for preview with correct Content-Type."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    file_path = os.path.join(config.UPLOAD_FOLDER, doc["filename"])
    if not os.path.exists(file_path):
        # Fallback: strip split_N_ prefix for legacy split documents
        import re
        stripped = re.sub(r"^split_\d+_", "", doc["filename"])
        fallback_path = os.path.join(config.UPLOAD_FOLDER, stripped)
        if os.path.exists(fallback_path):
            file_path = fallback_path
        else:
            return jsonify({"error": "File not found"}), 404

    mimetype_map = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    ext = doc.get("file_type", "").lower()
    mimetype = mimetype_map.get(ext, "application/octet-stream")

    return send_file(file_path, mimetype=mimetype)


# ---------------------------------------------------------------------------
# Document enrichment helper
# ---------------------------------------------------------------------------

def _enrich_document(doc: Dict[str, Any]) -> None:
    """Add vat_number and policy_warnings to a document dict in-place.

    Extracts vat_number from parsed_json.vendor.vat_number and runs
    the expense policy checker.
    """
    # Defensive: payment_queue + some other callers fetch rows via dict(r)
    # which leaves parsed_json / classification_json as raw JSON strings. The
    # db._row_to_dict helper decodes them but isn't always used. Handle both
    # shapes here.
    parsed = doc.get("parsed_json") or {}
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    classification = doc.get("classification_json") or {}
    if isinstance(classification, str):
        try:
            classification = json.loads(classification)
        except (json.JSONDecodeError, TypeError):
            classification = {}
    if not isinstance(classification, dict):
        classification = {}

    # Extract VAT number
    vendor_data = parsed.get("vendor") or {}
    if isinstance(vendor_data, dict):
        doc["vat_number"] = vendor_data.get("vat_number") or None
        # VIES enrichment fields surfaced to UI
        doc["vies_verified"] = vendor_data.get("vies_verified")
        doc["vies_official_name"] = vendor_data.get("vies_official_name")
        doc["vendor_address"] = vendor_data.get("address") or vendor_data.get("vies_address")
        # Manual VAT verification override (set by POST /api/documents/<id>/vat-verify)
        doc["vat_manually_verified"] = bool(vendor_data.get("vat_manually_verified"))
        doc["vat_verified_by"] = vendor_data.get("vat_verified_by")
        doc["vat_verification_url"] = vendor_data.get("vat_verification_url")
        doc["vat_verification_note"] = vendor_data.get("vat_verification_note")
        doc["vat_verified_at"] = vendor_data.get("vat_verified_at")
    else:
        doc["vat_number"] = None
        doc["vies_verified"] = None
        doc["vies_official_name"] = None
        doc["vendor_address"] = None
        doc["vat_manually_verified"] = False
        doc["vat_verified_by"] = None
        doc["vat_verification_url"] = None
        doc["vat_verification_note"] = None
        doc["vat_verified_at"] = None

    # Surface parser failure category and warnings for UI banner
    doc["parser_failure_category"] = parsed.get("parser_failure_category")
    doc["needs_manual_input"] = bool(parsed.get("needs_manual_input"))
    doc["parser_warnings"] = parsed.get("warnings") or []

    # Surface split allocation rows so the modal + Upload table can render multi-PC tiles
    allocations_raw = doc.get("allocations_json")
    parsed_allocations: List[Dict[str, Any]] = []
    if allocations_raw:
        try:
            decoded = json.loads(allocations_raw) if isinstance(allocations_raw, str) else allocations_raw
            if isinstance(decoded, list):
                parsed_allocations = [r for r in decoded if isinstance(r, dict)]
        except (json.JSONDecodeError, TypeError):
            parsed_allocations = []
    doc["allocations"] = parsed_allocations
    doc["is_split"] = len(parsed_allocations) > 1

    # Extract payment method
    doc["payment_method"] = parsed.get("payment_method") or None

    # Terminal receipt linking
    doc["is_terminal_receipt"] = parsed.get("is_terminal_receipt", False)
    doc["linked_vendor"] = parsed.get("linked_vendor") or None

    # Expense policy check
    try:
        doc["policy_warnings"] = check_expense_policy(parsed, classification)
    except Exception:
        doc["policy_warnings"] = []


# ---------------------------------------------------------------------------
# Stats & Schema
# ---------------------------------------------------------------------------

@app.route("/api/stats", methods=["GET"])
def stats():
    """Return dashboard stats (counts by status)."""
    return jsonify(db.get_stats())


@app.route("/api/ledger-schema", methods=["GET"])
def ledger_schema():
    """Return the chart of accounts."""
    try:
        with open(config.LEDGER_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({"error": "Schema not found"}), 404


@app.route("/api/actuals", methods=["GET"])
def actuals():
    """Return the current accounting actuals, optionally filtered by period."""
    try:
        with open(config.ACTUALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"streams": {}}

    period = request.args.get("period")
    if period:
        filtered: Dict[str, Any] = {"streams": {}}
        for stream_name, periods in data.get("streams", {}).items():
            if period in periods:
                filtered["streams"][stream_name] = {period: periods[period]}
        return jsonify(filtered)

    return jsonify(data)


@app.route("/api/actuals/summary", methods=["GET"])
def actuals_summary():
    """Return summary totals by stream for a given period."""
    try:
        with open(config.ACTUALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"streams": {}}

    period = request.args.get("period")
    result: Dict[str, Any] = {}

    for stream_name, periods in data.get("streams", {}).items():
        if period:
            period_data = periods.get(period, {})
        else:
            # Merge all periods
            period_data = {}
            for p_data in periods.values():
                for code, amt in p_data.items():
                    period_data[code] = period_data.get(code, 0.0) + amt

        total = sum(period_data.values())
        result[stream_name] = {
            "total": round(total, 2),
            "codes": period_data,
        }

    return jsonify(result)


_PC_TO_STREAM_NAME = {
    "AG": "amitours_group", "AA": "alps2alps", "RR": "rock2rock",
    "BK": "skibookers", "SR": "skipasser", "MT": "mountly",
    "AH": "mountly", "PK": "mypeak_finance", "CF": "mypeak_finance",
    "AL": "alveda",
}


@app.route("/api/analytics/spending", methods=["GET"])
def analytics_spending():
    """Cost & Spending Analytics endpoint.

    Query params:
      - profit_center: filter all metrics to a single stream (e.g. AA, BK).
      - date_from, date_to: ISO YYYY-MM-DD or YYYY-MM. (P3.1, 2026-06-07)
        If only date_from set: open-ended forward. If only date_to: open back.

    Returns:
      - by_period, by_code, by_vendor, projection
      - duplicate_services (overlapping vendors)
      - suspicious: outliers / mis-classification / VAT issues / fuzzy duplicates
      - department_load
    """
    try:
        with open(config.ACTUALS_FILE, "r", encoding="utf-8") as f:
            actuals = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        actuals = {"streams": {}}

    pc_filter = (request.args.get("profit_center") or "").strip().upper()
    stream_filter = _PC_TO_STREAM_NAME.get(pc_filter)

    # 2026-06-07 P3.1 — date range filter. Inputs accept YYYY-MM-DD or YYYY-MM
    # (a period). Normalise to YYYY-MM for the actuals/by_period dict, and to
    # YYYY-MM-DD for doc-level filtering against uploaded_at / approved_at.
    date_from_raw = (request.args.get("date_from") or "").strip()
    date_to_raw   = (request.args.get("date_to") or "").strip()

    def _period_of(s: str) -> str:
        # YYYY-MM-DD → YYYY-MM ; YYYY-MM stays
        return s[:7] if s and len(s) >= 7 else s

    period_from = _period_of(date_from_raw) or None
    period_to   = _period_of(date_to_raw) or None

    def _period_in_range(p: str) -> bool:
        if period_from and p < period_from: return False
        if period_to   and p > period_to:   return False
        return True

    # Filter actuals by stream if requested
    if stream_filter and stream_filter in actuals.get("streams", {}):
        actuals = {"streams": {stream_filter: actuals["streams"][stream_filter]}}
    elif pc_filter and not stream_filter:
        actuals = {"streams": {}}  # Unknown PC -- empty

    # Apply date range to actuals (drop periods outside the window)
    if period_from or period_to:
        actuals = {
            "streams": {
                stream: {p: codes for p, codes in periods.items() if _period_in_range(p)}
                for stream, periods in actuals.get("streams", {}).items()
            }
        }

    docs = db.get_documents()
    if pc_filter:
        docs = [d for d in docs if (d.get("profit_center") or "").upper() == pc_filter]
    if period_from or period_to:
        docs = [d for d in docs if _period_in_range(d.get("period") or "")]

    # 1. By period (across streams)
    by_period: Dict[str, float] = {}
    for stream, periods in actuals.get("streams", {}).items():
        for period, codes in periods.items():
            for amt in codes.values():
                by_period[period] = round(by_period.get(period, 0.0) + float(amt), 2)

    # 2. By ledger code
    by_code: Dict[str, float] = {}
    for stream, periods in actuals.get("streams", {}).items():
        for period, codes in periods.items():
            for code, amt in codes.items():
                by_code[code] = round(by_code.get(code, 0.0) + float(amt), 2)
    top_codes = sorted(by_code.items(), key=lambda x: -x[1])[:20]

    # 3. By vendor (from posted documents)
    by_vendor: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        if d.get("status") != "posted":
            continue
        vendor = (d.get("vendor") or "").strip()
        if not vendor or vendor.lower() == "mock vendor ltd":
            continue
        amount = float(d.get("amount") or 0)
        code = d.get("ledger_code") or "?"
        v = by_vendor.setdefault(vendor, {"vendor": vendor, "total": 0.0, "codes": {}, "doc_count": 0})
        v["total"] = round(v["total"] + amount, 2)
        v["doc_count"] += 1
        v["codes"][code] = round(v["codes"].get(code, 0.0) + amount, 2)
    top_vendors = sorted(by_vendor.values(), key=lambda x: -x["total"])[:30]

    # 4. Projection: simple linear trend on last 3 periods
    sorted_periods = sorted(by_period.keys())
    projection: Dict[str, Any] = {"next_periods": [], "trend": "flat"}
    if len(sorted_periods) >= 2:
        recent = sorted_periods[-3:] if len(sorted_periods) >= 3 else sorted_periods
        recent_vals = [by_period[p] for p in recent]
        avg = sum(recent_vals) / len(recent_vals)
        # Simple slope
        if len(recent_vals) >= 2:
            slope = (recent_vals[-1] - recent_vals[0]) / max(1, len(recent_vals) - 1)
        else:
            slope = 0.0
        last = recent_vals[-1]
        next3: List[Dict[str, Any]] = []
        for i in range(1, 4):
            year, month = recent[-1].split("-")
            month_int = int(month) + i
            year_int = int(year) + (month_int - 1) // 12
            month_int = ((month_int - 1) % 12) + 1
            next_period = "%04d-%02d" % (year_int, month_int)
            projected = max(0.0, last + slope * i)
            next3.append({"period": next_period, "projected": round(projected, 2)})
        projection = {
            "next_periods": next3,
            "trend": "rising" if slope > avg * 0.05 else "falling" if slope < -avg * 0.05 else "flat",
            "avg_recent": round(avg, 2),
            "slope_per_month": round(slope, 2),
        }

    # 5. Duplicate / overlapping services -- vendors sharing the same code
    code_to_vendors: Dict[str, List[Dict[str, Any]]] = {}
    for v in by_vendor.values():
        for code in v["codes"].keys():
            code_to_vendors.setdefault(code, []).append({
                "vendor": v["vendor"],
                "amount_in_code": v["codes"][code],
                "total": v["total"],
            })
    duplicates = []
    for code, vendors in code_to_vendors.items():
        if len(vendors) >= 2:
            vendors_sorted = sorted(vendors, key=lambda x: -x["amount_in_code"])
            cheapest = min(vendors_sorted, key=lambda x: x["amount_in_code"])
            most_expensive = max(vendors_sorted, key=lambda x: x["amount_in_code"])
            potential_savings = round(most_expensive["amount_in_code"] - cheapest["amount_in_code"], 2)
            duplicates.append({
                "ledger_code": code,
                "vendor_count": len(vendors),
                "vendors": vendors_sorted,
                "potential_savings": potential_savings,
                "suggestion": (
                    "Multiple vendors charging under code %s. Consolidating to the cheapest "
                    "(%s @ EUR %.2f) instead of the most expensive (%s @ EUR %.2f) "
                    "could save up to EUR %.2f."
                ) % (
                    code,
                    cheapest["vendor"], cheapest["amount_in_code"],
                    most_expensive["vendor"], most_expensive["amount_in_code"],
                    potential_savings,
                ),
            })
    duplicates.sort(key=lambda x: -x["potential_savings"])

    # 6. Department load (cost concentration per department)
    dept_load: Dict[str, float] = {}
    for d in docs:
        if d.get("status") != "posted":
            continue
        dept = d.get("department")
        if not dept:
            continue
        amount = float(d.get("amount") or 0)
        dept_load[dept] = round(dept_load.get(dept, 0.0) + amount, 2)

    suspicious = _detect_suspicious(docs, by_vendor)

    return jsonify({
        "profit_center_filter": pc_filter or None,
        "by_period": [{"period": p, "amount": by_period[p]} for p in sorted_periods],
        "by_code": [{"code": c, "amount": a} for c, a in top_codes],
        "by_vendor": top_vendors,
        "projection": projection,
        "duplicate_services": duplicates[:10],
        "suspicious": suspicious,
        "department_load": [{"department": k, "amount": v} for k, v in sorted(dept_load.items(), key=lambda x: -x[1])],
        "total_lifetime_spend": round(sum(by_period.values()), 2),
        "documents_posted": sum(1 for d in docs if d.get("status") == "posted"),
        "operational_cashflow": _compute_operational_cashflow(docs),
    })


@app.route("/api/analytics/ai-report", methods=["POST"])
def analytics_ai_report():
    """Generate a written commentary on the current spending snapshot via Claude.

    Body: { focus: "general" | "savings" | "anomalies" | "stream:AA",
            profit_center: "AA" (optional) }
    Returns: { report: "...markdown text...", model: "...", saved_to: "..." }

    Falls back gracefully if ANTHROPIC_API_KEY is missing or credits are out.
    """
    if not config.ANTHROPIC_API_KEY:
        return jsonify({
            "status": "no_api_key",
            "report": ("**AI report unavailable** — set `ANTHROPIC_API_KEY` "
                       "on Fly to enable Claude-generated commentary."),
        }), 200

    body = request.get_json(silent=True) or {}
    focus = (body.get("focus") or "general").strip()[:30]
    pc_filter = (body.get("profit_center") or "").strip().upper() or None
    # 2026-06-07 P3.2 — user-supplied prompt overrides the default focus steer.
    # Trimmed to 2000 chars to keep the LLM call bounded.
    custom_prompt = (body.get("custom_prompt") or "").strip()[:2000]

    # Reuse the same data the analytics_spending endpoint computes so the
    # commentary matches what the user sees on screen.
    try:
        with open(config.ACTUALS_FILE, "r", encoding="utf-8") as f:
            actuals = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        actuals = {"streams": {}}
    if pc_filter:
        stream_filter = _PC_TO_STREAM_NAME.get(pc_filter)
        if stream_filter and stream_filter in actuals.get("streams", {}):
            actuals = {"streams": {stream_filter: actuals["streams"][stream_filter]}}
        else:
            actuals = {"streams": {}}

    docs = db.get_documents()
    if pc_filter:
        docs = [d for d in docs if (d.get("profit_center") or "").upper() == pc_filter]

    # Compose a compact JSON snapshot for the prompt (don't send raw docs)
    snapshot: Dict[str, Any] = {
        "scope": pc_filter or "all_streams",
        "focus": focus,
        "actuals": actuals,
        "documents_posted": sum(1 for d in docs if d.get("status") == "posted"),
        "documents_pending_approval": sum(
            1 for d in docs if d.get("status") in ("classified", "approved")
        ),
        "documents_paid": sum(1 for d in docs if d.get("status") == "paid"),
        "cashflow": _compute_operational_cashflow(docs),
    }

    # Prompt: business-controller persona, specific, no marketing fluff
    focus_steer = {
        "savings": "Identify the top 3 specific cost-cutting opportunities. Name vendors and amounts. Suggest replacements where reasonable.",
        "anomalies": "Find anomalies: vendors charging above category averages, unusual single-doc spikes, duplicate services, missing VAT.",
        "general": "Give an executive summary: where money flows, which streams burn the most, what trend the data shows.",
    }
    if custom_prompt:
        # P3.2 — user has steered the analysis themselves; keep the persona
        # framing but substitute their instructions for the focus block.
        focus_text = custom_prompt
    elif focus.startswith("stream:"):
        focus_text = "Focus only on stream %s. Compare its spend mix vs typical mix for similar holdings." % focus.split(":", 1)[1]
    else:
        focus_text = focus_steer.get(focus, focus_steer["general"])

    prompt = (
        "You are the business controller for Amitours Holding, advising the CFO. "
        "Below is the current FIO accounting snapshot. Write a concise commentary "
        "in markdown — 4 to 8 short paragraphs, with concrete numbers and named "
        "vendors. Use headings (##). Skip fluff and disclaimers.\n\n"
        "Specific focus: " + focus_text + "\n\n"
        "Data:\n```json\n" + json.dumps(snapshot, ensure_ascii=False, indent=2)[:18000] + "\n```\n\n"
        "Write the commentary now."
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text if resp.content else ""
    except Exception as exc:
        logger.exception("Analytics AI report failed")
        return jsonify({
            "status": "llm_error",
            "report": "**Could not generate report.** Error: " + str(exc)[:300],
        }), 200

    # Persist for later reference
    reports_dir = os.path.join(os.path.dirname(config.DB_PATH), "ai_reports")
    os.makedirs(reports_dir, exist_ok=True)
    fname = "report_%s_%s_%s.md" % (
        datetime.utcnow().strftime("%Y%m%dT%H%M%S"),
        pc_filter or "all",
        focus,
    )
    saved = os.path.join(reports_dir, fname)
    try:
        with open(saved, "w", encoding="utf-8") as f:
            f.write("# FIO AI Report\n\n")
            f.write("- generated_at: " + datetime.utcnow().isoformat() + "\n")
            f.write("- focus: " + focus + "\n")
            f.write("- profit_center: " + (pc_filter or "all") + "\n\n---\n\n")
            f.write(text)
    except OSError as exc:
        logger.warning("Could not save AI report: %s", exc)

    db.insert_audit_log("system", "ai_report_generated", {
        "focus": focus,
        "profit_center": pc_filter,
        "by": _current_user_name() or "user",
        "saved_to": saved,
    })
    return jsonify({
        "status": "ok",
        "report": text,
        "model": "claude-sonnet-4-20250514",
        "focus": focus,
        "profit_center": pc_filter,
        "generated_at": datetime.utcnow().isoformat(),
    })


# ════════════════════════════════════════════════════════════════
# 2026-06-07 P3.3 — Spend-trend monthly drill-down
# ════════════════════════════════════════════════════════════════

@app.route("/api/analytics/month-breakdown", methods=["GET"])
def analytics_month_breakdown():
    """Return stream × ledger breakdown for one month.

    Powers the Spend Trend drill-down: user clicks a monthly bar →
    modal shows "where did this period's spend go" by stream and by
    ledger group, with click-through to the source documents.

    Query: period=YYYY-MM (required), profit_center=AA (optional filter)
    """
    period = (request.args.get("period") or "").strip()
    if not period or len(period) != 7:
        return jsonify({"error": "period=YYYY-MM is required"}), 400
    pc_filter = (request.args.get("profit_center") or "").strip().upper() or None

    # Source of truth = documents (the actuals JSON aggregates by code only,
    # losing the stream × code × vendor cross-section we need for drill-down).
    docs = db.get_documents()
    docs = [d for d in docs if d.get("period") == period
            and d.get("status") in ("posted", "paid", "confirmed_to_pay",
                                    "budget_validated", "approved")]
    if pc_filter:
        docs = [d for d in docs if (d.get("profit_center") or "").upper() == pc_filter]

    # Load ledger schema for grouping by statement_group (HR, SUB, CON, etc.)
    schema = {"codes": []}
    try:
        with open(config.LEDGER_FILE, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    code_to_group = {}
    for c in schema.get("codes", []):
        code_to_group[c["code"]] = c.get("group", c.get("statement", "Other"))

    by_stream: Dict[str, Dict[str, Any]] = {}
    by_group:  Dict[str, Dict[str, Any]] = {}
    total = 0.0

    for d in docs:
        pc = (d.get("profit_center") or "—").upper()
        amount = float(d.get("amount") or 0)
        code = d.get("ledger_code") or "?"
        group = code_to_group.get(code, "Other")
        total += amount
        s = by_stream.setdefault(pc, {"profit_center": pc, "amount": 0.0,
                                     "doc_count": 0, "doc_ids": []})
        s["amount"] = round(s["amount"] + amount, 2)
        s["doc_count"] += 1
        s["doc_ids"].append(d["id"])
        g = by_group.setdefault(group, {"group": group, "amount": 0.0,
                                        "doc_count": 0, "doc_ids": []})
        g["amount"] = round(g["amount"] + amount, 2)
        g["doc_count"] += 1
        g["doc_ids"].append(d["id"])

    return jsonify({
        "period": period,
        "profit_center_filter": pc_filter,
        "total": round(total, 2),
        "document_count": len(docs),
        "by_stream": sorted(by_stream.values(), key=lambda x: -x["amount"]),
        "by_ledger_group": sorted(by_group.values(), key=lambda x: -x["amount"]),
    })


def _compute_operational_cashflow(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Outflow snapshot driven by the Confirm-for-Payment workflow.

    Returns:
      pipeline:    {posted: € awaiting confirm, confirmed: € awaiting payment,
                    paid: € executed (lifetime), upcoming_this_week: €}
      by_week:     last 6 paid weeks (week_start, amount)
      by_account:  paid spend grouped by payment_account
    """
    from collections import defaultdict
    pipeline = {
        "posted_awaiting_confirm": 0.0,
        "confirmed_awaiting_payment": 0.0,
        "paid_lifetime": 0.0,
        "upcoming_this_week_confirmed": 0.0,
    }
    by_week: Dict[str, float] = defaultdict(float)
    by_account: Dict[str, float] = defaultdict(float)
    by_payee: Dict[str, float] = defaultdict(float)

    today = datetime.utcnow().date()
    # Monday of current week (UTC)
    monday = today.fromordinal(today.toordinal() - today.weekday())
    sunday = today.fromordinal(monday.toordinal() + 6)

    for d in docs:
        st = d.get("status")
        amt = float(d.get("amount") or 0)
        if st == "posted":
            pipeline["posted_awaiting_confirm"] += amt
        elif st == "confirmed_to_pay":
            pipeline["confirmed_awaiting_payment"] += amt
            # If confirmed during this week, count as upcoming outflow
            cat = d.get("confirmed_to_pay_at") or ""
            try:
                cdate = datetime.fromisoformat(cat[:10]).date() if cat else None
            except (ValueError, TypeError):
                cdate = None
            if cdate and monday <= cdate <= sunday:
                pipeline["upcoming_this_week_confirmed"] += amt
        elif st == "paid":
            pipeline["paid_lifetime"] += amt
            # Group by ISO week of payment
            pat = d.get("payment_executed_at") or ""
            try:
                pdate = datetime.fromisoformat(pat[:10]).date() if pat else None
            except (ValueError, TypeError):
                pdate = None
            if pdate:
                week_monday = pdate.fromordinal(pdate.toordinal() - pdate.weekday())
                by_week[week_monday.isoformat()] += amt
            acct = (d.get("payment_account") or "").strip() or "Unknown"
            by_account[acct] += amt
            payee = (d.get("vendor") or "").strip() or "Unknown"
            by_payee[payee] += amt

    # Sort by_week descending and keep last 8 weeks
    week_rows = sorted(by_week.items(), key=lambda x: x[0], reverse=True)[:8]
    account_rows = sorted(by_account.items(), key=lambda x: -x[1])[:10]
    payee_rows = sorted(by_payee.items(), key=lambda x: -x[1])[:10]

    return {
        "pipeline": {k: round(v, 2) for k, v in pipeline.items()},
        "by_week": [{"week_start": w, "amount": round(a, 2)} for w, a in week_rows],
        "by_paying_account": [{"account": k, "amount": round(v, 2)} for k, v in account_rows],
        "top_paid_payees": [{"vendor": k, "amount": round(v, 2)} for k, v in payee_rows],
    }


def _detect_suspicious(docs: List[Dict[str, Any]], by_vendor: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rule-based detector for suspicious costs and vendors. AI-style heuristics.

    Checks performed:
      A. Amount outliers (> 3 sigma above mean for the same ledger code)
      B. Vendors classified under multiple ledger codes (potential mis-tag)
      C. Posted documents without any VAT number (compliance risk)
      D. Foreign vendor warning carried from parser
      E. Fuzzy duplicate vendor names (Levenshtein <= 2 or substring)
      F. Round-number amounts on receipts > 500 EUR (manual entry / fraud risk)
    """
    posted = [d for d in docs if d.get("status") == "posted" and d.get("amount")]
    findings: List[Dict[str, Any]] = []

    # A. Outliers per ledger code
    by_code_amounts: Dict[str, List[Dict[str, Any]]] = {}
    for d in posted:
        code = d.get("ledger_code") or "?"
        by_code_amounts.setdefault(code, []).append(d)
    for code, items in by_code_amounts.items():
        if len(items) < 4:
            continue
        amounts = [float(x.get("amount") or 0) for x in items]
        mean = sum(amounts) / len(amounts)
        if mean <= 0:
            continue
        variance = sum((a - mean) ** 2 for a in amounts) / len(amounts)
        stdev = variance ** 0.5
        threshold = mean + 3 * stdev
        for item in items:
            amt = float(item.get("amount") or 0)
            if amt > threshold and amt > mean * 2:
                findings.append({
                    "severity": "high",
                    "kind": "amount_outlier",
                    "title": "Unusually large %s charge" % code,
                    "detail": "EUR %.2f from %s -- avg under %s is EUR %.2f (3-sigma cutoff %.2f)" % (
                        amt, item.get("vendor") or "Unknown", code, mean, threshold,
                    ),
                    "vendor": item.get("vendor"),
                    "amount": amt,
                    "doc_id": item.get("id"),
                    "ledger_code": code,
                })

    # B. Same vendor under multiple ledger codes
    for vname, v in by_vendor.items():
        codes_used = list((v.get("codes") or {}).keys())
        if len(codes_used) >= 3:
            findings.append({
                "severity": "medium",
                "kind": "vendor_multi_code",
                "title": "%s charged under %d different codes" % (vname[:60], len(codes_used)),
                "detail": "Codes: " + ", ".join(codes_used) + ". Likely mis-classification or vendor sells multiple service types.",
                "vendor": vname,
                "amount": v.get("total"),
                "ledger_codes": codes_used,
            })

    # C. Posted docs without any VAT
    no_vat = []
    for d in posted:
        parsed = d.get("parsed_json") or {}
        vendor_dict = parsed.get("vendor")
        vat = None
        if isinstance(vendor_dict, dict):
            vat = vendor_dict.get("vat_number")
        if not vat and not d.get("vat_number"):
            no_vat.append(d)
    if no_vat:
        total_no_vat = sum(float(d.get("amount") or 0) for d in no_vat)
        findings.append({
            "severity": "medium",
            "kind": "no_vat_compliance",
            "title": "%d posted documents without VAT number" % len(no_vat),
            "detail": "EUR %.2f in posted spend has no VAT/Reg.Nr captured. May fail audit." % total_no_vat,
            "amount": round(total_no_vat, 2),
            "count": len(no_vat),
            "doc_ids": [d.get("id") for d in no_vat[:10]],
        })

    # D. Foreign vendor flag
    foreign_count = 0
    foreign_total = 0.0
    for d in posted:
        parsed = d.get("parsed_json") or {}
        warns = (parsed.get("warnings") or []) if isinstance(parsed, dict) else []
        vendor_dict = parsed.get("vendor") if isinstance(parsed, dict) else None
        if isinstance(vendor_dict, dict):
            warns = warns + (vendor_dict.get("warnings") or [])
            country = vendor_dict.get("vies_country")
            if country and country not in ("LV", "EE", "LT") and vendor_dict.get("vies_verified"):
                foreign_count += 1
                foreign_total += float(d.get("amount") or 0)
        if "foreign_vendor" in warns:
            foreign_count += 1
            foreign_total += float(d.get("amount") or 0)
    if foreign_count:
        findings.append({
            "severity": "low",
            "kind": "foreign_vendors",
            "title": "%d posted documents from foreign (non-Baltic) vendors" % foreign_count,
            "detail": "EUR %.2f. Reverse-charge VAT rules may apply." % foreign_total,
            "amount": round(foreign_total, 2),
            "count": foreign_count,
        })

    # E. Fuzzy duplicate vendor names
    vendor_names = list(by_vendor.keys())
    seen_pairs = set()
    for i, a in enumerate(vendor_names):
        a_norm = re.sub(r"[^a-z0-9]", "", a.lower())
        if len(a_norm) < 4:
            continue
        for b in vendor_names[i + 1:]:
            b_norm = re.sub(r"[^a-z0-9]", "", b.lower())
            if len(b_norm) < 4:
                continue
            pair = tuple(sorted([a, b]))
            if pair in seen_pairs:
                continue
            similar = (
                a_norm == b_norm
                or a_norm in b_norm
                or b_norm in a_norm
                or _levenshtein(a_norm, b_norm) <= 2
            )
            if similar:
                seen_pairs.add(pair)
                a_total = by_vendor[a]["total"]
                b_total = by_vendor[b]["total"]
                findings.append({
                    "severity": "medium",
                    "kind": "fuzzy_duplicate_vendor",
                    "title": "Likely same vendor: '%s' ≈ '%s'" % (a, b),
                    "detail": "Names differ by spelling/punctuation. Combined spend EUR %.2f. Consolidate to one canonical name." % (a_total + b_total),
                    "vendors": [a, b],
                    "amount": round(a_total + b_total, 2),
                })

    # F. Round-number large receipts
    for d in posted:
        amt = float(d.get("amount") or 0)
        if amt >= 500 and amt == round(amt) and amt % 50 == 0:
            findings.append({
                "severity": "low",
                "kind": "round_amount",
                "title": "Round-number receipt EUR %.0f" % amt,
                "detail": "Round amounts on a posted receipt can indicate manual entry or a placeholder. Vendor: %s" % (d.get("vendor") or "Unknown"),
                "vendor": d.get("vendor"),
                "amount": amt,
                "doc_id": d.get("id"),
            })

    # Sort: high severity first, then by amount
    sev_order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda x: (sev_order.get(x.get("severity"), 9), -float(x.get("amount") or 0)))
    return findings[:25]


def _levenshtein(a: str, b: str) -> int:
    """Tiny Levenshtein distance for fuzzy vendor name matching."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 4:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


@app.route("/api/departments", methods=["GET"])
def departments():
    """Return department spec (sourced from BT4YOU brand.json).

    Optional query: ?profit_center=AA returns only departments relevant to AA.
    """
    all_depts = load_departments()
    pc = request.args.get("profit_center")
    if pc:
        pc_map = load_profit_center_departments()
        allowed = set(pc_map.get(pc, []))
        if allowed:
            all_depts = [d for d in all_depts if d["id"] in allowed]
    return jsonify({
        "departments": all_depts,
        "pc_to_departments": load_profit_center_departments(),
    })


@app.route("/api/people", methods=["GET"])
def people():
    """Return BT4YOU people roster (Asana-synced).

    Optional query: ?profit_center=AA filters by profit center.
    Used by the UI for uploaded_by autocomplete.
    """
    roster = load_people()
    pc = request.args.get("profit_center")
    if pc:
        roster = [p for p in roster if p["profit_center"] == pc]
    return jsonify({"people": roster, "count": len(roster)})


@app.route("/api/people/refresh-from-asana", methods=["POST"])
def refresh_from_asana():
    """Pull live user list directly from Asana, replacing the BT4YOU snapshot.

    Bypasses BT4YOU's holding_config.json (which packs multiple humans into
    one row, e.g. 'Rita Petukhova, Olga Guk, Dmitriy'). Admin only.

    Requires `ASANA_PAT` (Personal Access Token) set as a Fly secret.
    Gracefully reports missing-token state for the UI.
    """
    err = _require_role(roles_svc.ROLE_ADMIN)
    if err:
        return err

    token = os.environ.get("ASANA_PAT", "").strip()
    if not token:
        return jsonify({
            "status": "token_missing",
            "message": ("ASANA_PAT is not configured. Generate a Personal "
                        "Access Token at https://app.asana.com/0/my-apps "
                        "and set it via: fly secrets set ASANA_PAT=... "
                        "--app fio-amitours"),
        }), 503

    try:
        from services.asana_sync import fetch_users_from_asana
    except ImportError:
        return jsonify({"status": "error",
                        "message": "asana_sync service not deployed"}), 500

    try:
        result = fetch_users_from_asana(token=token,
                                        workspace_id=os.environ.get("ASANA_WORKSPACE_ID"))
    except Exception as exc:
        logger.exception("Asana refresh failed")
        return jsonify({"status": "error", "message": str(exc)}), 502

    db.insert_audit_log("system", "asana_refresh", {
        "pulled": result.get("count", 0),
        "by": _current_user_name() or "admin",
    })
    return jsonify({"status": "ok", **result})


# ────────────────────────────────────────────────────────────────────
# Phase 7 — Identity + role-based access
# ────────────────────────────────────────────────────────────────────

def _current_user_name() -> Optional[str]:
    """Pull the signed-in user name from the X-FIO-User header.

    Frontend sets this from localStorage (fio_signed_in_as) on every fetch.
    Returns None when nothing set -- treated as anonymous / viewer.
    """
    name = (request.headers.get("X-FIO-User") or "").strip()
    return name or None


def _require_role(*allowed_roles: str):
    """Decorator-style guard for admin-only endpoints.

    Usage:
        err = _require_role(roles_svc.ROLE_ADMIN)
        if err: return err
    """
    user = _current_user_name()
    role = roles_svc.get_role(user)
    if role not in allowed_roles:
        return jsonify({
            "error": "forbidden",
            "message": "Role '%s' is not allowed here. Required: %s" % (role, list(allowed_roles)),
            "you": user,
            "your_role": role,
        }), 403
    return None


@app.route("/api/me", methods=["GET"])
def me():
    """Tell the browser who it is and what tabs it can see.

    Reads X-FIO-User header (set by frontend from localStorage).
    Returns role + allowed tabs + profit center scope.
    """
    name = _current_user_name()
    role = roles_svc.get_role(name)
    allowed_tabs = roles_svc.tabs_for_role(role)
    roles_data = roles_svc.load_roles()
    entry = roles_data.get(name) if name else None
    return jsonify({
        "signed_in_as": name,
        "role": role,
        "allowed_tabs": allowed_tabs,
        "profit_center": (entry or {}).get("profit_center"),
        "title": (entry or {}).get("title"),
        "is_admin": role == roles_svc.ROLE_ADMIN,
        "all_roles": roles_svc.ALL_ROLES,
    })


@app.route("/api/users", methods=["GET"])
def users_with_roles():
    """List ONLY users that the bookkeeper has explicitly imported.

    Per spec: 'нельзя ставить группы людей из BT4YOU.executive.bot' —
    each person is added one-by-one via Upload Accounts from Asana.
    """
    err = _require_role(roles_svc.ROLE_ADMIN)
    if err:
        return err
    roles_data = roles_svc.load_roles()
    # Also enrich with title from BT4YOU snapshot when we can find a match.
    bt_by_name = {p["name"]: p for p in load_people()}
    out = []
    for name, entry in sorted(roles_data.items()):
        bt = bt_by_name.get(name) or {}
        out.append({
            "name": name,
            "title": entry.get("title") or bt.get("title") or "",
            "asana_gid": bt.get("asana_gid", ""),
            "profit_center": entry.get("profit_center") or bt.get("profit_center"),
            "role": entry.get("role") or roles_svc.ROLE_VIEWER,
            "updated_at": entry.get("updated_at"),
            "updated_by": entry.get("updated_by"),
            "in_bt4you": bool(bt),
        })
    return jsonify({
        "users": out,
        "count": len(out),
        "all_roles": roles_svc.ALL_ROLES,
        "tab_access": roles_svc.TAB_ACCESS,
    })


@app.route("/api/users/import-candidates", methods=["GET"])
def import_candidates():
    """Return BT4YOU people NOT yet imported into user_roles.json.

    Used by the 'Upload Accounts from Asana' picker so anyone in the
    admin/HR/bookkeeper triangle can add people. 2026-06-08 widened
    from admin-only after Rita's feedback: 'Accounting or HR should be
    able to import anyone registered in Asana.'
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err
    roles_data = roles_svc.load_roles()
    existing_names = set(roles_data.keys())
    candidates = [p for p in load_people() if p.get("name") not in existing_names]
    return jsonify({
        "candidates": candidates,
        "count": len(candidates),
        "already_imported": sorted(existing_names),
    })


@app.route("/api/users/import", methods=["POST"])
def import_user_from_asana():
    """Import a single BT4YOU person into user_roles.json with default 'viewer'.

    Body: {"name": "Anastasia Lizanets", "role": "viewer" (optional)}
    Widened 2026-06-08: admin OR bookkeeper. One person per call —
    enforces 'each person responsible for their own actions' (per spec).
    """
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    role = (body.get("role") or roles_svc.ROLE_VIEWER).strip()
    if role not in roles_svc.ALL_ROLES:
        return jsonify({"error": "Invalid role",
                        "allowed": roles_svc.ALL_ROLES}), 400
    # Look up BT4YOU details for title + profit_center seed
    bt = next((p for p in load_people() if p.get("name") == name), None)
    title = (bt or {}).get("title", "")
    pc = (bt or {}).get("profit_center")
    try:
        entry = roles_svc.set_role(
            user_name=name,
            role=role,
            profit_center=pc,
            title=title,
            changed_by=_current_user_name() or "admin",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db.insert_audit_log("system", "user_imported", {
        "name": name, "role": role, "from": "BT4YOU/Asana",
        "by": _current_user_name() or "admin",
    })
    return jsonify({"status": "imported", "name": name, "entry": entry})


@app.route("/api/users/<path:user_name>", methods=["DELETE"])
def delete_user(user_name: str):
    """Remove a user from user_roles.json. Admin only.

    Does NOT touch their historical documents -- their name stays in
    audit logs and on past approvals as a paper trail.
    """
    err = _require_role(roles_svc.ROLE_ADMIN)
    if err:
        return err
    roles_data = roles_svc.load_roles()
    if user_name not in roles_data:
        return jsonify({"error": "Not found in roles list",
                        "name": user_name}), 404
    # Don't let an admin nuke themselves (cuts off their own access)
    me = _current_user_name()
    if user_name == me:
        return jsonify({"error": "You can't delete your own role assignment"}), 400
    removed = roles_data.pop(user_name)
    roles_svc.save_roles(roles_data)
    db.insert_audit_log("system", "user_role_removed", {
        "name": user_name,
        "previous_role": removed.get("role"),
        "by": me or "admin",
    })
    return jsonify({"status": "removed", "name": user_name, "previous": removed})


@app.route("/api/users/<path:user_name>/role", methods=["POST"])
def set_user_role(user_name: str):
    """Set or update a user's role. Admin only.

    Body: {"role": "admin|holding_ceo|bookkeeper|stream_owner|viewer",
           "profit_center": "AA"  (optional)}
    """
    err = _require_role(roles_svc.ROLE_ADMIN)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    new_role = (body.get("role") or "").strip()
    if new_role not in roles_svc.ALL_ROLES:
        return jsonify({"error": "Invalid role",
                        "allowed": roles_svc.ALL_ROLES}), 400
    try:
        updated = roles_svc.set_role(
            user_name=user_name,
            role=new_role,
            profit_center=body.get("profit_center"),
            title=body.get("title"),
            changed_by=_current_user_name() or "admin",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    db.insert_audit_log("system", "role_changed", {
        "user": user_name,
        "new_role": new_role,
        "profit_center": body.get("profit_center"),
        "by": _current_user_name() or "admin",
    })
    return jsonify({"status": "ok", "user": user_name, "entry": updated})


@app.route("/api/audit-log", methods=["GET"])
def audit_log():
    """Return recent audit log entries."""
    limit = request.args.get("limit", 50, type=int)
    entries = db.get_audit_log(limit=limit)
    return jsonify(entries)


@app.route("/api/stream-stats", methods=["GET"])
def stream_stats():
    """Return document counts grouped by profit center and status."""
    return jsonify(db.get_document_stats_by_stream())


# ───────────────────────────────────────────────────────────────────────
# 2026-06-07 P1 — FIO users roster (Admin tab) + Legal Entities reference
# NOTE: namespace is /api/fio-users to avoid the older /api/users endpoint
# (line ~2628) which serves the role-assignment table from user_roles.json.
# The two coexist: /api/users = role config; /api/fio-users = HR roster.
# ───────────────────────────────────────────────────────────────────────

@app.route("/api/fio-users", methods=["GET"])
def fio_users_list():
    """List FIO users for the Admin tab and Upload "Who is uploading" dropdown.

    Query params:
      active=true   → only active users (default: all)
      role=<role>   → filter to a single role
    """
    from services import users as users_svc
    active_only = (request.args.get("active") or "").lower() in ("true", "1", "yes")
    role = request.args.get("role") or None
    out = users_svc.list_users(active_only=active_only, role=role)
    return jsonify({"users": out, "roles": users_svc.ROLES})


@app.route("/api/fio-users", methods=["POST"])
def fio_users_create():
    """Create a user. Admin/HR/Bookkeeper-gated (UI controls visibility)."""
    from services import users as users_svc
    body = request.get_json(silent=True) or {}
    actor = request.headers.get("X-FIO-User") or "admin"
    try:
        u = users_svc.create_user(body, created_by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("create_user failed")
        return jsonify({"error": str(exc)}), 500
    db.insert_audit_log("user:" + str(u["id"]), "fio_user_create",
                        {"full_name": u["full_name"], "role": u["role"]},
                        performed_by=actor)
    return jsonify(u), 201


@app.route("/api/fio-users/<int:user_id>", methods=["PATCH"])
def fio_users_update(user_id: int):
    from services import users as users_svc
    body = request.get_json(silent=True) or {}
    actor = request.headers.get("X-FIO-User") or "admin"
    try:
        u = users_svc.update_user(user_id, body, updated_by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not u:
        return jsonify({"error": "Not found"}), 404
    db.insert_audit_log("user:" + str(user_id), "fio_user_update",
                        {"fields": list(body.keys())},
                        performed_by=actor)
    return jsonify(u)


@app.route("/api/fio-users/<int:user_id>", methods=["DELETE"])
def fio_users_delete(user_id: int):
    from services import users as users_svc
    actor = request.headers.get("X-FIO-User") or "admin"
    users_svc.delete_user(user_id)
    db.insert_audit_log("user:" + str(user_id), "fio_user_deactivate",
                        {}, performed_by=actor)
    return jsonify({"status": "deactivated"})


# ───────────────────────────────────────────────────────────────────────
# 2026-06-08 Top-7 P2.3 — Paying accounts CRUD (Admin tab)
# ───────────────────────────────────────────────────────────────────────

@app.route("/api/paying-accounts", methods=["GET"])
def paying_accounts_list():
    """List paying accounts for the Mark Paid dropdown + Admin table."""
    from services import paying_accounts as pa
    active_only = (request.args.get("active") or "").lower() in ("true", "1", "yes")
    legal_entity = request.args.get("legal_entity") or None
    return jsonify({"accounts": pa.list_accounts(
        active_only=active_only, legal_entity=legal_entity)})


@app.route("/api/paying-accounts", methods=["POST"])
def paying_accounts_create():
    from services import paying_accounts as pa
    body = request.get_json(silent=True) or {}
    actor = request.headers.get("X-FIO-User") or "admin"
    try:
        a = pa.create_account(body, created_by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("create paying_account failed")
        return jsonify({"error": str(exc)}), 500
    db.insert_audit_log("paying_account:" + str(a["id"]),
                        "paying_account_create",
                        {"label": a["label"]}, performed_by=actor)
    return jsonify(a), 201


@app.route("/api/paying-accounts/<int:acc_id>", methods=["PATCH"])
def paying_accounts_update(acc_id: int):
    from services import paying_accounts as pa
    body = request.get_json(silent=True) or {}
    actor = request.headers.get("X-FIO-User") or "admin"
    a = pa.update_account(acc_id, body, updated_by=actor)
    if not a:
        return jsonify({"error": "Not found"}), 404
    db.insert_audit_log("paying_account:" + str(acc_id),
                        "paying_account_update",
                        {"fields": list(body.keys())}, performed_by=actor)
    return jsonify(a)


@app.route("/api/paying-accounts/<int:acc_id>", methods=["DELETE"])
def paying_accounts_delete(acc_id: int):
    from services import paying_accounts as pa
    actor = request.headers.get("X-FIO-User") or "admin"
    pa.delete_account(acc_id)
    db.insert_audit_log("paying_account:" + str(acc_id),
                        "paying_account_deactivate",
                        {}, performed_by=actor)
    return jsonify({"status": "deactivated"})


@app.route("/api/legal-entities", methods=["GET"])
def legal_entities():
    """Return the 9 holding legal entities for the Awaiting CEO / Awaiting
    Payment dropdowns. Stream ≠ Legal Entity — see data/legal_entities.json
    docstring for the rationale (Rita's 2026-06-07 feedback)."""
    import json as _json
    path = os.path.join(os.path.dirname(__file__), "data", "legal_entities.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return jsonify(data)
    except (FileNotFoundError, _json.JSONDecodeError) as exc:
        return jsonify({"entities": [], "error": str(exc)}), 500


@app.route("/api/accounting/by-legal-entity", methods=["GET"])
def accounting_by_legal_entity():
    """Per-entity counts + EUR totals for Rita's bookkeeper handover view.

    Query params:
      period=2026-05            optional period filter (YYYY-MM)
      include_unassigned=1      include docs with no legal_entity (default: 1)
    """
    period = (request.args.get("period") or "").strip() or None
    include_unassigned = request.args.get("include_unassigned", "1") == "1"

    conn = db.get_connection()
    try:
        sql = ("SELECT legal_entity, COUNT(*) as cnt, "
               "       SUM(COALESCE(amount, 0)) as total_eur, "
               "       SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) as paid_cnt "
               "FROM documents "
               "WHERE status IN ('approved','posted','confirmed_to_pay','paid','budget_validated','classified')")
        params: List[Any] = []
        if period:
            sql += " AND period = ?"
            params.append(period)
        sql += " GROUP BY legal_entity ORDER BY total_eur DESC NULLS LAST"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    # Load the reference list to surface names + zero-count entities
    import json as _json
    ref_path = os.path.join(os.path.dirname(__file__), "data", "legal_entities.json")
    ref = {"entities": []}
    try:
        with open(ref_path, "r", encoding="utf-8") as f:
            ref = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        pass
    ref_by_code = {e["code"]: e for e in ref.get("entities", [])}

    groups = []
    seen = set()
    for r in rows:
        code = r[0]
        if code is None and not include_unassigned:
            continue
        seen.add(code)
        meta = ref_by_code.get(code, {}) if code else {}
        groups.append({
            "code":         code,
            "name":         meta.get("name") if code else "(unassigned)",
            "country":      meta.get("country", ""),
            "doc_count":    r[1],
            "total_eur":    float(r[2] or 0),
            "paid_count":   r[3],
            "unpaid_count": r[1] - r[3],
        })
    # Surface 0-count entities (so Rita sees the full landscape)
    for code, meta in ref_by_code.items():
        if code not in seen:
            groups.append({
                "code": code, "name": meta["name"], "country": meta.get("country", ""),
                "doc_count": 0, "total_eur": 0.0, "paid_count": 0, "unpaid_count": 0,
            })

    return jsonify({"groups": groups, "period": period})


@app.route("/api/accounting/export-by-legal-entity", methods=["GET"])
def export_by_legal_entity():
    """Download a single CSV scoped to one Legal Entity.

    Reuses the same row-flattening as /api/accounting/export but adds
    a `legal_entity` filter. `unassigned` is a magic value for docs with
    NULL legal_entity (Rita's "what still needs routing" view).
    """
    le_code = (request.args.get("legal_entity") or "").strip()
    if not le_code:
        return jsonify({"error": "legal_entity is required"}), 400
    period = (request.args.get("period") or "").strip() or None

    conn = db.get_connection()
    try:
        sql = ("SELECT * FROM documents WHERE status IN "
               "('approved','posted','confirmed_to_pay','paid','budget_validated','classified')")
        params: List[Any] = []
        if le_code == "unassigned":
            sql += " AND (legal_entity IS NULL OR legal_entity = '')"
        else:
            sql += " AND legal_entity = ?"
            params.append(le_code)
        if period:
            sql += " AND period = ?"
            params.append(period)
        sql += " ORDER BY period DESC, approved_at DESC"
        rows = [db._row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    # Build CSV — same shape as accounting_export
    import csv as _csv
    import io as _io
    from flask import Response as _Resp

    fieldnames = ["document_id", "uploaded_at", "vendor", "vat_number",
                  "period", "profit_center", "ledger_code",
                  "currency_orig", "amount_orig", "amount_eur",
                  "legal_entity", "status", "payment_state",
                  "desired_payment_date", "payment_executed_at",
                  "payment_currency", "payment_amount_orig",
                  "payment_account", "payment_reference",
                  "payment_comment", "approved_by"]
    buf = _io.StringIO()
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    buf.write(f"# Amitours Holding — Per-Legal-Entity Export\n")
    buf.write(f"# Legal Entity: {le_code}\n")
    buf.write(f"# Period: {period or 'ALL'}\n")
    buf.write(f"# Generated at: {generated_at}\n")
    buf.write(f"# Total documents: {len(rows)}\n\n")

    writer = _csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for d in rows:
        writer.writerow({
            "document_id":           d.get("id"),
            "uploaded_at":           d.get("uploaded_at"),
            "vendor":                d.get("vendor"),
            "vat_number":            d.get("vat_number"),
            "period":                d.get("period"),
            "profit_center":         d.get("profit_center"),
            "ledger_code":           d.get("ledger_code"),
            "currency_orig":         d.get("currency_orig") or d.get("currency"),
            "amount_orig":           d.get("amount_orig") or d.get("amount"),
            "amount_eur":            d.get("amount"),
            "legal_entity":          d.get("legal_entity"),
            "status":                d.get("status"),
            "payment_state":         d.get("payment_state"),
            "desired_payment_date":  d.get("desired_payment_date"),
            "payment_executed_at":   d.get("payment_executed_at"),
            "payment_currency":      d.get("payment_currency"),
            "payment_amount_orig":   d.get("payment_amount_orig"),
            "payment_account":       d.get("payment_account"),
            "payment_reference":     d.get("payment_reference"),
            "payment_comment":       d.get("payment_comment"),
            "approved_by":           d.get("approved_by"),
        })

    fname_parts = ["fio_by_entity", le_code]
    if period:
        fname_parts.append(period)
    fname = "_".join(fname_parts) + ".csv"
    return _Resp(buf.getvalue(), mimetype="text/csv",
                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------------------
# Blueprint registration (Phase 7.1 refactor — see docs/architecture.md)
# ---------------------------------------------------------------------------
from routes.card_audit import card_audit_bp  # noqa: E402

app.register_blueprint(card_audit_bp)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    port = int(os.getenv("PORT", "8002"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1")
    logger.info("Starting FIO on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
