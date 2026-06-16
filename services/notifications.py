"""In-app notification bell — Phase 2 (2026-06-16).

When CEO confirms a payment, Rita gets a notification without having
to refresh the queue. Foundation for future Phase 3 budget-alarm pings
and any "you have a thing to look at" signal.

Recipients model:
    recipient_user  — specific full_name (highest priority)
    recipient_role  — broadcast to everyone with this role
    NULL/NULL       — broadcast to everyone

A user fetches via `for_user(name, role)` which OR's together
(user=name) OR (role=user.role) OR (user IS NULL AND role IS NULL).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "create",
    "for_user",
    "unread_count",
    "mark_read",
    "mark_all_read",
]

_VALID_SEVERITY = {"info", "warning", "urgent"}


def create(*, kind: str, title: str, body: Optional[str] = None,
           recipient_role: Optional[str] = None,
           recipient_user: Optional[str] = None,
           doc_id: Optional[str] = None,
           href: Optional[str] = None,
           severity: str = "info",
           created_by: Optional[str] = None) -> Dict[str, Any]:
    if not kind or not title:
        raise ValueError("kind + title required")
    if severity not in _VALID_SEVERITY:
        raise ValueError("severity must be one of: %s" % sorted(_VALID_SEVERITY))
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO notifications "
            "(recipient_role, recipient_user, kind, title, body, doc_id, "
            " href, severity, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (recipient_role, recipient_user, kind, title, body, doc_id,
             href, severity, now, created_by),
        )
        nid = cur.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM notifications WHERE id = ?", (nid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def for_user(user_name: Optional[str], role: Optional[str], *,
             limit: int = 50, only_unread: bool = False) -> List[Dict[str, Any]]:
    """Return notifications visible to (user_name, role). Newest first."""
    where = ["( recipient_user = ? OR recipient_role = ? OR "
             "  (recipient_user IS NULL AND recipient_role IS NULL) )"]
    params: List[Any] = [user_name or "__none__", role or "__none__"]
    if only_unread:
        where.append("read_at IS NULL")
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE " + " AND ".join(where) +
            " ORDER BY created_at DESC LIMIT ?",
            tuple(params) + (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def unread_count(user_name: Optional[str], role: Optional[str]) -> int:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL "
            "AND ( recipient_user = ? OR recipient_role = ? OR "
            "      (recipient_user IS NULL AND recipient_role IS NULL) )",
            (user_name or "__none__", role or "__none__"),
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def mark_read(notif_id: int, *, by: Optional[str] = None) -> bool:
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE notifications SET read_at = ?, read_by = ? "
            "WHERE id = ? AND read_at IS NULL",
            (now, by, notif_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_all_read(user_name: Optional[str], role: Optional[str],
                  *, by: Optional[str] = None) -> int:
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE notifications SET read_at = ?, read_by = ? "
            "WHERE read_at IS NULL "
            "AND ( recipient_user = ? OR recipient_role = ? OR "
            "      (recipient_user IS NULL AND recipient_role IS NULL) )",
            (now, by, user_name or "__none__", role or "__none__"),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
