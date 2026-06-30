"""Revenue / Accounts Receivable Blueprint — Phase 1 of #94.

Endpoints:
    GET    /api/revenue                          — list (filters)
    GET    /api/revenue/<id>                     — single doc + receipts + audit
    POST   /api/revenue                          — create proforma/invoice/credit_note
    POST   /api/revenue/<id>                     — patch fields
    POST   /api/revenue/<id>/convert             — proforma → invoice
    POST   /api/revenue/<id>/status              — explicit status change
    DELETE /api/revenue/<id>

    GET    /api/revenue/<id>/receipts            — list receipts
    POST   /api/revenue/<id>/receipts            — add receipt
    DELETE /api/revenue-receipts/<int:receipt_id>

Role gating mirrors expense routes:
    read:        admin / holding_ceo / bookkeeper / stream_owner
    write:       admin / bookkeeper / stream_owner (own stream)
    delete:      admin only
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from services import revenue as rev_svc
from services import revenue_receipts as rr_svc
from services import cashflow as cf_svc
from services import revenue_bank_match as bm_svc
from services import roles as roles_svc

logger = logging.getLogger(__name__)

revenue_bp = Blueprint("revenue", __name__, url_prefix="/api")

_READ_ROLES = ("admin", "holding_ceo", "bookkeeper", "stream_owner")
_WRITE_ROLES = ("admin", "bookkeeper", "stream_owner")
_DELETE_ROLES = ("admin",)


def _user():
    return (request.headers.get("X-FIO-User") or "").strip() or None


def _require(*allowed):
    role = roles_svc.get_role(_user())
    if role not in allowed:
        return jsonify({
            "error": "forbidden",
            "your_role": role,
            "required": list(allowed),
        }), 403
    return None


# 2026-06-26 (D) — capability gate (mirrors app._require_capability,
# kept local to avoid circular import).
def _require_cap(cap: str):
    user = _user()
    if roles_svc.has_capability(user, cap):
        return None
    return jsonify({
        "error": "forbidden",
        "your_role": roles_svc.get_role(user),
        "missing_capability": cap,
        "message": "Capability '%s' required for this Revenue action." % cap,
    }), 403


# ── Documents ────────────────────────────────────────────────────────────────

@revenue_bp.route("/revenue", methods=["GET"])
def list_revenue() -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    return jsonify({
        "docs": rev_svc.list_docs(
            period=request.args.get("period") or None,
            pc=request.args.get("pc") or None,
            status=request.args.get("status") or None,
            kind=request.args.get("kind") or None,
            q=request.args.get("q") or None,
            limit=int(request.args.get("limit") or 500),
        ),
    })


@revenue_bp.route("/revenue/<doc_id>", methods=["GET"])
def get_revenue(doc_id: str) -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    doc = rev_svc.get_doc(doc_id)
    if not doc:
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        "doc": doc,
        "receipts": rr_svc.list_receipts(doc_id),
        "total_received": rr_svc.total_received(doc_id),
        "remaining": rr_svc.remaining(doc_id),
        "audit": rev_svc.audit_for(doc_id),
    })


# 2026-06-30 — Revenue file-serve route. The Add Revenue modal already
# persists the attached PDF/image at doc.file_path (FB-A), but until this
# endpoint existed the detail modal could only show the metadata — the
# operator had to know where the file was on disk to look at it.
@revenue_bp.route("/revenue/<doc_id>/file", methods=["GET"])
def revenue_get_file(doc_id: str) -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    doc = rev_svc.get_doc(doc_id)
    if not doc:
        return jsonify({"error": "not_found"}), 404
    file_path = doc.get("file_path")
    if not file_path:
        return jsonify({"error": "no_file_attached",
                        "message": "This revenue document has no attached "
                                    "file. Upload one via the Add Revenue "
                                    "modal."}), 404
    import os as _os
    from flask import send_file as _send_file
    if not _os.path.isfile(file_path):
        return jsonify({"error": "file_missing_on_disk",
                        "expected_path": file_path}), 404
    ext = (doc.get("file_type") or "").lower()
    mime = {
        "pdf":  "application/pdf",
        "jpg":  "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(ext, "application/octet-stream")
    return _send_file(file_path, mimetype=mime, as_attachment=False,
                       download_name=_os.path.basename(file_path))


@revenue_bp.route("/revenue", methods=["POST"])
def create_revenue() -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    # 2026-06-24 FB-A — accept both JSON and multipart (with optional file).
    # The Revenue tab's Add modal now ships the invoice PDF alongside the form.
    file_obj = None
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        payload = {k: v for k, v in request.form.items()}
        # Coerce types that were JSON numbers but are now form strings
        for fld in ("amount", "amount_eur"):
            if fld in payload and payload[fld]:
                try:
                    payload[fld] = float(payload[fld])
                except (TypeError, ValueError):
                    pass
        file_obj = request.files.get("file")
    else:
        payload = request.get_json(silent=True) or {}

    try:
        doc = rev_svc.create_doc(payload, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400

    # Persist the attached file on the data volume + record path on the doc.
    if file_obj and file_obj.filename:
        import os
        import uuid
        import config as _cfg
        ext = os.path.splitext(file_obj.filename)[1].lower()
        safe = f"rev_{doc['id']}_{uuid.uuid4().hex[:8]}{ext}"
        target_dir = os.path.join(_cfg.UPLOAD_FOLDER, "revenue")
        os.makedirs(target_dir, exist_ok=True)
        file_path = os.path.join(target_dir, safe)
        file_obj.save(file_path)
        try:
            rev_svc.update_doc(doc["id"], {
                "file_path": file_path,
                "file_type": ext.lstrip("."),
            }, by=_user())
        except Exception:  # noqa: BLE001
            pass
        doc = rev_svc.get_doc(doc["id"])

    return jsonify({"doc": doc}), 201


@revenue_bp.route("/revenue/<doc_id>", methods=["POST"])
def update_revenue(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        doc = rev_svc.update_doc(doc_id, payload, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    if not doc:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"doc": doc})


@revenue_bp.route("/revenue/<doc_id>/convert", methods=["POST"])
def convert_revenue(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        doc = rev_svc.convert_proforma_to_invoice(doc_id, payload, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    return jsonify({"doc": doc}), 201


@revenue_bp.route("/revenue/<doc_id>/status", methods=["POST"])
def status_revenue(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    new_status = (payload.get("status") or "").strip()
    try:
        doc = rev_svc.update_status(doc_id, new_status, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    if not doc:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"doc": doc})


@revenue_bp.route("/revenue/<doc_id>", methods=["DELETE"])
def delete_revenue(doc_id: str) -> Any:
    err = _require(*_DELETE_ROLES)
    if err:
        return err
    ok = rev_svc.delete_doc(doc_id, by=_user())
    if not ok:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


# ── Receipts ─────────────────────────────────────────────────────────────────

@revenue_bp.route("/revenue/<doc_id>/receipts", methods=["GET"])
def list_receipts(doc_id: str) -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    return jsonify({
        "receipts": rr_svc.list_receipts(doc_id),
        "total_received": rr_svc.total_received(doc_id),
        "remaining": rr_svc.remaining(doc_id),
    })


@revenue_bp.route("/revenue/<doc_id>/receipts", methods=["POST"])
def add_receipt(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        receipt = rr_svc.add_receipt(
            doc_id,
            amount_eur=payload.get("amount_eur"),
            received_at=payload.get("received_at"),
            method=payload.get("method"),
            reference=payload.get("reference"),
            bank_statement_tx_id=payload.get("bank_statement_tx_id"),
            by=_user(),
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    return jsonify({
        "receipt": receipt,
        "doc": rev_svc.get_doc(doc_id),  # so UI sees status transition immediately
        "total_received": rr_svc.total_received(doc_id),
        "remaining": rr_svc.remaining(doc_id),
    }), 201


@revenue_bp.route("/revenue-receipts/<int:receipt_id>", methods=["DELETE"])
def delete_receipt(receipt_id: int) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    ok = rr_svc.delete_receipt(receipt_id, by=_user())
    if not ok:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


# ── Cashflow analytics (Phase 2 of #94) ─────────────────────────────────────

# 2026-06-26 (G1) — 13-week rolling cash-flow projection (Phase 3 G1).
# Self-service read for anyone who can see revenue analytics.
@revenue_bp.route("/cashflow/projection", methods=["GET"])
def cashflow_projection() -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    from services import cashflow_projection as cp_svc
    try:
        weeks = int(request.args.get("weeks") or 13)
    except ValueError:
        weeks = 13
    pc = request.args.get("pc") or None
    opening_override = request.args.get("opening_eur")
    try:
        opening_override = float(opening_override) if opening_override else None
    except ValueError:
        opening_override = None
    try:
        return jsonify(cp_svc.project(weeks=weeks, pc=pc,
                                      opening_override=opening_override))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@revenue_bp.route("/cashflow/opening-balance", methods=["GET"])
def cashflow_opening_balance() -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    from services import cashflow_projection as cp_svc
    pc = request.args.get("pc") or None
    return jsonify({
        "pc": pc or "ALL",
        "opening_balance_eur": cp_svc.opening_balance_for(pc),
        "snapshots": cp_svc.list_opening_balances(pc=pc, limit=20),
    })


@revenue_bp.route("/cashflow/opening-balance", methods=["POST"])
def cashflow_set_opening_balance() -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("manage_payees")
    if err:
        return err
    from services import cashflow_projection as cp_svc
    body = request.get_json(silent=True) or {}
    pc = (body.get("pc") or "").strip()
    balance_eur = body.get("balance_eur")
    if not pc:
        return jsonify({"error": "pc required"}), 400
    try:
        balance_eur = float(balance_eur)
    except (TypeError, ValueError):
        return jsonify({"error": "balance_eur must be a number"}), 400
    try:
        rec = cp_svc.set_opening_balance(
            pc=pc, balance_eur=balance_eur,
            as_of_date=(body.get("as_of_date") or "").strip() or None,
            paying_account_id=body.get("paying_account_id"),
            legal_entity=(body.get("legal_entity") or "").strip() or None,
            balance_orig=body.get("balance_orig"),
            currency=(body.get("currency") or "").strip() or None,
            source=(body.get("source") or "manual").strip(),
            by=_user(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    return jsonify(rec), 201


@revenue_bp.route("/cashflow/check-runway", methods=["POST"])
def cashflow_check_runway() -> Any:
    """G-xalarm — manual or cron trigger to evaluate runway and fire alarm.
    Returns the alarm payload (or {fired: false, reason: ...}) for observability.
    """
    err = _require(*_WRITE_ROLES) or _require_cap("approve_budget")
    if err:
        return err
    from services import xalarm as _xa
    body = request.get_json(silent=True) or {}
    try:
        threshold = int(body.get("threshold_weeks") or _xa.RUNWAY_DEFAULT_THRESHOLD_WEEKS)
    except (TypeError, ValueError):
        threshold = _xa.RUNWAY_DEFAULT_THRESHOLD_WEEKS
    pc = (body.get("pc") or "").strip() or None
    result = _xa.fire_if_low_runway(threshold_weeks=threshold, pc=pc, actor=_user())
    if result is None:
        return jsonify({"fired": False, "threshold_weeks": threshold, "pc": pc or "ALL"})
    return jsonify({"fired": True, **result})


# ─────────────────────────────────────────────────────────────────────
# Weekly cashflow timeline (Amitours UNIFIED CASH TIMELINE) — 2026-06-29.
# Read endpoints land first; UI editor + Google-Sheet import in next phase.
# ─────────────────────────────────────────────────────────────────────

@revenue_bp.route("/cashflow/weekly", methods=["GET"])
def cashflow_weekly_list() -> Any:
    """Return weekly rows centred on the current ISO Monday.
    Query params:
      weeks_before (int, default 8)
      weeks_after  (int, default 13)
      row_types    (csv: actual,forecast,estimate,plug; default all)
    """
    err = _require(*_READ_ROLES)
    if err:
        return err
    from services import cashflow_weekly as _cw
    try:
        wb = int(request.args.get("weeks_before") or 8)
        wa = int(request.args.get("weeks_after") or 13)
    except ValueError:
        return jsonify({"error": "weeks_before / weeks_after must be integers"}), 400
    raw_types = (request.args.get("row_types") or "").strip()
    row_types = [t.strip() for t in raw_types.split(",") if t.strip()] or None
    if row_types:
        bad = [t for t in row_types if t not in _cw.VALID_ROW_TYPES]
        if bad:
            return jsonify({"error": "invalid row_types",
                            "invalid": bad,
                            "valid": list(_cw.VALID_ROW_TYPES)}), 400
    try:
        return jsonify(_cw.list_weeks(weeks_before=wb, weeks_after=wa, row_types=row_types))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@revenue_bp.route("/cashflow/weekly/totals", methods=["GET"])
def cashflow_weekly_totals() -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    from services import cashflow_weekly as _cw
    try:
        wb = int(request.args.get("weeks_before") or 8)
        wa = int(request.args.get("weeks_after") or 13)
    except ValueError:
        return jsonify({"error": "weeks_before / weeks_after must be integers"}), 400
    return jsonify(_cw.totals(weeks_before=wb, weeks_after=wa))


@revenue_bp.route("/cashflow/weekly", methods=["POST"])
def cashflow_weekly_upsert() -> Any:
    """Upsert one forecast / estimate / plug row.
    Body: {week_start, row_type, week_label?, note?, fields: {…}}
    Actuals are derived; this endpoint refuses row_type=actual.
    """
    err = _require(*_WRITE_ROLES) or _require_cap("approve_budget")
    if err:
        return err
    from services import cashflow_weekly as _cw
    body = request.get_json(silent=True) or {}
    try:
        rec = _cw.upsert_row(
            week_start=(body.get("week_start") or "").strip(),
            row_type=(body.get("row_type") or "").strip(),
            fields=body.get("fields") or {},
            week_label=(body.get("week_label") or "").strip() or None,
            note=(body.get("note") or "").strip() or None,
            source=(body.get("source") or "").strip() or None,
            by=_user(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(rec), 201


@revenue_bp.route("/cashflow/weekly/import-tsv", methods=["POST"])
def cashflow_weekly_import_tsv() -> Any:
    """Accept a paste from the operator's Google Sheet (TSV or CSV) and
    upsert each row as forecast/estimate/plug. Returns a structured
    summary: rows_seen, rows_imported, skipped_examples, unknown_columns.

    Body: {text: "...", default_row_type?: 'forecast', dry_run?: false}
    """
    err = _require(*_WRITE_ROLES) or _require_cap("approve_budget")
    if err:
        return err
    from services import cashflow_weekly as _cw
    body = request.get_json(silent=True) or {}
    text = body.get("text") or ""
    drt = (body.get("default_row_type") or "forecast").strip()
    if drt not in _cw.WRITABLE_ROW_TYPES:
        return jsonify({"error": "default_row_type must be one of "
                                  + str(list(_cw.WRITABLE_ROW_TYPES))}), 400
    try:
        out = _cw.import_tsv(text=text,
                              default_row_type=drt,
                              by=_user(),
                              dry_run=bool(body.get("dry_run")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(out), 201 if not body.get("dry_run") else 200


@revenue_bp.route("/cashflow/weekly/derive-actuals", methods=["POST"])
def cashflow_weekly_derive_actuals() -> Any:
    """Recompute 'actual' rows for the past N weeks from source data
    (revenue_receipts + paid documents + bank_account_balances). Idempotent —
    wipes prior actuals in the window first.

    Body: {weeks_before?: 26, weeks_after?: 0}
    """
    err = _require(*_WRITE_ROLES) or _require_cap("approve_budget")
    if err:
        return err
    from services import cashflow_weekly as _cw
    body = request.get_json(silent=True) or {}
    try:
        wb = int(body.get("weeks_before") or 26)
        wa = int(body.get("weeks_after") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "weeks_before / weeks_after must be ints"}), 400
    out = _cw.derive_actuals(weeks_before=wb, weeks_after=wa, by=_user())
    return jsonify(out)


@revenue_bp.route("/cashflow/weekly/<week_start>/<row_type>", methods=["DELETE"])
def cashflow_weekly_delete(week_start: str, row_type: str) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("approve_budget")
    if err:
        return err
    from services import cashflow_weekly as _cw
    try:
        deleted = _cw.delete_row(week_start, row_type)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not deleted:
        return jsonify({"error": "not_found",
                        "week_start": week_start, "row_type": row_type}), 404
    return jsonify({"deleted": True,
                    "week_start": week_start, "row_type": row_type})


@revenue_bp.route("/cashflow", methods=["GET"])
def cashflow_monthly() -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    pf = request.args.get("from") or None
    pt = request.args.get("to") or None
    pc = request.args.get("pc") or None
    return jsonify({
        "series": cf_svc.monthly_series(pf, pt, pc),
        "totals": cf_svc.totals_for_period(pf, pt, pc),
    })


@revenue_bp.route("/cashflow/by-stream", methods=["GET"])
def cashflow_by_stream() -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    return jsonify({
        "rows": cf_svc.breakdown_by_stream(
            request.args.get("from") or None,
            request.args.get("to") or None,
        ),
    })


@revenue_bp.route("/revenue/bank-match/suggestions/<batch_id>", methods=["GET"])
def bank_match_suggestions(batch_id: str) -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    return jsonify({"suggestions": bm_svc.suggestions_for_batch(batch_id)})


@revenue_bp.route("/revenue/bank-match/apply", methods=["POST"])
def bank_match_apply() -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    tx_id = (payload.get("tx_id") or "").strip()
    doc_id = (payload.get("revenue_doc_id") or "").strip()
    if not tx_id or not doc_id:
        return jsonify({"error": "tx_id and revenue_doc_id required"}), 400
    try:
        receipt = bm_svc.apply_match(tx_id, doc_id, by=_user())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"receipt": receipt,
                    "doc": rev_svc.get_doc(doc_id)}), 201


@revenue_bp.route("/revenue/bank-match/auto/<batch_id>", methods=["POST"])
def bank_match_auto(batch_id: str) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("create_revenue")
    if err:
        return err
    min_score = int(request.args.get("min_score") or 80)
    applied = bm_svc.auto_match_batch(batch_id, min_score=min_score, by=_user())
    return jsonify({"applied": applied, "count": len(applied)})


# 2026-06-26 (G2) — consolidated P&L with intercompany elimination.
# Per FIO Governance SOP §4.2.
@revenue_bp.route("/pnl/consolidated", methods=["GET"])
def pnl_consolidated() -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    from services import intercompany as ic_svc
    return jsonify(ic_svc.consolidated_pnl(
        period=request.args.get("period") or None,
        pc=request.args.get("pc") or None,
    ))


@revenue_bp.route("/pnl/intercompany-pairs", methods=["GET"])
def pnl_intercompany_pairs() -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    from services import intercompany as ic_svc
    return jsonify({"pairs": ic_svc.by_pair(period=request.args.get("period") or None)})


@revenue_bp.route("/documents/<doc_id>/counterparty-pc", methods=["POST"])
def set_doc_counterparty_pc(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES) or _require_cap("post_to_pnl")
    if err:
        return err
    from services import intercompany as ic_svc
    body = request.get_json(silent=True) or {}
    raw = body.get("counterparty_pc")
    cp = (raw or "").strip() or None
    ok = ic_svc.set_counterparty_pc(doc_id, cp, by=_user())
    if not ok:
        return jsonify({"error": "doc not found"}), 404
    return jsonify({"doc_id": doc_id, "counterparty_pc": cp})


@revenue_bp.route("/cashflow/by-ledger", methods=["GET"])
def cashflow_by_ledger() -> Any:
    err = _require(*_READ_ROLES) or _require_cap("view_revenue")
    if err:
        return err
    return jsonify({
        "rows": cf_svc.breakdown_by_ledger(
            request.args.get("from") or None,
            request.args.get("to") or None,
            request.args.get("side") or "both",
        ),
    })
