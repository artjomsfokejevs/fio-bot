"""Cashflow analytics — Phase 2 of #94.

Combines:
  IN  = sum(revenue_receipts.amount_eur)         per stream × month
  OUT = sum(documents.amount_eur where status='paid')  per stream × month

Net = IN - OUT. We expose monthly series, per-stream breakdowns, and
per-ledger breakdowns. Foundation for the Revenue dashboard + Analytics
cashflow chart.

The "month" key is YYYY-MM and is derived from:
  * revenue_receipts.received_at     (cash basis on the income side)
  * documents.payment_executed_at     (cash basis on the expense side)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services import db
from services import pc_codes

logger = logging.getLogger(__name__)

__all__ = [
    "monthly_series",
    "breakdown_by_stream",
    "breakdown_by_ledger",
    "totals_for_period",
]


def _pc_filter_sql(pc: Optional[str], col: str = "profit_center"):
    """Returns (clause, params). Translates PC to canonical + legacy aliases."""
    if not pc:
        return "", []
    canonical = pc_codes.to_canonical(pc) or pc
    aliases = pc_codes.legacy_aliases_of(canonical)
    placeholders = ",".join("?" for _ in aliases)
    return f" AND {col} IN ({placeholders})", list(aliases)


def monthly_series(period_from: Optional[str] = None,
                   period_to: Optional[str] = None,
                   pc: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns [{month, revenue, expense, net}, ...] for the window.

    period_from / period_to are 'YYYY-MM-DD' inclusive. pc filters one stream.
    """
    conn = db.get_connection()
    try:
        # ── Revenue side ─────────────────────────────────────────────────
        rev_where = ["1=1"]
        rev_params: List[Any] = []
        if period_from:
            rev_where.append("rr.received_at >= ?"); rev_params.append(period_from)
        if period_to:
            rev_where.append("rr.received_at <= ? || 'T23:59:59'"); rev_params.append(period_to)
        pc_clause, pc_params = _pc_filter_sql(pc, "rd.profit_center")
        rev_sql = (
            "SELECT substr(rr.received_at, 1, 7) AS month, "
            "       SUM(rr.amount_eur) AS revenue "
            "FROM revenue_receipts rr "
            "JOIN revenue_documents rd ON rd.id = rr.revenue_doc_id "
            "WHERE " + " AND ".join(rev_where) + pc_clause +
            " GROUP BY month"
        )
        rev_rows = conn.execute(rev_sql, tuple(rev_params + pc_params)).fetchall()

        # ── Expense side (documents.payment_executed_at when status=paid) ─
        exp_where = ["status = 'paid'", "payment_executed_at IS NOT NULL"]
        exp_params: List[Any] = []
        if period_from:
            exp_where.append("payment_executed_at >= ?"); exp_params.append(period_from)
        if period_to:
            exp_where.append("payment_executed_at <= ? || 'T23:59:59'"); exp_params.append(period_to)
        exp_pc_clause, exp_pc_params = _pc_filter_sql(pc, "profit_center")
        exp_sql = (
            "SELECT substr(payment_executed_at, 1, 7) AS month, "
            "       SUM(COALESCE(amount, 0)) AS expense "
            "FROM documents "
            "WHERE " + " AND ".join(exp_where) + exp_pc_clause +
            " GROUP BY month"
        )
        exp_rows = conn.execute(exp_sql, tuple(exp_params + exp_pc_params)).fetchall()
    finally:
        conn.close()

    # Merge into ordered series
    months: Dict[str, Dict[str, float]] = {}
    for r in rev_rows:
        months.setdefault(r["month"], {"revenue": 0.0, "expense": 0.0})["revenue"] = float(r["revenue"] or 0)
    for r in exp_rows:
        months.setdefault(r["month"], {"revenue": 0.0, "expense": 0.0})["expense"] = float(r["expense"] or 0)
    out = []
    for month in sorted(months.keys()):
        rev = months[month]["revenue"]
        exp = months[month]["expense"]
        out.append({"month": month, "revenue": rev, "expense": exp,
                    "net": rev - exp})
    return out


