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
    """Upload bank/card statement (CSV, XLSX, or PDF). Auto-detects format.

    multipart/form-data fields:
      file: required, the statement
      source: optional, force format (mercury / revolut / stripe / airwallex / generic)
      profit_center: optional, stamp all rows in this batch with this PC
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "empty file"}), 400

    auth = request.authorization
    imported_by = (request.form.get("imported_by")
                   or request.headers.get("X-FIO-User")
                   or (auth.username if auth else None)
                   or "user")
    source_override = (request.form.get("source") or "").strip() or None
    profit_center = (request.form.get("profit_center") or "").strip() or None

    try:
        result = card_audit.import_statement(
            raw, f.filename,
            imported_by=imported_by,
            source_override=source_override,
            profit_center=profit_center,
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


# 2026-06-11 (Top-9 self-review P1.1 fix) — single-tx fetch endpoint.
# The modal previously called .list(limit=1) + .find() which silently
# failed when the target tx wasn't the first row. Now there's a direct
# GET for the row.
@card_audit_bp.route("/transactions/<tx_id>", methods=["GET"])
def card_audit_get_one(tx_id: str) -> Any:
    tx = card_audit.get_card_tx(tx_id)
    if not tx:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"transaction": tx})


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


# ───────────────────────────────────────────────────────────────────────
# 2026-06-11 Top-9 P2.6 + P2.7 — Month-close mandatory workflow
# Rita's request: bookkeeper runs ALL bank statements at month-close to
# confirm "we received every document". Unmatched → AI determines which
# business stream the transaction belongs to + bookkeeper triggers a
# chase task (Asana or email template).
# ───────────────────────────────────────────────────────────────────────

@card_audit_bp.route("/month-close-status", methods=["GET"])
def card_audit_month_close_status() -> Any:
    """Checklist payload for the Month-Close section in Bank Statement Audit.

    Returns 3 booleans + counts so the UI can render a traffic-light
    progress bar:
        statements_imported     — at least 1 batch in this period
        all_reconciled           — every imported tx has match_status ≠ unmatched
        unmatched_have_owner     — every unmatched has card_holder OR
                                   profit_center filled (so a stakeholder
                                   exists to chase)
    Plus the count of tx still requiring action.
    """
    from services import card_audit as ca_svc
    period = (request.args.get("period")
              or datetime.utcnow().strftime("%Y-%m"))
    summary = ca_svc.audit_summary(period)
    by_status = summary.get("by_status", {})
    # 2026-06-11 (Top-2 Phase A test catch) — audit_summary returns
    # {"total": {"n", "eur"}}, not a flat "total_transactions" int.
    total_obj = summary.get("total") or {}
    total = int(total_obj.get("n", 0) or 0) if isinstance(total_obj, dict) else int(total_obj or 0)
    # 2026-06-11 (Top-2 phase A test catch) — by_status values are
    # {n, eur} dicts, not ints. Extract the count.
    def _n(status_key):
        v = by_status.get(status_key, {})
        if isinstance(v, dict):
            return int(v.get("n", 0) or 0)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    unmatched = _n("unmatched")
    suggested = _n("suggested")
    matched = _n("auto") + _n("manual")

    statements_imported = total > 0
    # "all reconciled" means: no row left with status 'unmatched' OR
    # 'suggested'. Bookkeeper must explicitly Confirm / Assign each one.
    all_reconciled = statements_imported and (unmatched + suggested == 0)

    # Pull unmatched txs to verify they have at least an owner
    unmatched_rows = ca_svc.list_card_tx(period=period, match_status="unmatched")
    unmatched_without_owner = sum(
        1 for t in unmatched_rows
        if not (t.get("card_holder") or t.get("profit_center"))
    )
    unmatched_have_owner = statements_imported and unmatched_without_owner == 0

    can_close = all_reconciled and unmatched_have_owner

    return jsonify({
        "period": period,
        "total_transactions": total,
        "matched_count": matched,
        "suggested_count": suggested,
        "unmatched_count": unmatched,
        "unmatched_without_owner": unmatched_without_owner,
        "checks": {
            "statements_imported": statements_imported,
            "all_reconciled": all_reconciled,
            "unmatched_have_owner": unmatched_have_owner,
        },
        "can_close": can_close,
    })


@card_audit_bp.route("/transactions/<tx_id>/suggest-owner", methods=["POST"])
def card_audit_suggest_owner(tx_id: str) -> Any:
    """AI guess for who owns an unmatched transaction.

    Uses BT4YOU map + transaction description heuristics. Returns the
    suggestion without writing it — bookkeeper confirms via the existing
    Assign flow.
    """
    tx = card_audit.get_card_tx(tx_id)
    if not tx:
        return jsonify({"error": "Not found"}), 404

    # Three signals, in order of confidence:
    # 1. If counterparty / description contains a known vendor that
    #    suggests a stream (Stripe → AA tech, Hetzner → AA infra, etc.)
    # 2. Card holder already set → resolve via BT4YOU
    # 3. Fallback: AA (Alps2Alps) — the dominant stream by volume
    description = ((tx.get("counterparty") or "") + " " +
                   (tx.get("description") or "")).lower()
    holder = tx.get("card_holder")

    suggested_pc = None
    suggested_person = None
    reason = ""

    if holder:
        try:
            people_map = bts.build_people_map()
            lookup = bts.suggest_pc_for_uploader(holder, people_map)
            if lookup:
                suggested_pc = lookup.get("profit_center")
                suggested_person = holder
                reason = "cardholder " + holder + " → " + (suggested_pc or "?")
        except Exception:  # noqa: BLE001 — defensive: BT4YOU map can be stale
            pass

    if not suggested_pc:
        try:
            vendor_match = bts.suggest_pc_for_vendor(description, "")
            if vendor_match:
                suggested_pc = vendor_match.get("profit_center")
                suggested_person = vendor_match.get("primary_contact") or ""
                reason = "vendor `" + (vendor_match.get("brand") or "") + "` → " + (suggested_pc or "?")
        except Exception:  # noqa: BLE001 — defensive: BT4YOU map can be stale
            pass

    if not suggested_pc:
        suggested_pc = "AA"
        reason = "no strong signal — fallback to AA (dominant stream)"

    return jsonify({
        "transaction_id": tx_id,
        "suggested_profit_center": suggested_pc,
        "suggested_person": suggested_person,
        "reason": reason,
    })


@card_audit_bp.route("/monthly-dashboard", methods=["GET"])
def card_audit_monthly_dashboard() -> Any:
    """End-of-month KPIs for the bookkeeper handover.

    Returns: tx_count, sum_in, sum_out, by_stream (with in/out + status),
    cases_investigation_count (= unmatched + suggested that haven't been
    resolved).
    """
    period = (request.args.get("period")
              or datetime.utcnow().strftime("%Y-%m"))
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM card_transactions WHERE period = ?",
            (period,)
        ).fetchall()
        txs = [db._row_to_dict(r) for r in rows]
    finally:
        conn.close()

    sum_in = sum(float(t.get("amount_eur") or t.get("amount") or 0)
                 for t in txs if (t.get("amount_eur") or 0) > 0)
    sum_out = sum(abs(float(t.get("amount_eur") or t.get("amount") or 0))
                  for t in txs if (t.get("amount_eur") or 0) < 0)
    matched = sum(1 for t in txs
                  if t.get("match_status") in ("auto", "manual"))
    pending = sum(1 for t in txs
                  if t.get("match_status") in ("unmatched", "suggested"))
    excluded = sum(1 for t in txs if t.get("match_status") == "excluded")

    by_stream: Dict[str, Dict[str, Any]] = {}
    for t in txs:
        pc = (t.get("profit_center") or "—").upper()
        amt = float(t.get("amount_eur") or t.get("amount") or 0)
        s = by_stream.setdefault(pc, {
            "profit_center": pc,
            "tx_count": 0, "sum_in": 0.0, "sum_out": 0.0,
            "matched": 0, "pending": 0,
        })
        s["tx_count"] += 1
        if amt > 0: s["sum_in"] += amt
        else:       s["sum_out"] += abs(amt)
        if t.get("match_status") in ("auto", "manual"): s["matched"] += 1
        elif t.get("match_status") in ("unmatched", "suggested"): s["pending"] += 1

    return jsonify({
        "period": period,
        "tx_count": len(txs),
        "sum_in": round(sum_in, 2),
        "sum_out": round(sum_out, 2),
        "net": round(sum_in - sum_out, 2),
        "matched_count": matched,
        "pending_count": pending,
        "excluded_count": excluded,
        "by_stream": sorted(by_stream.values(),
                            key=lambda x: -(x["sum_out"] + x["sum_in"])),
    })


# 2026-06-11 (Top-2 backlog #3) — Asana create-task integration. When
# ASANA_PAT is configured, this hits the Asana /tasks API and returns
# the created task's permalink. Otherwise returns 503 with a clear
# message + the rendered template so the caller can copy-paste manually.
@card_audit_bp.route("/chase-asana/<tx_id>", methods=["POST"])
def card_audit_chase_asana(tx_id: str) -> Any:
    """Create one Asana task for a specific unmatched transaction.

    Body:
      workspace_id  — required if no default workspace configured
      project_id    — optional
      assignee_gid  — optional
      task_title    — override the template-rendered title
      task_body     — override the template-rendered body

    The frontend usually pre-renders title+body via /chase-missing and
    passes them back here so this endpoint doesn't re-render. That keeps
    the bookkeeper's last-minute edits intact.
    """
    body = request.get_json(silent=True) or {}
    tx = card_audit.get_card_tx(tx_id)
    if not tx:
        return jsonify({"error": "Not found"}), 404
    title = (body.get("task_title") or "").strip()
    notes = (body.get("task_body") or "").strip()
    if not title or not notes:
        return jsonify({"error": "task_title and task_body are required"}), 400
    workspace_id = (body.get("workspace_id") or "").strip() or None
    project_id   = (body.get("project_id") or "").strip() or None
    assignee_gid = (body.get("assignee_gid") or "").strip() or None

    from services import asana_sync as asana_svc
    try:
        task = asana_svc.create_task(
            name=title, notes=notes,
            workspace_id=workspace_id, project_id=project_id,
            assignee_gid=assignee_gid,
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "ASANA_PAT not configured" in msg:
            return jsonify({
                "status": "not_configured",
                "error": msg,
                "hint": "Set ASANA_PAT secret on the Fly app to enable auto-create.",
            }), 503
        return jsonify({"status": "asana_error", "error": msg}), 502

    db.insert_audit_log("card_tx_" + tx_id, "asana_task_created", {
        "task_gid": task.get("gid"),
        "permalink": task.get("permalink_url"),
    })
    return jsonify({"status": "created",
                    "task_gid": task.get("gid"),
                    "permalink": task.get("permalink_url")})


@card_audit_bp.route("/chase-missing", methods=["POST"])
def card_audit_chase_missing() -> Any:
    """Generate chase tasks for every unmatched tx in the period.

    Body: { period: "YYYY-MM" }
    Returns: array of chase items — each with suggested_pc, suggested_person,
    rendered email/asana text. Frontend renders a copy-pasteable list +
    "Open Asana to create" link per item.

    We DON'T actually call Asana here (token may not be present) — the
    response is a template the bookkeeper can paste into Asana, Slack,
    or email. Asana auto-creation can be wired later when ASANA_PAT
    is configured.
    """
    body = request.get_json(silent=True) or {}
    period = (body.get("period")
              or request.args.get("period")
              or datetime.utcnow().strftime("%Y-%m"))

    unmatched_rows = card_audit.list_card_tx(period=period, match_status="unmatched")
    items = []
    for t in unmatched_rows:
        # Suggest owner — reuse the logic above via a direct call to keep
        # one implementation source.
        description = ((t.get("counterparty") or "") + " " +
                       (t.get("description") or "")).lower()
        suggested_pc = t.get("profit_center")
        suggested_person = t.get("card_holder") or ""
        reason = "from existing assignment"

        if not suggested_pc:
            try:
                vendor_match = bts.suggest_pc_for_vendor(description, "")
                if vendor_match:
                    suggested_pc = vendor_match.get("profit_center")
                    suggested_person = vendor_match.get("primary_contact") or suggested_person
                    reason = "vendor `" + (vendor_match.get("brand") or "") + "`"
            except Exception:  # noqa: BLE001 — defensive
                pass

        if not suggested_pc:
            suggested_pc = "AA"
            reason = "no strong signal — defaulted to AA"

        amount_eur = abs(float(t.get("amount_eur") or t.get("amount") or 0))
        # 2026-06-11 Top-2 P1 — templates editable via Admin tab.
        # 2026-06-22 #92 — chase routing: derive stakeholder per stream
        # from BT4YOU holding_config snapshot. New placeholders:
        # {stakeholder} {stakeholder_title} {stakeholder_asana_gid}
        from services import settings as _settings
        from services import chase_routing as _cr
        from services import pc_codes as _pcc
        stakeholder_info = _cr.stakeholder_for(suggested_pc) or {}
        title_tmpl = _settings.get("chase_task_title")
        body_tmpl  = _settings.get("chase_task_body")
        fmt_args = {
            "amount":       f"{amount_eur:.2f}",
            "vendor":       t.get("counterparty") or t.get("description") or "?",
            "date":         (t.get("posted_at") or "")[:10],
            "description":  t.get("description", "?"),
            "counterparty": t.get("counterparty", "?"),
            "reference":    t.get("reference", "—"),
            "source":       t.get("source", "?"),
            "pc":           suggested_pc,
            "pc_label":              _pcc.label_of(suggested_pc),
            "stakeholder":           stakeholder_info.get("name") or "—",
            "stakeholder_title":     stakeholder_info.get("title") or "",
            "stakeholder_asana_gid": stakeholder_info.get("asana_gid") or "",
            "reason":       reason,
        }
        try:
            task_title = title_tmpl.format(**fmt_args)
            task_body  = body_tmpl.format(**fmt_args)
        except (KeyError, IndexError) as exc:
            # Template references an unknown placeholder — fall back to defaults
            logger.warning("chase template format error: %s", exc)
            task_title = _settings.DEFAULTS["chase_task_title"].format(**fmt_args)
            task_body  = _settings.DEFAULTS["chase_task_body"].format(**fmt_args)

        items.append({
            "transaction_id": t.get("id"),
            "posted_at": t.get("posted_at"),
            "amount_eur": amount_eur,
            "description": t.get("description"),
            "counterparty": t.get("counterparty"),
            "suggested_profit_center": suggested_pc,
            "suggested_person": suggested_person,
            "reason": reason,
            "task_title": task_title,
            "task_body": task_body,
            # 2026-06-22 #92 — routed stakeholder per stream (BT4YOU snapshot)
            "stakeholder": stakeholder_info or None,
        })

    # Group by suggested_profit_center for easier batch chasing
    by_stream = {}
    for it in items:
        by_stream.setdefault(it["suggested_profit_center"], []).append(it)

    return jsonify({
        "period": period,
        "total_unmatched": len(items),
        "items": items,
        "by_stream": by_stream,
    })


# ───────────────────────────────────────────────────────────────────────
# 2026-06-09 Top-10 P1.3 — Past statement imports archive
# Lists every CSV/XLSX/PDF batch ever imported, with match counts +
# the filename so Rita can find a specific upload from weeks ago.
# ───────────────────────────────────────────────────────────────────────

@card_audit_bp.route("/batches", methods=["GET"])
def card_audit_batches() -> Any:
    """List past import batches with per-batch counts + match summary."""
    conn = db.get_connection()
    try:
        rows = conn.execute("""
            SELECT batch_id,
                   source,
                   MIN(imported_at)        AS imported_at,
                   MIN(imported_by)        AS imported_by,
                   COUNT(*)                AS row_count,
                   SUM(CASE WHEN match_status = 'auto'      THEN 1 ELSE 0 END) AS auto_count,
                   SUM(CASE WHEN match_status = 'manual'    THEN 1 ELSE 0 END) AS manual_count,
                   SUM(CASE WHEN match_status = 'suggested' THEN 1 ELSE 0 END) AS suggested_count,
                   SUM(CASE WHEN match_status = 'unmatched' THEN 1 ELSE 0 END) AS unmatched_count,
                   SUM(CASE WHEN match_status = 'excluded'  THEN 1 ELSE 0 END) AS excluded_count,
                   MIN(period)             AS first_period,
                   MAX(period)             AS last_period,
                   SUM(COALESCE(amount_eur, amount, 0))                       AS total_eur
            FROM card_transactions
            GROUP BY batch_id, source
            ORDER BY MIN(imported_at) DESC
            LIMIT 200
        """).fetchall()
        batches = [dict(r) for r in rows]
    finally:
        conn.close()
    return jsonify({"batches": batches, "count": len(batches)})


@card_audit_bp.route("/batches/<batch_id>/export", methods=["GET"])
def card_audit_batch_export(batch_id: str) -> Any:
    """Download one batch as CSV with match results column for handover."""
    period = request.args.get("period") or None
    conn = db.get_connection()
    try:
        sql = ("SELECT * FROM card_transactions WHERE batch_id = ? "
               "ORDER BY posted_at DESC, id")
        rows = conn.execute(sql, (batch_id,)).fetchall()
        txs = [db._row_to_dict(r) for r in rows]
    finally:
        conn.close()

    buf = _io.StringIO()
    buf.write("# Amitours Holding — FIO Card Audit batch export\n")
    buf.write(f"# Batch ID: {batch_id}\n")
    buf.write(f"# Generated at: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n")
    buf.write(f"# Row count: {len(txs)}\n\n")
    fields = ["id", "source", "posted_at", "period", "amount", "currency",
              "amount_eur", "description", "counterparty", "reference",
              "card_holder", "department", "profit_center",
              "match_status", "match_confidence", "matched_invoice_id",
              "match_reason", "notes"]
    w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for t in txs:
        w.writerow(t)
    fname = f"fio_card_audit_batch_{batch_id[:12]}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


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


@card_audit_bp.route("/non-matching/print", methods=["GET"])
def card_audit_print_non_matching() -> Any:
    """Print-friendly HTML page listing all unmatched transactions.

    Stakeholder uses this to chase down missing receipts. Browser's native
    Cmd+P → Save as PDF turns it into a PDF report.

    Query: ?period=YYYY-MM&profit_center=AA  (both optional)
    """
    period = (request.args.get("period") or datetime.utcnow().strftime("%Y-%m"))
    pc = (request.args.get("profit_center") or "").strip().upper() or None
    rows = card_audit.list_card_tx(period=period, match_status="unmatched", limit=5000)
    if pc:
        rows = [r for r in rows if (r.get("profit_center") or "").upper() == pc]

    total_eur = sum(float(r.get("amount_eur") or 0) for r in rows)
    pc_label = pc or "All streams"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    table_rows = []
    for r in rows:
        date_s = (r.get("posted_at") or "")[:10]
        amt = "{:,.2f}".format(float(r.get("amount") or 0))
        amt_eur = "{:,.2f}".format(float(r.get("amount_eur") or 0))
        desc = (r.get("description") or "")[:160]
        cpty = (r.get("counterparty") or "")[:120]
        ccy = (r.get("currency") or "EUR")
        table_rows.append(
            "<tr>"
            f"<td>{date_s}</td>"
            f"<td>{cpty}</td>"
            f"<td class='desc'>{desc}</td>"
            f"<td class='num'>{amt} {ccy}</td>"
            f"<td class='num'>€ {amt_eur}</td>"
            "<td class='action-cell'></td>"
            "</tr>"
        )

    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>FIO Card Audit — non-matching transactions</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; color: #111; margin: 24px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .meta { color: #555; font-size: 12px; margin-bottom: 18px; }
  .summary {
    background: #fef3c7; border: 1px solid #f59e0b; border-radius: 6px;
    padding: 10px 14px; margin-bottom: 16px; font-size: 13px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { border-bottom: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }
  th { background: #f1f5f9; }
  td.num { text-align: right; font-family: ui-monospace, Menlo, monospace; white-space: nowrap; }
  td.desc { max-width: 320px; }
  td.action-cell { width: 110px; }
  .empty { padding: 24px; text-align: center; color: #6b7280; }
  .print-controls { margin-bottom: 16px; }
  @media print {
    body { margin: 12mm; }
    .print-controls { display: none; }
    table { font-size: 10px; }
  }
</style>
</head><body>
<div class="print-controls">
  <button onclick="window.print()" style="padding:8px 14px;font-size:14px;background:#0ea5e9;color:#fff;border:0;border-radius:6px;cursor:pointer;">🖨️ Print / Save as PDF</button>
  <span style="margin-left:12px;color:#555;font-size:12px;">Cmd + P also works.</span>
</div>
<h1>FIO Card Audit — non-matching transactions</h1>
<div class="meta">Period: <strong>""" + period + """</strong> · Profit center: <strong>""" + pc_label + """</strong> · Generated at """ + now + """</div>
<div class="summary">
  <strong>""" + str(len(rows)) + """</strong> transaction(s) without a matched invoice · total <strong>€ """ + "{:,.2f}".format(total_eur) + """</strong>.
  Use the rightmost column to note where you found the receipt.
</div>
""" + (
        "<table><thead><tr><th>Date</th><th>Counterparty</th><th>Description</th><th>Amount</th><th>EUR</th><th>Receipt found at…</th></tr></thead><tbody>"
        + "\n".join(table_rows)
        + "</tbody></table>"
        if rows else
        "<div class='empty'>✅ All transactions in this period have a matching invoice.</div>"
    ) + """
</body></html>"""
    return Response(html, mimetype="text/html")
