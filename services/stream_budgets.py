"""Stream budgets — Phase 3 (2026-06-16).

Per-stream monthly cost caps agreed with the Holding CEO. See
docs/stream-budgets-architecture.md for the design.

set_budget() writes a history row on every change so the audit trail
is complete without a separate journaling layer.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "list_budgets",
    "get_budget",
    "set_budget",
    "history_for",
    "actuals_for",
    "is_over",
]

# Document statuses that count toward "money committed this period"
_COMMITTED_STATUSES = ("confirmed_to_pay", "paid", "archived",
                       "payment_executed", "budget_validated")


def list_budgets(period: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        if period:
            rows = conn.execute(
                "SELECT * FROM stream_budgets WHERE period = ? "
                "ORDER BY profit_center",
                (period,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stream_budgets ORDER BY period DESC, profit_center"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_budget(pc: str, period: str) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM stream_budgets WHERE profit_center = ? AND period = ?",
            (pc, period),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_budget(*, pc: str, period: str, eur: float,
               agreed_by_ceo: Optional[str] = None,
               agreed_by_ceo_at: Optional[str] = None,
               notes: Optional[str] = None,
               reason: Optional[str] = None,
               by: Optional[str] = None) -> Dict[str, Any]:
    if not pc:
        raise ValueError("profit_center required")
    if not period or len(period) != 7 or period[4] != "-":
        raise ValueError("period must be YYYY-MM")
    try:
        eur = float(eur)
    except (TypeError, ValueError):
        raise ValueError("eur must be a number")
    if eur < 0:
        raise ValueError("eur must be >= 0")

    now = datetime.utcnow().isoformat()
    existing = get_budget(pc, period)
    old_eur = float(existing["budget_eur"]) if existing else None
    conn = db.get_connection()
    try:
        if existing:
            conn.execute(
                "UPDATE stream_budgets SET budget_eur=?, agreed_by_ceo=?, "
                "agreed_by_ceo_at=?, notes=?, updated_at=?, updated_by=? "
                "WHERE id=?",
                (eur, agreed_by_ceo, agreed_by_ceo_at, notes, now, by, existing["id"]),
            )
            bid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO stream_budgets "
                "(profit_center, period, budget_eur, agreed_by_ceo, "
                " agreed_by_ceo_at, notes, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pc, period, eur, agreed_by_ceo, agreed_by_ceo_at, notes, now, by),
            )
            bid = cur.lastrowid
        conn.execute(
            "INSERT INTO stream_budget_history "
            "(budget_id, pc, period, changed_at, changed_by, old_eur, new_eur, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bid, pc, period, now, by, old_eur, eur, reason),
        )
        conn.commit()
    finally:
        conn.close()
    return get_budget(pc, period)  # type: ignore[return-value]


def history_for(pc: Optional[str] = None, period: Optional[str] = None,
                limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM stream_budget_history"
    where = []
    params: List[Any] = []
    if pc:
        where.append("pc = ?"); params.append(pc)
    if period:
        where.append("period = ?"); params.append(period)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY changed_at DESC LIMIT ?"
    params.append(limit)
    conn = db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def actuals_for(pc: str, period: str) -> float:
    """Sum of EUR-equivalent for docs in this (pc, period) that count
    toward the spend. Uses amount_eur when present, else amount when
    currency=EUR, else 0 (skipped)."""
    # NOTE: `documents.amount` is EUR after parser FX-conversion (since
    # Phase 2.1 — original currency lives in amount_orig/currency_orig).
    # No separate amount_eur column. Just sum amount.
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT amount FROM documents "
            "WHERE profit_center = ? AND period = ? AND status IN (%s)" %
            ",".join("?" for _ in _COMMITTED_STATUSES),
            (pc, period) + _COMMITTED_STATUSES,
        ).fetchall()
        total = sum(float(r["amount"] or 0) for r in rows)
        return round(total, 2)
    finally:
        conn.close()


def is_over(pc: str, period: str) -> Dict[str, Any]:
    """Returns the budget/actual/remaining status for the (pc, period)."""
    b = get_budget(pc, period)
    budget = float(b["budget_eur"]) if b else 0.0
    actual = actuals_for(pc, period)
    return {
        "profit_center": pc,
        "period": period,
        "has_budget": bool(b),
        "budget_eur": budget,
        "actual_eur": actual,
        "remaining_eur": round(budget - actual, 2),
        "over": (budget > 0 and actual > budget),
        "overrun_eur": round(max(0.0, actual - budget), 2),
        "overrun_pct": round(((actual - budget) / budget * 100), 2) if budget > 0 else 0.0,
    }
