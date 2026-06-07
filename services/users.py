"""FIO users roster — managed in the Admin tab by HR / Bookkeeper.

Source of truth for the "Who is uploading?" dropdown in Upload tab and any
other place that needs a list of valid people. Auth itself stays on the
Basic Auth middleware in app.py — this module is a domain roster only.

Migrated in: 2026-06-07 (FIO testers feedback, Top-16 P1).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "list_users", "get_user", "create_user", "update_user", "delete_user",
    "ROLES", "VALID_ROLES",
]

# Role enum — kept narrow on purpose. New roles must be added explicitly here
# and to the Admin tab UI; we do not accept arbitrary role strings.
ROLES: Dict[str, str] = {
    "uploader":     "Uploader",
    "approver":     "Approver",
    "bookkeeper":   "Bookkeeper",
    "hr":           "HR",
    "holding_ceo":  "Holding CEO",
    "stream_owner": "Stream owner",
    "admin":        "Admin",
}
VALID_ROLES = frozenset(ROLES.keys())


def list_users(active_only: bool = False, role: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all users, optionally filtered by active flag / role."""
    conn = db.get_connection()
    try:
        sql = "SELECT * FROM fio_users WHERE 1=1"
        params: List[Any] = []
        if active_only:
            sql += " AND active = 1"
        if role:
            sql += " AND role = ?"
            params.append(role)
        sql += " ORDER BY full_name COLLATE NOCASE"
        rows = conn.execute(sql, params).fetchall()
        return [db._row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM fio_users WHERE id = ?", (user_id,)).fetchone()
        return db._row_to_dict(row) if row else None
    finally:
        conn.close()


def create_user(payload: Dict[str, Any], created_by: str = "admin") -> Dict[str, Any]:
    """Insert a new user. Validates role + non-empty full_name.

    Raises ValueError on bad input, sqlite3.IntegrityError on duplicate name.
    """
    full_name = (payload.get("full_name") or "").strip()
    if not full_name:
        raise ValueError("full_name is required")
    role = (payload.get("role") or "uploader").strip()
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}; got {role!r}")

    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO fio_users
                 (full_name, email, role, profit_center, department,
                  active, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                full_name,
                (payload.get("email") or "").strip() or None,
                role,
                (payload.get("profit_center") or "").strip() or None,
                (payload.get("department") or "").strip() or None,
                1 if payload.get("active", True) else 0,
                now,
                created_by,
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        logger.info("Created fio_user %s (id=%s, role=%s)", full_name, new_id, role)
    finally:
        conn.close()
    return get_user(new_id)  # type: ignore[arg-type]


def update_user(user_id: int, patch: Dict[str, Any], updated_by: str = "admin") -> Optional[Dict[str, Any]]:
    """Partial update. Only known columns are accepted; unknown keys ignored."""
    allowed = {"full_name", "email", "role", "profit_center", "department", "active"}
    fields = {k: v for k, v in patch.items() if k in allowed}
    if not fields:
        return get_user(user_id)
    if "role" in fields and fields["role"] not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [datetime.utcnow().isoformat(), updated_by, user_id]
    conn = db.get_connection()
    try:
        conn.execute(
            f"UPDATE fio_users SET {set_clause}, updated_at = ?, updated_by = ? WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return get_user(user_id)


def delete_user(user_id: int) -> bool:
    """Soft-delete: set active=0. Hard delete loses uploader history."""
    return update_user(user_id, {"active": False}, updated_by="admin") is not None
