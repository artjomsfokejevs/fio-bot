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

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "csv", "xlsx"}


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

    for f in files:
        if not f.filename or not _allowed_file(f.filename):
            results.append({"filename": f.filename, "error": "Invalid file type"})
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
                    top_code = classification["codes"][0] if classification["codes"] else {}
                    vendor_data = single_doc.get("vendor", "")
                    vendor = vendor_data.get("name", str(vendor_data)) if isinstance(vendor_data, dict) else str(vendor_data)
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

                # Derive period from parsed dates
                doc_date = parsed.get("dates", {}).get("document_date")
                if doc_date:
                    update_fields["period"] = doc_date[:7]
                else:
                    update_fields["period"] = datetime.utcnow().strftime("%Y-%m")

                db.update_document(doc_id, update_fields)
                db.insert_audit_log(doc_id, "parsed", {"vendor": vendor})
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

        except Exception as exc:
            logger.exception("Auto-parse failed for %s", doc_id)
            db.update_document(doc_id, {"error": str(exc)})
            results.append({"id": doc_id, "filename": f.filename, "status": "pending", "error": str(exc)})

    return jsonify({"uploaded": results}), 201


# ---------------------------------------------------------------------------
# Document CRUD
# ---------------------------------------------------------------------------

@app.route("/api/documents", methods=["GET"])
def list_documents():
    """List all documents, optionally filtered by status, profit_center, and/or ledger_code."""
    status = request.args.get("status")
    profit_center = request.args.get("profit_center")
    ledger_code = request.args.get("ledger_code")

    if profit_center:
        docs = db.get_documents_by_profit_center(profit_center, status=status)
    else:
        docs = db.get_documents(status=status)

    # Filter by ledger_code in Python (supports drill-down without DB changes)
    if ledger_code:
        docs = [d for d in docs if d.get("ledger_code") == ledger_code]

    for doc in docs:
        _enrich_document(doc)
    return jsonify(docs)


@app.route("/api/documents/<doc_id>", methods=["DELETE"])
def delete_doc(doc_id: str):
    """Delete a document by ID, removing from DB, disk, AND actuals."""
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "Not found"}), 404

    # If document was posted, subtract from actuals
    if doc.get("status") == "posted" and doc.get("ledger_code") and doc.get("amount"):
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
    """Remove a deleted document's amount from accounting_actuals.json."""
    try:
        with open(config.ACTUALS_FILE, "r") as f:
            actuals = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    pc = doc.get("profit_center", "AG")
    mapping = {"AG": "amitours_group", "AA": "alps2alps", "RR": "rock2rock",
               "BK": "skibookers", "SR": "skipasser", "MT": "mountly",
               "AH": "mountly", "PK": "mypeak_finance", "CF": "mypeak_finance",
               "AL": "alveda"}
    stream = mapping.get(pc, pc.lower())
    period = doc.get("period", "")
    code = doc.get("ledger_code", "")
    amount = doc.get("amount", 0)

    streams = actuals.get("streams", {})
    if stream in streams and period in streams[stream] and code in streams[stream][period]:
        streams[stream][period][code] = round(streams[stream][period][code] - amount, 2)
        if streams[stream][period][code] <= 0:
            del streams[stream][period][code]
        if not streams[stream][period]:
            del streams[stream][period]
        if not streams[stream]:
            del streams[stream]

    with open(config.ACTUALS_FILE, "w") as f:
        json.dump(actuals, f, indent=2, ensure_ascii=False)

    logger.info("Subtracted EUR %.2f (%s/%s/%s) from actuals after delete", amount, stream, period, code)


# ---------------------------------------------------------------------------
# BT4YOU SYNC
# ---------------------------------------------------------------------------

@app.route("/api/sync-bt4you", methods=["POST"])
def sync_to_bt4you():
    """Push FIO actuals to BT4YOU Executive Bot.

    Sends the accounting_actuals.json to BT4YOU's cashflow fact endpoint.
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
    parsed = doc.get("parsed_json") or {}
    classification = doc.get("classification_json") or {}

    # Extract VAT number
    vendor_data = parsed.get("vendor", {})
    if isinstance(vendor_data, dict):
        doc["vat_number"] = vendor_data.get("vat_number") or None
    else:
        doc["vat_number"] = None

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

    # Filter actuals by stream if requested
    if stream_filter and stream_filter in actuals.get("streams", {}):
        actuals = {"streams": {stream_filter: actuals["streams"][stream_filter]}}
    elif pc_filter and not stream_filter:
        actuals = {"streams": {}}  # Unknown PC -- empty

    docs = db.get_documents()
    if pc_filter:
        docs = [d for d in docs if (d.get("profit_center") or "").upper() == pc_filter]

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
    })


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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    port = int(os.getenv("PORT", "8002"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1")
    logger.info("Starting FIO on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
