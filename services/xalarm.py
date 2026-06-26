"""X-alarm — Phase 3 (2026-06-16).

When a stream's actuals exceed its CEO-agreed budget for a period,
this fires: email to CEO+Artjoms+Rita+stream-owner + in-app
notification + optional Asana auto-task on stream owner.

Dedup: one row per (pc, period) per 24 hours. Subsequent triggers
update the existing row's actual_eur instead of sending duplicates.

P85 graceful: email send + Asana task degrade independently. The
in-app notification ALWAYS fires.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from services import db
from services import stream_budgets as sb
from services import notifications as notif
from services import email_send

logger = logging.getLogger(__name__)

__all__ = [
    "fire_if_overrun",
    "fire_if_low_runway",
    "list_log",
    "acknowledge",
    "recipients_for",
]

# Runway alarm threshold default — Phase 3 G-xalarm (2026-06-26).
# "Under 90 days" ≈ 13 weeks; CFO escalation per Financial Governance SOP §5.3.
RUNWAY_PERIOD_KEY = "runway"
RUNWAY_DEFAULT_THRESHOLD_WEEKS = 13

PC_LABELS = {
    # 2026-06-22 #87 — canonical codes per BT4YOU ledger (services/pc_codes.py).
    # AH now canonically means Amitours Holding OÜ (not Mountly).
    # MN = Mountly (was MT/AH historically). MT = Medical Travel (new).
    # SP = Skipasser (was SR). Legacy codes are auto-translated by pc_codes.label_of().
    "AA": "Alps2Alps", "SP": "Skipasser",
    "MN": "Mountly",   "MT": "Medical Travel",
    "CF": "MyPeak Finance", "AL": "ALVEDA",
    "AH": "Amitours Holding OÜ",
    # Legacy aliases kept for backwards-compat label rendering:
    "SR": "Skipasser (legacy SR)", "BK": "Skibookers (decommissioned)",
}


def recipients_for(pc: str) -> List[str]:
    """Compute the X-alarm recipient list:
       CEO holding + Artjoms (ops) + Rita (bookkeeper) + stream owner email."""
    out: List[str] = []
    ceo = (os.getenv("XALARM_CEO_EMAIL") or "").strip()
    if ceo:
        out.append(ceo)
    ops = (os.getenv("XALARM_OPS_EMAIL") or "artjoms.fokejevs@gmail.com").strip()
    if ops:
        out.append(ops)
    # Rita: first active bookkeeper from fio_users with email
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT email FROM fio_users "
                "WHERE role='bookkeeper' AND active=1 AND email IS NOT NULL "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if row and row["email"]:
                out.append(row["email"].strip())
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("rita lookup failed")
    # Stream owner email — from fio_users where profit_center matches.
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT email FROM fio_users "
                "WHERE profit_center=? AND active=1 AND email IS NOT NULL "
                "AND role IN ('stream_owner','admin') "
                "ORDER BY id LIMIT 1",
                (pc,),
            ).fetchone()
            if row and row["email"]:
                out.append(row["email"].strip())
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("stream owner lookup failed for pc=%s", pc)
    # Dedup case-insensitive
    seen = set(); deduped = []
    for e in out:
        k = e.lower()
        if k not in seen:
            seen.add(k); deduped.append(e)
    return deduped


def _build_email_body(*, pc: str, period: str, status: Dict[str, Any],
                      trigger_doc: Optional[Dict[str, Any]] = None,
                      actor: Optional[str] = None,
                      action: Optional[str] = None,
                      history_rows: Optional[List[Dict[str, Any]]] = None) -> str:
    pc_name = PC_LABELS.get(pc, pc)
    lines: List[str] = []
    lines.append("X-ALARM: %s stream %s spent %.2f EUR of %.2f EUR budget"
                 % (pc_name, period, status["actual_eur"], status["budget_eur"]))
    lines.append("Overrun: %.2f EUR (+%.1f%%)" % (status["overrun_eur"], status["overrun_pct"]))
    lines.append("")
    if trigger_doc:
        lines.append("Triggering invoice: %s — %.2f %s" % (
            trigger_doc.get("vendor") or "—",
            float(trigger_doc.get("amount") or 0),
            trigger_doc.get("currency") or "EUR",
        ))
        lines.append("Doc id: %s" % trigger_doc.get("id"))
    if actor:
        lines.append("Triggered by: %s%s" % (actor, (" via " + action) if action else ""))
    lines.append("")
    lines.append("Per protocol:")
    lines.append("  1. Stream owner: schedule a meeting with Holding CEO within 48h.")
    lines.append("     Either renegotiate the budget upward with reasons OR commit to cost cuts.")
    lines.append("  2. Until that conversation: no further invoices > 5,000 EUR approved in this stream.")
    lines.append("  3. Rita: flag any new %s invoices > 5,000 EUR as 'on_hold' pending CEO sign-off."
                 % pc_name)
    if history_rows:
        lines.append("")
        lines.append("Recent budget history for %s:" % pc_name)
        for h in history_rows[:5]:
            lines.append("  %s — by %s: %.2f -> %.2f%s" % (
                (h.get("changed_at") or "")[:16].replace("T", " "),
                h.get("changed_by") or "?",
                float(h.get("old_eur") or 0),
                float(h.get("new_eur") or 0),
                (" (" + h.get("reason") + ")") if h.get("reason") else "",
            ))
    lines.append("")
    lines.append("— FIO Accounting Bot (auto-generated X-alarm)")
    return "\n".join(lines)


def _recent_unack_for(pc: str, period: str, hours: int = 24) -> Optional[Dict[str, Any]]:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM xalarm_log "
            "WHERE profit_center=? AND period=? AND triggered_at >= ? "
            "AND acknowledged_at IS NULL "
            "ORDER BY triggered_at DESC LIMIT 1",
            (pc, period, cutoff),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _upsert_log(*, existing: Optional[Dict[str, Any]],
                pc: str, period: str, status: Dict[str, Any],
                trigger_doc_id: Optional[str], recipients: List[str],
                email_status: str, asana_task_url: Optional[str] = None) -> int:
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        if existing:
            conn.execute(
                "UPDATE xalarm_log SET actual_eur=?, overrun_eur=?, "
                "overrun_pct=?, email_status=? WHERE id=?",
                (status["actual_eur"], status["overrun_eur"],
                 status["overrun_pct"], email_status, existing["id"]),
            )
            xid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO xalarm_log "
                "(triggered_at, profit_center, period, budget_eur, actual_eur, "
                " overrun_eur, overrun_pct, trigger_doc_id, recipients_json, "
                " email_status, asana_task_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, pc, period, status["budget_eur"], status["actual_eur"],
                 status["overrun_eur"], status["overrun_pct"], trigger_doc_id,
                 json.dumps(recipients), email_status, asana_task_url),
            )
            xid = cur.lastrowid
        conn.commit()
        return xid
    finally:
        conn.close()


def fire_if_overrun(*, doc_id: str, triggering_action: str,
                    actor: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Check whether the doc's (pc, period) is now over budget and, if so,
    send the X-alarm (with 24h dedup). Best-effort — never raises."""
    try:
        doc = db.get_document(doc_id)
        if not doc:
            return None
        pc = doc.get("profit_center")
        period = doc.get("period")
        if not pc or not period:
            return None
        status = sb.is_over(pc, period)
        if not status["over"]:
            return None

        # Dedup window
        existing = _recent_unack_for(pc, period)
        recipients = recipients_for(pc)

        # Build + send email (P85 graceful)
        subject = "X-ALARM: %s stream %s over budget by %.2f EUR (+%.1f%%)" % (
            PC_LABELS.get(pc, pc), period, status["overrun_eur"], status["overrun_pct"])
        body = _build_email_body(
            pc=pc, period=period, status=status, trigger_doc=doc,
            actor=actor, action=triggering_action,
            history_rows=sb.history_for(pc=pc, period=period, limit=5),
        )
        email_res = email_send.send(to=recipients, subject=subject, body_text=body)
        email_status = str(email_res.get("status") or "unknown")

        # Asana auto-task (best-effort)
        asana_url = None
        try:
            from services import asana_sync as _asn
            res = _asn.create_task(
                name=subject,
                notes=body + ("\n\nDoc URL: /?doc=" + doc_id),
            )
            if isinstance(res, dict):
                asana_url = res.get("permalink_url") or res.get("url")
        except Exception:  # noqa: BLE001 — graceful when ASANA_PAT absent
            logger.debug("xalarm asana task skipped (likely not configured)")

        xid = _upsert_log(
            existing=existing, pc=pc, period=period, status=status,
            trigger_doc_id=doc_id, recipients=recipients,
            email_status=email_status, asana_task_url=asana_url,
        )

        # In-app notification (always)
        try:
            notif.create(
                kind="budget_alarm",
                title="🚨 X-alarm: %s %s — €%.2f over (%.1f%%)" % (
                    PC_LABELS.get(pc, pc), period,
                    status["overrun_eur"], status["overrun_pct"]),
                body=("Triggered by %s on doc %s. Email %s. See Stream Budgets."
                      % (actor or "system", doc_id, email_status)),
                recipient_role="admin",  # CEO + Artjoms have admin/holding_ceo
                doc_id=doc_id,
                href="/?xalarm=" + str(xid),
                severity="urgent",
                created_by=actor or "system",
            )
            notif.create(
                kind="budget_alarm",
                title="🚨 X-alarm: %s %s — €%.2f over (%.1f%%)" % (
                    PC_LABELS.get(pc, pc), period,
                    status["overrun_eur"], status["overrun_pct"]),
                body=("Triggered by %s on doc %s. Email %s." %
                      (actor or "system", doc_id, email_status)),
                recipient_role="bookkeeper",
                doc_id=doc_id,
                href="/?xalarm=" + str(xid),
                severity="urgent",
                created_by=actor or "system",
            )
        except Exception:  # noqa: BLE001
            logger.exception("xalarm in-app notif create failed")

        # Audit log
        try:
            db.insert_audit_log(doc_id, "xalarm_fired", {
                "pc": pc, "period": period, "overrun_eur": status["overrun_eur"],
                "email_status": email_status, "dedup_hit": bool(existing),
                "recipients_count": len(recipients),
            }, performed_by=actor or "system")
        except Exception:  # noqa: BLE001
            pass

        return {
            "id": xid, "pc": pc, "period": period,
            "overrun_eur": status["overrun_eur"],
            "overrun_pct": status["overrun_pct"],
            "email_status": email_status,
            "dedup_hit": bool(existing),
            "recipients": recipients,
            "asana_task_url": asana_url,
        }
    except Exception:  # noqa: BLE001 — must never break the caller
        logger.exception("fire_if_overrun failed for doc=%s", doc_id)
        return None


