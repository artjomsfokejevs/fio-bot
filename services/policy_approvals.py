"""Accounting / CEO approvals of policy violations.

A violation_key is sha1(doc_id|policy_name|level|message)[:16] — stable across
re-classification of the same doc, so an approval survives reparse.

Added 2026-06-16 for Phase 1 P1.3.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "violation_key",
    "is_approved",
    "get_approval",
    "approve",
    "list_approvals",
    "delete_approval",
]


def violation_key(doc_id: Any, policy_name: str, level: str, message: str) -> str:
    raw = "|".join([str(doc_id or ""), policy_name or "", level or "", (message or "")[:120]])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def is_approved(key: str) -> bool:
    return get_approval(key) is not None


def get_approval(key: str) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM policy_violation_approvals WHERE violation_key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def approve(*, key: str, doc_id: Any, policy_name: str, level: str,
            message: str, approved_by: str, role: Optional[str] = None,
            reason: Optional[str] = None) -> Dict[str, Any]:
    if not key:
        raise ValueError("violation_key required")
    if not approved_by:
        raise ValueError("approved_by required")
    existing = get_approval(key)
    if existing:
        return existing  # idempotent
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO policy_violation_approvals "
            "(violation_key, doc_id, policy_name, level, message, "
            " approved_by, approved_at, role, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, str(doc_id) if doc_id else None, policy_name, level,
             message, approved_by, now, role, reason),
        )
        conn.commit()
    finally:
        conn.close()
    return get_approval(key)  # type: ignore[return-value]


def list_approvals(doc_id: Optional[Any] = None) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        if doc_id is None:
            rows = conn.execute(
                "SELECT * FROM policy_violation_approvals "
                "ORDER BY approved_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM policy_violation_approvals "
                "WHERE doc_id = ? ORDER BY approved_at DESC",
                (str(doc_id),),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_approval(approval_id: int) -> bool:
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM policy_violation_approvals WHERE id = ?",
            (approval_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
