"""Partial payments on internal invoices (P1.5, 2026-06-16).

Internal invoices (issued by one of our own legal entities to another
of our legal entities) are frequently paid in instalments rather than
in one wire. Bookkeeper tracks each instalment via this table; when the
running total reaches the invoice total, the document auto-transitions
to status='paid' (and the audit log records the auto-transition).

Gated by documents.is_internal=1 on the API and UI layers. Service
itself is policy-agnostic — it'll record a partial on any doc_id.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "list_for_doc",
    "add",
    "delete",
    "total_paid",
    "remaining",
    "auto_transition_if_complete",
]

_VALID_METHODS = {"bank_transfer", "card", "cash", "netting", "other"}


def list_for_doc(doc_id: str) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM partial_payments WHERE doc_id = ? ORDER BY paid_at ASC, id ASC",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def total_paid(doc_id: str) -> float:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_eur), 0) AS t FROM partial_payments WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        return float(row["t"] or 0.0)
    finally:
        conn.close()


def remaining(doc_id: str) -> float:
    doc = db.get_document(doc_id)
    if not doc:
        return 0.0
    invoice_total = float(doc.get("amount") or 0.0)
    return max(0.0, invoice_total - total_paid(doc_id))


def add(*, doc_id: str, amount_eur: float, paid_at: str,
        method: Optional[str] = None, reference: Optional[str] = None,
        by: Optional[str] = None) -> Dict[str, Any]:
    """Insert one partial-payment row. Returns the inserted row dict."""
    if not doc_id:
        raise ValueError("doc_id required")
    try:
        amount_eur = float(amount_eur)
    except (TypeError, ValueError):
        raise ValueError("amount_eur must be a number")
    if amount_eur <= 0:
        raise ValueError("amount_eur must be > 0")
    if not paid_at:
        raise ValueError("paid_at (YYYY-MM-DD) required")
    if method and method not in _VALID_METHODS:
        raise ValueError("method must be one of: %s" % sorted(_VALID_METHODS))

    doc = db.get_document(doc_id)
    if not doc:
        raise ValueError("doc %s not found" % doc_id)

    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO partial_payments "
            "(doc_id, amount_eur, paid_at, method, reference, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, amount_eur, paid_at, method, reference, now, by),
        )
        pid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    auto_transition_if_complete(doc_id, by=by)

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM partial_payments WHERE id = ?", (pid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def delete(partial_id: int) -> bool:
    conn = db.get_connection()
    try:
        # Fetch doc_id first so we can recompute on delete
        row = conn.execute(
            "SELECT doc_id FROM partial_payments WHERE id = ?", (partial_id,)
        ).fetchone()
        if not row:
            return False
        doc_id = row["doc_id"]
        conn.execute("DELETE FROM partial_payments WHERE id = ?", (partial_id,))
        conn.commit()
    finally:
        conn.close()
    # If the doc was auto-transitioned to paid and we removed an
    # instalment, leave the status as-is — operator must manually undo
    # via the existing unmark-paid flow. We just emit an audit entry.
    try:
        db.insert_audit_log(doc_id, "partial_payment_deleted",
                            {"partial_id": partial_id}, performed_by="system")
    except Exception:  # noqa: BLE001
        pass
    return True


def auto_transition_if_complete(doc_id: str, *, by: Optional[str] = None) -> bool:
    """If running total >= invoice total, flip doc.status to 'paid'.

    Idempotent — no-op if already paid or below threshold. Returns True
    when a transition happened.
    """
    doc = db.get_document(doc_id)
    if not doc:
        return False
    if doc.get("status") in ("paid", "archived"):
        return False
    invoice_total = float(doc.get("amount") or 0.0)
    if invoice_total <= 0:
        return False
    paid = total_paid(doc_id)
    if paid + 0.005 < invoice_total:   # half-cent epsilon for float rounding
        return False
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE documents SET status = 'paid', payment_executed_at = ?, "
            "payment_executed_by = ? WHERE id = ?",
            (now, by or "system (partial-payments auto-complete)", doc_id),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        db.insert_audit_log(doc_id, "auto_paid_via_partials",
                            {"total_paid": paid, "invoice_total": invoice_total},
                            performed_by=by or "system")
    except Exception:  # noqa: BLE001
        pass
    logger.info("doc %s auto-transitioned to paid (sum %.2f >= total %.2f)",
                doc_id, paid, invoice_total)
    return True
