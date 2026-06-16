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
    actor = _current_user_name() or "unknown"
    ok = notif_svc.mark_read(notif_id, by=actor)
    return jsonify({"status": "read" if ok else "already_read_or_missing"})


@notify_bp.route("/notifications/mark-all-read", methods=["POST"])
def mark_all_read() -> Any:
    user = _current_user_name()
    role = _current_role()
    n = notif_svc.mark_all_read(user, role, by=user or "unknown")
    return jsonify({"marked": n})


# ─────────────────────────────────────────────────────────────
# Slack CEO urgent notification
# ─────────────────────────────────────────────────────────────

@notify_bp.route("/documents/<doc_id>/send-ceo-urgent", methods=["POST"])
def send_ceo_urgent(doc_id: str) -> Any:
    err = _require_role("admin", "bookkeeper", "holding_ceo")
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
    err = _require_role("admin", "holding_ceo")
    if err:
        return err
    return jsonify({
        "configured": slack_svc.is_configured(),
        "result": slack_svc.send_ping(),
    })


@notify_bp.route("/slack/status", methods=["GET"])
def slack_status() -> Any:
    return jsonify({"configured": slack_svc.is_configured()})


# ─────────────────────────────────────────────────────────────
# Bank statement archive — list past imports + recheck
# ─────────────────────────────────────────────────────────────

@notify_bp.route("/bank-statements/archives", methods=["GET"])
def bank_statement_archives() -> Any:
    """List past CSV imports as 'archives' so Rita can revisit a closed
    reconciliation after new invoices arrive."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT batch_id, source, MIN(imported_at) AS first_at, "
            "       MAX(imported_at) AS last_at, COUNT(*) AS tx_count, "
            "       SUM(CASE WHEN match_status='matched' THEN 1 ELSE 0 END) AS matched, "
            "       SUM(CASE WHEN match_status='unmatched' THEN 1 ELSE 0 END) AS unmatched, "
            "       SUM(CASE WHEN match_status='suggested' THEN 1 ELSE 0 END) AS suggested, "
            "       MAX(period) AS period "
            "FROM card_transactions GROUP BY batch_id "
            "ORDER BY MAX(imported_at) DESC LIMIT 200"
        ).fetchall()
        return jsonify({"archives": [dict(r) for r in rows]})
    finally:
        conn.close()


@notify_bp.route("/bank-statements/archives/<batch_id>/recheck", methods=["POST"])
def recheck_archive(batch_id: str) -> Any:
    """Re-run the matcher against the archived batch — useful after Rita
    uploads new invoices that didn't exist when the batch was first
    reconciled. Marks the batch's unmatched/suggested rows for a fresh
    look without disturbing already-confirmed matches."""
    err = _require_role("admin", "bookkeeper")
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
