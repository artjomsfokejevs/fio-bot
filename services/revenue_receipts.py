"""Receipts (incoming customer payments) for revenue_documents.

Mirrors `partial_payments` for the AP (expense) flow. A receipt is a
single inbound payment; we sum them to detect status transitions:

  total_received == 0          → status unchanged (sent / draft)
  0 < total_received < amount  → status becomes 'partially_paid'
  total_received >= amount     → status becomes 'paid'

Phase 1 keeps it dead simple — no FX, no overpayment refunds (we just
clamp to paid). Phase 3 will wire bank_statement_tx_id matching.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db
from services import revenue as _rev

logger = logging.getLogger(__name__)

__all__ = [
    "add_receipt",
    "list_receipts",
    "delete_receipt",
    "total_received",
    "remaining",
    "auto_transition_if_complete",
]


def _now() -> str:
    return datetime.utcnow().isoformat()


def add_receipt(revenue_doc_id: str, amount_eur: float, *,
                received_at: Optional[str] = None,
                method: Optional[str] = None,
                reference: Optional[str] = None,
                bank_statement_tx_id: Optional[str] = None,
                by: Optional[str] = None) -> Dict[str, Any]:
    if not revenue_doc_id:
        raise ValueError("revenue_doc_id required")
    try:
        amount_eur = float(amount_eur)
    except (TypeError, ValueError):
        raise ValueError("amount_eur must be a number")
    if amount_eur <= 0:
        raise ValueError("amount_eur must be > 0")
    doc = _rev.get_doc(revenue_doc_id)
    if not doc:
        raise ValueError(f"revenue doc {revenue_doc_id} not found")
    now = _now()
    received_at = received_at or now
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO revenue_receipts "
            "(revenue_doc_id, received_at, amount_eur, method, reference, "
            " bank_statement_tx_id, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (revenue_doc_id, received_at, amount_eur, method, reference,
             bank_statement_tx_id, now, by),
        )
        receipt_id = cur.lastrowid
        # Audit on the parent doc (so the full history reads in one query)
        conn.execute(
            "INSERT INTO revenue_audit (revenue_doc_id, action, details_json, actor, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (revenue_doc_id, "received_payment",
             f'{{"receipt_id": {receipt_id}, "amount_eur": {amount_eur}, "method": "{method or ""}"}}',
             by, now),
        )
        conn.commit()
    finally:
        conn.close()
    # Trigger status auto-transition after commit (own transaction)
    auto_transition_if_complete(revenue_doc_id, by=by)
    return {"id": receipt_id, "revenue_doc_id": revenue_doc_id,
            "amount_eur": amount_eur, "received_at": received_at}


def list_receipts(revenue_doc_id: str) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM revenue_receipts WHERE revenue_doc_id = ? "
            "ORDER BY received_at ASC, id ASC",
            (revenue_doc_id,),
        ).fetchall()]
    finally:
        conn.close()


def delete_receipt(receipt_id: int, *, by: Optional[str] = None) -> bool:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT revenue_doc_id, amount_eur FROM revenue_receipts WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        if not row:
            return False
        doc_id = row["revenue_doc_id"]
        conn.execute("DELETE FROM revenue_receipts WHERE id = ?", (receipt_id,))
        conn.execute(
            "INSERT INTO revenue_audit (revenue_doc_id, action, details_json, actor, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, "receipt_removed",
             f'{{"receipt_id": {receipt_id}, "amount_eur": {row["amount_eur"]}}}',
             by, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    # Re-evaluate status; could drop back from paid → partially_paid → sent
    _recompute_status(doc_id, by=by)
    return True


def total_received(revenue_doc_id: str) -> float:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_eur), 0) AS t FROM revenue_receipts "
            "WHERE revenue_doc_id = ?",
            (revenue_doc_id,),
        ).fetchone()
        return float(row["t"] or 0.0)
    finally:
        conn.close()


def remaining(revenue_doc_id: str) -> float:
    doc = _rev.get_doc(revenue_doc_id)
    if not doc:
        return 0.0
    target = float(doc.get("amount_eur") or doc.get("amount") or 0.0)
    return max(0.0, target - total_received(revenue_doc_id))


def auto_transition_if_complete(revenue_doc_id: str, *, by: Optional[str] = None) -> Optional[str]:
    """Bump status forward only — never demote here (delete_receipt handles demotion)."""
    doc = _rev.get_doc(revenue_doc_id)
    if not doc:
        return None
    if doc.get("status") in ("cancelled",):
        return doc.get("status")
    target = float(doc.get("amount_eur") or doc.get("amount") or 0.0)
    if target <= 0:
        return doc.get("status")
    received = total_received(revenue_doc_id)
    new_status = doc.get("status")
    if received >= target - 0.005:           # within half a cent
        new_status = "paid"
    elif received > 0 and doc.get("status") in ("draft", "sent"):
        new_status = "partially_paid"
    if new_status != doc.get("status"):
        _rev.update_status(revenue_doc_id, new_status, by=by or "system")
    return new_status


def _recompute_status(revenue_doc_id: str, *, by: Optional[str] = None) -> Optional[str]:
    """Full recompute — used on receipt deletion (can demote)."""
    doc = _rev.get_doc(revenue_doc_id)
    if not doc or doc.get("status") == "cancelled":
        return doc.get("status") if doc else None
    target = float(doc.get("amount_eur") or doc.get("amount") or 0.0)
    received = total_received(revenue_doc_id)
    if target <= 0:
        return doc.get("status")
    if received >= target - 0.005:
        new_status = "paid"
    elif received > 0:
        new_status = "partially_paid"
    else:
        # Fallback: if previously sent/partially_paid, stay 'sent'; if draft, stay draft.
        new_status = "sent" if doc.get("status") in ("partially_paid", "paid") else doc.get("status")
    if new_status != doc.get("status"):
        _rev.update_status(revenue_doc_id, new_status, by=by or "system")
    return new_status
