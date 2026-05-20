"""Card Audit Blueprint — month-close workflow.

Extracted from app.py during Phase 7.1 refactor. Endpoints:
- POST   /api/card-audit/import              upload CSV, auto-detect format, reconcile
- GET    /api/card-audit/transactions        list with filters
- GET    /api/card-audit/summary             KPI summary for a period
- POST   /api/card-audit/transactions/<id>   update (assign holder, manual match, exclude)
- DELETE /api/card-audit/transactions/<id>   delete
- POST   /api/card-audit/reconcile           manual reconcile trigger
- GET    /api/card-audit/export              CSV export with corporate header

All routes require Basic Auth (enforced by app-level middleware).
"""
from __future__ import annotations

import csv as _csv
import io as _io
import logging
from datetime import datetime
from typing import Any, Dict

from flask import Blueprint, Response, jsonify, request

from services import db, card_audit, bt4you_sync as bts

logger = logging.getLogger(__name__)

card_audit_bp = Blueprint("card_audit", __name__, url_prefix="/api/card-audit")


@card_audit_bp.route("/import", methods=["POST"])
def card_audit_import() -> Any:
    """Upload bank/card statement CSV. Auto-detects format and persists rows.

    multipart/form-data with field 'file'. Optional form field 'source' to
    force format (mercury / revolut / stripe / airwallex / generic).
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400

    auth = request.authorization
    imported_by = (request.form.get("imported_by")
                   or (auth.username if auth else None)
                   or "user")
    source_override = (request.form.get("source") or "").strip() or None

    try:
        result = card_audit.import_csv(
            raw, f.filename, imported_by=imported_by, source_override=source_override
        )
    except Exception as exc:
        logger.exception("card_audit import failed")
        return jsonify({"error": str(exc)}), 500

    # Kick off reconciliation for the periods touched by this batch
    try:
        conn = db.get_connection()
        periods = [r[0] for r in conn.execute(
            "SELECT DISTINCT period FROM card_transactions WHERE batch_id = ?",
            (result["batch_id"],)
        ).fetchall()]
        conn.close()
        for p in periods:
            card_audit.reconcile_period(p)
        result["periods_reconciled"] = periods
    except Exception as exc:
        logger.warning("post-import reconcile failed: %s", exc)
        result["reconcile_warning"] = str(exc)

    db.insert_audit_log("system", "card_audit_import", {
        "batch_id":  result["batch_id"],
        "source":    result["source"],
        "filename":  f.filename,
        "inserted":  result["inserted"],
        "skipped":   result["skipped"],
        "imported_by": imported_by,
    })
    return jsonify(result)


@card_audit_bp.route("/transactions", methods=["GET"])
def card_audit_list() -> Any:
    return jsonify({
        "transactions": card_audit.list_card_tx(
            period=request.args.get("period"),
            department=request.args.get("department"),
            card_holder=request.args.get("card_holder"),
            match_status=request.args.get("status"),
            limit=int(request.args.get("limit", 500)),
        ),
    })


@card_audit_bp.route("/summary", methods=["GET"])
def card_audit_summary() -> Any:
    period = (request.args.get("period") or datetime.utcnow().strftime("%Y-%m"))
    return jsonify(card_audit.audit_summary(period))


@card_audit_bp.route("/transactions/<tx_id>", methods=["POST"])
def card_audit_update(tx_id: str) -> Any:
    """Update card-tx fields: assign card_holder + auto-resolve department/PC,
    manually attach/detach an invoice, or add a note."""
    tx = card_audit.get_card_tx(tx_id)
    if not tx:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(silent=True) or {}
    updates: Dict[str, Any] = {}

    # Assign card holder → auto-resolve department + PC via BT4YOU map
    if "card_holder" in body:
        holder = (body["card_holder"] or "").strip() or None
        updates["card_holder"] = holder
        if holder:
            try:
                pm = bts.build_people_map()
                lookup = bts.suggest_pc_for_uploader(holder, pm)
                if lookup:
                    updates["department"] = lookup.get("department")
                    updates["profit_center"] = lookup.get("profit_center")
            except Exception as exc:
                logger.debug("holder->dept resolve failed: %s", exc)
        else:
            updates["department"] = None
            updates["profit_center"] = None

    # Direct override
    for k in ("department", "profit_center", "notes"):
        if k in body:
            updates[k] = body[k]

    # Manual match / unmatch
    if "matched_invoice_id" in body:
        inv_id = body["matched_invoice_id"] or None
        if inv_id:
            updates["matched_invoice_id"] = inv_id
            updates["match_status"] = "manual"
            updates["match_confidence"] = 100
            updates["match_reason"] = "manual link by " + (body.get("changed_by") or "user")
        else:
            updates["matched_invoice_id"] = None
            updates["match_status"] = "unmatched"
            updates["match_confidence"] = 0
            updates["match_reason"] = "manual unlink"

    # Exclude from audit (e.g. personal expense reimbursed separately)
    if body.get("exclude") is True:
        updates["match_status"] = "excluded"
        updates["match_reason"] = (body.get("exclude_reason") or "marked as excluded")
    elif body.get("exclude") is False and tx.get("match_status") == "excluded":
        updates["match_status"] = "unmatched"
        updates["match_reason"] = None

    if not updates:
        return jsonify({"status": "no_changes"})

    card_audit.update_card_tx(tx_id, updates)
    db.insert_audit_log("card_tx_" + tx_id, "card_audit_update", {
        "fields": list(updates.keys()), "by": body.get("changed_by", "user"),
    })
    return jsonify({"status": "saved", "transaction": card_audit.get_card_tx(tx_id)})


@card_audit_bp.route("/transactions/<tx_id>", methods=["DELETE"])
def card_audit_delete(tx_id: str) -> Any:
    card_audit.delete_card_tx(tx_id)
    db.insert_audit_log("card_tx_" + tx_id, "card_audit_delete", {})
    return jsonify({"status": "deleted"})


@card_audit_bp.route("/reconcile", methods=["POST"])
def card_audit_reconcile() -> Any:
    """Manually trigger reconciliation for a period (or current month)."""
    body = request.get_json(silent=True) or {}
    period = (body.get("period") or request.args.get("period")
              or datetime.utcnow().strftime("%Y-%m"))
    counts = card_audit.reconcile_period(period)
    return jsonify({"period": period, **counts})


@card_audit_bp.route("/export", methods=["GET"])
def card_audit_export() -> Any:
    """Export card-tx as CSV — same corporate header as accounting export."""
    period = (request.args.get("period") or datetime.utcnow().strftime("%Y-%m"))
    department = request.args.get("department") or None
    status = request.args.get("status") or None

    rows = card_audit.list_card_tx(period=period, department=department,
                                   match_status=status, limit=5000)
    buf = _io.StringIO()
    buf.write("# Amitours Holding -- FIO Card Audit Export\n")
    buf.write(f"# Period: {period}\n")
    buf.write(f"# Department filter: {department or 'ALL'}\n")
    buf.write(f"# Status filter: {status or 'ALL'}\n")
    buf.write(f"# Generated at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n")
    buf.write(f"# Total rows: {len(rows)}\n")
    buf.write("# Source system: FIO Accounting Bot -- Card Audit module\n\n")
    fields = ["id", "source", "posted_at", "period", "card_holder", "department",
              "profit_center", "amount", "currency", "amount_eur", "fx_rate",
              "description", "counterparty", "reference", "match_status",
              "match_confidence", "matched_invoice_id", "match_reason", "notes"]
    w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    fname = f"fio_card_audit_{period}{'_' + department if department else ''}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})
