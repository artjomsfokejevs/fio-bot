"""Notifications + Slack + Bank-statement archive Blueprint — Phase 2.

Endpoints:
    GET    /api/notifications                 — for current user (role-aware)
    GET    /api/notifications/unread-count    — int for the bell badge
    POST   /api/notifications/<id>/read       — mark one read
    POST   /api/notifications/mark-all-read   — mark all visible read

    POST   /api/documents/<id>/send-ceo-urgent — flag invoice for CEO via Slack
    POST   /api/slack/test                    — admin: ping the configured webhook

    GET    /api/bank-statements/archives      — list past CSV imports
    POST   /api/bank-statements/archives/<batch_id>/recheck — re-run reconcile

Added 2026-06-16 Phase 2.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request, url_for

from services import db
from services import notifications as notif_svc
from services import slack_notify as slack_svc
from services import roles as roles_svc

logger = logging.getLogger(__name__)

notify_bp = Blueprint("notify", __name__, url_prefix="/api")


def _current_user_name():
    name = (request.headers.get("X-FIO-User") or "").strip()
    return name or None


def _current_role():
    return roles_svc.get_role(_current_user_name())


def _require_role(*allowed_roles: str):
    user = _current_user_name()
    role = roles_svc.get_role(user)
    if role not in allowed_roles:
        return jsonify({
            "error": "forbidden",
            "message": "Role '%s' is not allowed here. Required: %s" %
                       (role, list(allowed_roles)),
            "you": user, "your_role": role,
        }), 403
    return None


# 2026-06-26 (D2) — local capability gate (avoid circular import from app.py)
def _require_capability(cap: str):
    user = _current_user_name()
    if roles_svc.has_capability(user, cap):
        return None
    return jsonify({
        "error": "forbidden",
        "your_role": roles_svc.get_role(user),
        "missing_capability": cap,
        "message": "Capability '%s' required." % cap,
    }), 403


# ─────────────────────────────────────────────────────────────
# Notifications bell
# ─────────────────────────────────────────────────────────────

@notify_bp.route("/notifications", methods=["GET"])
def list_notifications() -> Any:
    only_unread = (request.args.get("only_unread") or "").lower() in ("1", "true", "yes")
    user = _current_user_name()
    role = _current_role()
    items = notif_svc.for_user(user, role, only_unread=only_unread, limit=50)
    return jsonify({
        "items": items,
        "unread_count": notif_svc.unread_count(user, role),
    })


@notify_bp.route("/notifications/unread-count", methods=["GET"])
def notifications_unread_count() -> Any:
    user = _current_user_name()
    role = _current_role()
    return jsonify({"unread_count": notif_svc.unread_count(user, role)})


@notify_bp.route("/notifications/<int:notif_id>/read", methods=["POST"])
def read_notification(notif_id: int) -> Any:
    # 2026-06-26 (D2) — self-service action, all roles have use_notifications by default
    err = _require_capability("use_notifications")
    if err:
        return err
    actor = _current_user_name() or "unknown"
    ok = notif_svc.mark_read(notif_id, by=actor)
    return jsonify({"status": "read" if ok else "already_read_or_missing"})


@notify_bp.route("/notifications/mark-all-read", methods=["POST"])
def mark_all_read() -> Any:
    err = _require_capability("use_notifications")
    if err:
        return err
    user = _current_user_name()
    role = _current_role()
    n = notif_svc.mark_all_read(user, role, by=user or "unknown")
    return jsonify({"marked": n})


# ─────────────────────────────────────────────────────────────
# Slack CEO urgent notification
# ─────────────────────────────────────────────────────────────

@notify_bp.route("/documents/<doc_id>/send-ceo-urgent", methods=["POST"])
def send_ceo_urgent(doc_id: str) -> Any:
    err = (_require_role("admin", "bookkeeper", "holding_ceo")
           or _require_capability("approve_budget"))
    if err:
        return err
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "doc not found"}), 404
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip() or None
    actor = _current_user_name() or "unknown"
    base = request.host_url.rstrip("/")
    res = slack_svc.send_urgent_payment(
        vendor=doc.get("vendor") or doc.get("original_name") or doc_id,
        amount=float(doc.get("amount") or 0),
        currency=doc.get("currency") or "EUR",
        doc_url=base + "/?doc=" + doc_id,
        reason=reason,
        flagged_by=actor,
        due_date=doc.get("desired_payment_date"),
    )
    # Always record in-app too so CEO sees the bell even without Slack.
    try:
        notif_svc.create(
            kind="urgent_payment",
            title="🚨 Urgent payment: " + (doc.get("vendor") or "—") +
                  " · %.2f %s" % (float(doc.get("amount") or 0), doc.get("currency") or "EUR"),
            body=reason,
            recipient_role="holding_ceo",
            doc_id=doc_id,
            href="/?doc=" + doc_id,
            severity="urgent",
            created_by=actor,
        )
    except Exception:  # noqa: BLE001
        logger.exception("in-app urgent notif create failed")
    try:
        db.insert_audit_log(doc_id, "ceo_urgent_flagged",
                            {"reason": reason, "slack_result": res.get("status")},
                            performed_by=actor)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({
        "slack": res,
        "in_app": "notification created",
        "configured": slack_svc.is_configured(),
    }), (200 if res.get("status") in ("sent", "not_configured") else 502)


@notify_bp.route("/slack/test", methods=["POST"])
def slack_test() -> Any:
    err = _require_role("admin", "holding_ceo") or _require_capability("manage_payees")
    if err:
        return err
    return jsonify({
        "configured": slack_svc.is_configured(),
        "result": slack_svc.send_ping(),
    })


@notify_bp.route("/slack/status", methods=["GET"])
def slack_status() -> Any:
    return jsonify({
        "configured": slack_svc.is_configured(),
        "transport": slack_svc.transport(),
    })


# ─────────────────────────────────────────────────────────────
# Bank statement archive — list past imports + recheck
# ─────────────────────────────────────────────────────────────

@notify_bp.route("/bank-statements/archives", methods=["GET"])
def bank_statement_archives() -> Any:
    """List past CSV imports as 'archives' so Rita can revisit a closed
    reconciliation after new invoices arrive."""
    conn = db.get_connection()
    try:
        # 2026-06-24 FB-K — surface uploader, period range, value sums, all status counts
        rows = conn.execute(
            "SELECT batch_id, source, MIN(imported_at) AS first_at, "
            "       MAX(imported_at) AS last_at, COUNT(*) AS tx_count, "
            "       MAX(COALESCE(imported_by, '—')) AS imported_by, "
            "       SUM(CASE WHEN match_status='matched' THEN 1 ELSE 0 END) AS matched, "
            "       SUM(CASE WHEN match_status='unmatched' THEN 1 ELSE 0 END) AS unmatched, "
            "       SUM(CASE WHEN match_status='suggested' THEN 1 ELSE 0 END) AS suggested, "
            "       SUM(CASE WHEN match_status='excluded' THEN 1 ELSE 0 END) AS excluded, "
            "       MIN(posted_at) AS period_start, MAX(posted_at) AS period_end, "
            "       MAX(period) AS period, "
            "       SUM(CASE WHEN amount > 0 THEN amount_eur ELSE 0 END) AS sum_in_eur, "
            "       SUM(CASE WHEN amount < 0 THEN amount_eur ELSE 0 END) AS sum_out_eur "
            "FROM card_transactions GROUP BY batch_id "
            "ORDER BY MAX(imported_at) DESC LIMIT 200"
        ).fetchall()
        return jsonify({"archives": [dict(r) for r in rows]})
    finally:
        conn.close()


@notify_bp.route("/bank-statements/archives/<batch_id>/transactions", methods=["GET"])
def archive_transactions(batch_id: str) -> Any:
    """List every transaction in a past CSV batch — feeds the Preview modal (#91).
    Also returns the batch metadata so the modal can show "this is a closed/
    confirmed batch, here is the snapshot at confirmation" — re-opens read-only (#89)."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, source, posted_at, period, amount, currency, amount_eur, "
            "       description, counterparty, reference, card_holder, profit_center, "
            "       match_status, match_confidence, matched_invoice_id, notes "
            "FROM card_transactions WHERE batch_id = ? "
            "ORDER BY posted_at DESC, id ASC LIMIT 1000",
            (batch_id,),
        ).fetchall()
        meta = conn.execute(
            "SELECT batch_id, MIN(imported_at) AS first_at, MAX(imported_at) AS last_at, "
            "       COUNT(*) AS tx_count, MAX(period) AS period, MAX(source) AS source "
            "FROM card_transactions WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not meta or meta["tx_count"] == 0:
            return jsonify({"error": "batch not found"}), 404
        return jsonify({
            "batch": dict(meta),
            "transactions": [dict(r) for r in rows],
        })
    finally:
        conn.close()


@notify_bp.route("/bank-statements/archives/<batch_id>/recheck", methods=["POST"])
def recheck_archive(batch_id: str) -> Any:
    """Re-run the matcher against the archived batch — useful after Rita
    uploads new invoices that didn't exist when the batch was first
    reconciled. Marks the batch's unmatched/suggested rows for a fresh
    look without disturbing already-confirmed matches."""
    err = _require_role("admin", "bookkeeper") or _require_capability("approve_budget")
    if err:
        return err
    # Lightweight implementation — flip suggested+unmatched rows back to
    # unmatched so the existing reconciliation flow picks them up on the
    # next /api/card-audit/reconcile call. The reconciler then re-evaluates
    # against the (now newer) documents corpus.
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE card_transactions SET match_status='unmatched', "
            "matched_invoice_id=NULL, match_confidence=0, match_reason=NULL "
            "WHERE batch_id = ? AND match_status IN ('unmatched', 'suggested')",
            (batch_id,),
        )
        conn.commit()
        affected = cur.rowcount
    finally:
        conn.close()
    actor = _current_user_name() or "unknown"
    try:
        db.insert_audit_log("batch:" + batch_id, "archive_recheck_requested",
                            {"batch_id": batch_id, "rows_reset": affected},
                            performed_by=actor)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({
        "batch_id": batch_id,
        "rows_reset_for_recheck": affected,
        "hint": "Now call /api/card-audit/reconcile to actually re-match.",
    })