def list_log(pc: Optional[str] = None, period: Optional[str] = None,
             limit: int = 50, only_unack: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM xalarm_log"
    where = []
    params: List[Any] = []
    if pc:
        where.append("profit_center = ?"); params.append(pc)
    if period:
        where.append("period = ?"); params.append(period)
    if only_unack:
        where.append("acknowledged_at IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY triggered_at DESC LIMIT ?"
    params.append(limit)
    conn = db.get_connection()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("recipients_json"):
                try:
                    d["recipients"] = json.loads(d["recipients_json"])
                except Exception:
                    d["recipients"] = []
            out.append(d)
        return out
    finally:
        conn.close()


def _runway_recipients() -> List[str]:
    """Recipients for runway alarms — CEO + Artjoms + Rita (no stream owner,
    runway is consolidated)."""
    out: List[str] = []
    ceo = (os.getenv("XALARM_CEO_EMAIL") or "").strip()
    if ceo:
        out.append(ceo)
    ops = (os.getenv("XALARM_OPS_EMAIL") or "artjoms.fokejevs@gmail.com").strip()
    if ops:
        out.append(ops)
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT email FROM fio_users WHERE role='bookkeeper' AND active=1 "
                "AND email IS NOT NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if row and row["email"]:
                out.append(row["email"].strip())
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("rita lookup failed (runway)")
    seen = set(); deduped = []
    for e in out:
        k = e.lower()
        if k not in seen:
            seen.add(k); deduped.append(e)
    return deduped


def fire_if_low_runway(*, threshold_weeks: int = RUNWAY_DEFAULT_THRESHOLD_WEEKS,
                       pc: Optional[str] = None,
                       actor: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Project 13-week cashflow; if runway_weeks <= threshold, fire the alarm.

    Reuses xalarm_log with period='runway' so dedup window (24h) prevents
    spam. budget_eur stores the threshold, actual_eur stores the observed
    runway (or threshold+1 sentinel if positive but very close).

    Best-effort — never raises. Returns the alarm payload or None when
    runway is healthy / projection has no data.
    """
    try:
        from services import cashflow_projection as cf
    except Exception:  # noqa: BLE001
        logger.exception("cashflow_projection import failed")
        return None
    try:
        proj = cf.project(weeks=max(threshold_weeks, 13), pc=pc)
    except Exception:  # noqa: BLE001
        logger.exception("cashflow projection failed for pc=%s", pc)
        return None

    runway = proj.get("runway_weeks")
    ending = float(proj.get("ending_balance_eur") or 0)
    opening = float(proj.get("opening_balance_eur") or 0)
    pc_label = pc or "ALL"

    # Healthy: runway None (stays positive) OR runway above threshold
    if runway is None or runway > threshold_weeks:
        return None
    # If we have zero opening balance AND zero projected cash movement,
    # the projection is meaningless — skip rather than firing on noise.
    if opening == 0 and not any((w.get("ar_in") or 0) + (w.get("ap_out") or 0)
                                 for w in proj.get("series") or []):
        return None

    period = RUNWAY_PERIOD_KEY
    pc_key = pc_label

    existing = _recent_unack_for(pc_key, period)
    recipients = _runway_recipients()

    overrun_weeks = max(0, threshold_weeks - runway)
    overrun_pct = (overrun_weeks / threshold_weeks * 100.0) if threshold_weeks else 0.0
    status = {
        "budget_eur": float(threshold_weeks),  # semantic: threshold
        "actual_eur": float(runway),           # semantic: observed runway
        "overrun_eur": float(overrun_weeks),
        "overrun_pct": round(overrun_pct, 2),
    }

    subject = ("X-ALARM: %s runway %d week%s — below %d-week threshold (ending balance %.2f EUR)"
               % (pc_label, runway, "" if runway == 1 else "s",
                  threshold_weeks, ending))
    body_lines = [
        subject,
        "",
        "13-week cashflow projection shows the running bank balance",
        "going negative in week %d (threshold: under %d weeks)." % (runway, threshold_weeks),
        "",
        "Opening balance: %.2f EUR" % opening,
        "Ending balance (wk 13): %.2f EUR" % ending,
        "",
        "Per Financial Governance SOP §5.3:",
        "  1. CFO + CEO review runway within 24h.",
        "  2. Trigger AR collection sprint on top-10 outstanding invoices.",
        "  3. Defer non-critical AP > 5,000 EUR until runway > %d weeks." % threshold_weeks,
        "",
        "See Analytics → 📈 13-week cashflow projection for the breakdown.",
        "",
        "— FIO Accounting Bot (auto-generated runway alarm)",
    ]
    body = "\n".join(body_lines)

    email_res = email_send.send(to=recipients, subject=subject, body_text=body) \
        if recipients else {"status": "no-recipients"}
    email_status = str(email_res.get("status") or "unknown")

    asana_url = None
    try:
        from services import asana_sync as _asn
        res = _asn.create_task(name=subject, notes=body)
        if isinstance(res, dict):
            asana_url = res.get("permalink_url") or res.get("url")
    except Exception:  # noqa: BLE001
        logger.debug("runway xalarm asana task skipped")

    xid = _upsert_log(
        existing=existing, pc=pc_key, period=period, status=status,
        trigger_doc_id=None, recipients=recipients,
        email_status=email_status, asana_task_url=asana_url,
    )

    try:
        notif.create(
            kind="runway_alarm",
            title="🚨 Runway alarm: %s — %d wk left (threshold %d)" % (
                pc_label, runway, threshold_weeks),
            body=("Ending balance %.2f EUR. Email %s." % (ending, email_status)),
            recipient_role="admin",
            href="/?xalarm=" + str(xid),
            severity="urgent",
            created_by=actor or "system",
        )
        notif.create(
            kind="runway_alarm",
            title="🚨 Runway alarm: %s — %d wk left" % (pc_label, runway),
            body=("Ending balance %.2f EUR." % ending),
            recipient_role="bookkeeper",
            href="/?xalarm=" + str(xid),
            severity="urgent",
            created_by=actor or "system",
        )
    except Exception:  # noqa: BLE001
        logger.exception("runway xalarm in-app notif failed")

    return {
        "id": xid, "pc": pc_key, "period": period,
        "runway_weeks": runway, "threshold_weeks": threshold_weeks,
        "ending_balance_eur": ending, "opening_balance_eur": opening,
        "email_status": email_status, "dedup_hit": bool(existing),
        "recipients": recipients, "asana_task_url": asana_url,
    }


def acknowledge(xid: int, *, by: Optional[str] = None) -> bool:
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE xalarm_log SET acknowledged_at=?, acknowledged_by=? "
            "WHERE id=? AND acknowledged_at IS NULL",
            (now, by, xid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
