"""Paying accounts roster — managed in Admin tab by Admin / Bookkeeper.

Single source of truth for the "Paying account / bank" dropdown in the
Mark Paid modal. Before this, the modal used a free-text prompt and
labels drifted ('Revolut Amitours', 'Revolut · Amitours London', 'Rev AL').

Added in: 2026-06-08 (FIO testers feedback, Top-7 P2.3).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = ["list_accounts", "get_account", "create_account",
           "update_account", "delete_account"]


def list_accounts(active_only: bool = False,
                  legal_entity: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return accounts, optionally filtered."""
    conn = db.get_connection()
    try:
        sql = "SELECT * FROM paying_accounts WHERE 1=1"
        params: List[Any] = []
        if active_only:
            sql += " AND active = 1"
        if legal_entity:
            sql += " AND legal_entity = ?"
            params.append(legal_entity)
        sql += " ORDER BY active DESC, label COLLATE NOCASE"
        rows = conn.execute(sql, params).fetchall()
        return [db._row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_account(account_id: int) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        row = conn.execute("SELECT * FROM paying_accounts WHERE id = ?",
                           (account_id,)).fetchone()
        return db._row_to_dict(row) if row else None
    finally:
        conn.close()


def create_account(payload: Dict[str, Any],
                   created_by: str = "admin") -> Dict[str, Any]:
    """Insert a new paying account.

    Raises ValueError on missing/empty label,
    sqlite3.IntegrityError on duplicate label.
    """
    label = (payload.get("label") or "").strip()
    if not label:
        raise ValueError("label is required")

    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO paying_accounts
                 (label, bank, iban, account_number, currency, legal_entity,
                  notes, active, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                label,
                (payload.get("bank") or "").strip() or None,
                (payload.get("iban") or "").strip() or None,
                (payload.get("account_number") or "").strip() or None,
                (payload.get("currency") or "EUR").strip().upper(),
                (payload.get("legal_entity") or "").strip() or None,
                (payload.get("notes") or "").strip() or None,
                1 if payload.get("active", True) else 0,
                now,
                created_by,
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        logger.info("Created paying_account %s (id=%s)", label, new_id)
    finally:
        conn.close()
    return get_account(new_id)  # type: ignore[arg-type]


def update_account(account_id: int, patch: Dict[str, Any],
                   updated_by: str = "admin") -> Optional[Dict[str, Any]]:
    """Partial update. Only known columns accepted."""
    allowed = {"label", "bank", "iban", "account_number", "currency",
               "legal_entity", "notes", "active"}
    fields = {k: v for k, v in patch.items() if k in allowed}
    if not fields:
        return get_account(account_id)
    if "active" in fields:
        fields["active"] = 1 if fields["active"] else 0
    if "currency" in fields and fields["currency"]:
        fields["currency"] = fields["currency"].strip().upper()

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [datetime.utcnow().isoformat(),
                                      updated_by, account_id]
    conn = db.get_connection()
    try:
        conn.execute(
            f"UPDATE paying_accounts SET {set_clause}, "
            f"updated_at = ?, updated_by = ? WHERE id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()
    return get_account(account_id)


def delete_account(account_id: int) -> bool:
    """Soft-delete (active=0) to preserve audit-log integrity."""
    return update_account(account_id, {"active": False},
                          updated_by="admin") is not None