# 2026-06-26 FB-K (C) — printable PDF report of one archived batch.
# Team feedback: "preview as PDF чтобы всё сразу было видно". This gives
# a 1-page header (uploader/period/totals/status counts) + per-tx table
# (date · counterparty · amount · status) — for handover or audit trail.
@notify_bp.route("/bank-statements/archives/<batch_id>/report.pdf", methods=["GET"])
def archive_pdf_report(batch_id: str) -> Any:
    from flask import Response as _Resp
    import io as _io

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        )
    except ImportError:
        return jsonify({
            "status": "not_configured",
            "error": "reportlab not installed",
            "hint": "Add 'reportlab>=4.0.0' to requirements.txt and redeploy.",
        }), 503

    conn = db.get_connection()
    try:
        meta_row = conn.execute(
            "SELECT batch_id, source, "
            "       MIN(imported_at) AS first_at, MAX(imported_at) AS last_at, "
            "       MAX(COALESCE(imported_by, '—')) AS imported_by, "
            "       COUNT(*) AS tx_count, "
            "       SUM(CASE WHEN match_status='matched' THEN 1 ELSE 0 END) AS matched, "
            "       SUM(CASE WHEN match_status='unmatched' THEN 1 ELSE 0 END) AS unmatched, "
            "       SUM(CASE WHEN match_status='suggested' THEN 1 ELSE 0 END) AS suggested, "
            "       SUM(CASE WHEN match_status='excluded' THEN 1 ELSE 0 END) AS excluded, "
            "       MIN(posted_at) AS period_start, MAX(posted_at) AS period_end, "
            "       SUM(CASE WHEN amount > 0 THEN amount_eur ELSE 0 END) AS sum_in_eur, "
            "       SUM(CASE WHEN amount < 0 THEN amount_eur ELSE 0 END) AS sum_out_eur "
            "FROM card_transactions WHERE batch_id = ? GROUP BY batch_id",
            (batch_id,),
        ).fetchone()
        if not meta_row or not meta_row["tx_count"]:
            return jsonify({"error": "batch not found"}), 404
        meta = dict(meta_row)
        tx_rows = conn.execute(
            "SELECT posted_at, amount_eur, currency, counterparty, description, "
            "       match_status, profit_center "
            "FROM card_transactions WHERE batch_id = ? "
            "ORDER BY posted_at ASC, id ASC LIMIT 500",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title=f"FIO Bank Statement · {batch_id[:8]}",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16,
                         textColor=colors.HexColor("#1f2937"))
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9,
                           textColor=colors.HexColor("#374151"))
    muted = ParagraphStyle("muted", parent=styles["Normal"], fontSize=8,
                            textColor=colors.HexColor("#6b7280"))
    flow: list = []
    flow.append(Paragraph("📊 Bank Statement Archive · FIO", h1))
    flow.append(Paragraph(
        f"Batch <font face='Courier'>{batch_id}</font> · "
        f"source <b>{meta.get('source') or '—'}</b>",
        body,
    ))
    flow.append(Spacer(1, 4 * mm))

    # Header KPI block as 2-column table
    period_start = (meta.get("period_start") or "")[:10]
    period_end = (meta.get("period_end") or "")[:10]
    period_label = f"{period_start} → {period_end}" if period_start and period_end and period_start != period_end else (period_start or "—")
    sum_in = float(meta.get("sum_in_eur") or 0)
    sum_out = float(meta.get("sum_out_eur") or 0)
    net = sum_in + sum_out
    header_table = Table([
        ["Uploaded by", meta.get("imported_by") or "—",
         "Period covered", period_label],
        ["Imported at", (meta.get("last_at") or "").replace("T", " ")[:16],
         "Transactions", str(meta.get("tx_count") or 0)],
        ["Money in (€)", f"{sum_in:,.2f}",
         "Money out (€)", f"{abs(sum_out):,.2f}"],
        ["Net (€)", f"{net:,.2f}",
         "Match status",
         f"✓ {meta.get('matched') or 0} · ~ {meta.get('suggested') or 0}"
         f" · ✗ {meta.get('unmatched') or 0}"
         f" · — {meta.get('excluded') or 0}"],
    ], colWidths=[35 * mm, 50 * mm, 35 * mm, 50 * mm])
    header_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#6b7280")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#6b7280")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f9fafb")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e7eb")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    flow.append(header_table)
    flow.append(Spacer(1, 5 * mm))

    # Per-tx table
    flow.append(Paragraph(
        f"<b>Transactions</b> ({len(tx_rows)} of {meta.get('tx_count') or 0})",
        body,
    ))
    flow.append(Spacer(1, 2 * mm))

    status_colors = {
        "matched":   colors.HexColor("#16a34a"),
        "suggested": colors.HexColor("#ca8a04"),
        "unmatched": colors.HexColor("#dc2626"),
        "excluded":  colors.HexColor("#94a3b8"),
        "manual":    colors.HexColor("#16a34a"),
    }
    table_rows = [["Date", "Counterparty / Description", "PC", "€ EUR", "Status"]]
    for r in tx_rows:
        date_s = (r["posted_at"] or "")[:10]
        cp = (r["counterparty"] or r["description"] or "?")[:60]
        amt = float(r["amount_eur"] or 0)
        amt_s = f"{amt:>12,.2f}"
        pc = r["profit_center"] or "—"
        st = (r["match_status"] or "?")[:9]
        table_rows.append([date_s, cp, pc, amt_s, st])

    tx_table = Table(table_rows, colWidths=[20 * mm, 90 * mm, 12 * mm, 28 * mm, 22 * mm], repeatRows=1)
    style_cmds = [
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("ALIGN", (4, 0), (4, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#9ca3af")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#fafafa")]),
    ]
    # Per-status colored last column
    for i, r in enumerate(tx_rows, start=1):
        c = status_colors.get(r["match_status"], colors.black)
        style_cmds.append(("TEXTCOLOR", (4, i), (4, i), c))
        if (r["amount_eur"] or 0) < 0:
            style_cmds.append(("TEXTCOLOR", (3, i), (3, i),
                               colors.HexColor("#dc2626")))
        else:
            style_cmds.append(("TEXTCOLOR", (3, i), (3, i),
                               colors.HexColor("#16a34a")))
    tx_table.setStyle(TableStyle(style_cmds))
    flow.append(tx_table)
    flow.append(Spacer(1, 4 * mm))
    flow.append(Paragraph(
        "Generated by FIO Accounting Bot · "
        f"{(meta.get('last_at') or '')[:19].replace('T',' ')} UTC",
        muted,
    ))

    doc.build(flow)
    buf.seek(0)
    fname = f"fio_bank_archive_{batch_id[:12]}.pdf"
    return _Resp(buf.getvalue(), mimetype="application/pdf",
                 headers={"Content-Disposition": f'inline; filename="{fname}"'})