def breakdown_by_stream(period_from: Optional[str] = None,
                        period_to: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return per-stream {pc, label, revenue, expense, net} aggregated over the window."""
    conn = db.get_connection()
    try:
        rev_where = ["1=1"]; rev_params: List[Any] = []
        if period_from:
            rev_where.append("rr.received_at >= ?"); rev_params.append(period_from)
        if period_to:
            rev_where.append("rr.received_at <= ? || 'T23:59:59'"); rev_params.append(period_to)
        rev_sql = (
            "SELECT rd.profit_center AS pc, SUM(rr.amount_eur) AS revenue "
            "FROM revenue_receipts rr "
            "JOIN revenue_documents rd ON rd.id = rr.revenue_doc_id "
            "WHERE " + " AND ".join(rev_where) + " GROUP BY rd.profit_center"
        )
        rev_rows = conn.execute(rev_sql, tuple(rev_params)).fetchall()

        exp_where = ["status = 'paid'", "payment_executed_at IS NOT NULL"]
        exp_params: List[Any] = []
        if period_from:
            exp_where.append("payment_executed_at >= ?"); exp_params.append(period_from)
        if period_to:
            exp_where.append("payment_executed_at <= ? || 'T23:59:59'"); exp_params.append(period_to)
        exp_sql = (
            "SELECT profit_center AS pc, SUM(COALESCE(amount, 0)) AS expense "
            "FROM documents WHERE " + " AND ".join(exp_where) + " GROUP BY profit_center"
        )
        exp_rows = conn.execute(exp_sql, tuple(exp_params)).fetchall()
    finally:
        conn.close()

    by_pc: Dict[str, Dict[str, float]] = {}
    for r in rev_rows:
        canonical = pc_codes.to_canonical(r["pc"]) or r["pc"] or "??"
        by_pc.setdefault(canonical, {"revenue": 0.0, "expense": 0.0})["revenue"] += float(r["revenue"] or 0)
    for r in exp_rows:
        canonical = pc_codes.to_canonical(r["pc"]) or r["pc"] or "??"
        by_pc.setdefault(canonical, {"revenue": 0.0, "expense": 0.0})["expense"] += float(r["expense"] or 0)
    out = []
    for pc, totals in sorted(by_pc.items()):
        out.append({
            "pc": pc,
            "label": pc_codes.label_of(pc),
            "revenue": totals["revenue"],
            "expense": totals["expense"],
            "net": totals["revenue"] - totals["expense"],
        })
    out.sort(key=lambda r: -r["revenue"])
    return out


def breakdown_by_ledger(period_from: Optional[str] = None,
                        period_to: Optional[str] = None,
                        side: str = "both") -> List[Dict[str, Any]]:
    """Per-ledger-code totals. side ∈ {'revenue', 'expense', 'both'}."""
    out: Dict[str, Dict[str, float]] = {}
    conn = db.get_connection()
    try:
        if side in ("revenue", "both"):
            where = ["1=1"]; params: List[Any] = []
            if period_from:
                where.append("rr.received_at >= ?"); params.append(period_from)
            if period_to:
                where.append("rr.received_at <= ? || 'T23:59:59'"); params.append(period_to)
            sql = (
                "SELECT COALESCE(rd.ledger_code, 'UNCODED') AS code, "
                "       SUM(rr.amount_eur) AS revenue "
                "FROM revenue_receipts rr "
                "JOIN revenue_documents rd ON rd.id = rr.revenue_doc_id "
                "WHERE " + " AND ".join(where) + " GROUP BY code"
            )
            for r in conn.execute(sql, tuple(params)).fetchall():
                out.setdefault(r["code"], {"revenue": 0.0, "expense": 0.0})["revenue"] += float(r["revenue"] or 0)

        if side in ("expense", "both"):
            where = ["status = 'paid'", "payment_executed_at IS NOT NULL"]
            params = []
            if period_from:
                where.append("payment_executed_at >= ?"); params.append(period_from)
            if period_to:
                where.append("payment_executed_at <= ? || 'T23:59:59'"); params.append(period_to)
            # documents have allocations table; for simplicity sum by ledger_code on top-level
            sql = (
                "SELECT COALESCE(ledger_code, 'UNCODED') AS code, "
                "       SUM(COALESCE(amount, 0)) AS expense "
                "FROM documents WHERE " + " AND ".join(where) + " GROUP BY code"
            )
            try:
                for r in conn.execute(sql, tuple(params)).fetchall():
                    out.setdefault(r["code"], {"revenue": 0.0, "expense": 0.0})["expense"] += float(r["expense"] or 0)
            except Exception as exc:
                # documents may not have a ledger_code column in older schemas — degrade gracefully
                logger.warning("expense ledger breakdown skipped: %s", exc)
    finally:
        conn.close()

    rows = []
    for code, totals in out.items():
        rows.append({"code": code, "revenue": totals["revenue"],
                     "expense": totals["expense"],
                     "net": totals["revenue"] - totals["expense"]})
    rows.sort(key=lambda r: -(abs(r["revenue"]) + abs(r["expense"])))
    return rows


def totals_for_period(period_from: Optional[str] = None,
                      period_to: Optional[str] = None,
                      pc: Optional[str] = None) -> Dict[str, float]:
    series = monthly_series(period_from, period_to, pc)
    rev = sum(r["revenue"] for r in series)
    exp = sum(r["expense"] for r in series)
    return {"revenue": rev, "expense": exp, "net": rev - exp,
            "months": len(series)}
