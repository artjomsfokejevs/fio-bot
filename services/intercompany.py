"""Inter-company elimination — Phase 3 (G2) of FIO Governance SOP §4.2.

When AG charges AA for shared services, the AG row and the AA row both
post to actuals — so the holding sum double-counts that €1. The elimination
layer subtracts every (paid expense doc where counterparty_pc IS NOT NULL)
from the raw total to produce a clean consolidated number.

Storage: documents.counterparty_pc (NULL = external vendor, no elim).

Two functions:

  by_pair(period) → list of {pc_from, pc_to, amount} aggregates
  consolidated_pnl(period, pc=None) → {raw, eliminations, consolidated,
                                        by_stream, by_ledger_group}

The consolidated_pnl output is the data contract for the Phase 4
"Consolidated P&L" dashboard card.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from services import db
from services import pc_codes

logger = logging.getLogger(__name__)

__all__ = [
    "by_pair",
    "consolidated_pnl",
    "set_counterparty_pc",
    "intercompany_total",
]


# Statuses that count as "real cost" for elimination — same set used by
# the existing analytics service.
_PAID_STATUSES = ("paid", "confirmed_to_pay", "budget_validated",
                  "approved", "posted")


def _period_clause(period: Optional[str]) -> tuple[str, list]:
    if not period:
        return "", []
    # YYYY-MM
    return " AND period = ?", [period]


def by_pair(period: Optional[str] = None) -> List[Dict[str, Any]]:
    """List intercompany expense rows grouped by (counterparty_pc → profit_center).

    Returns [{pc_from, pc_to, amount_eur, doc_count}, ...] sorted by amount desc.
    Only rows with non-null counterparty_pc count.
    """
    conn = db.get_connection()
    try:
        sql = (
            "SELECT counterparty_pc AS pc_from, profit_center AS pc_to, "
            "       SUM(COALESCE(amount, 0)) AS amount_eur, "
            "       COUNT(*) AS doc_count "
            "FROM documents "
            "WHERE counterparty_pc IS NOT NULL AND counterparty_pc != '' "
            "AND profit_center IS NOT NULL AND profit_center != '' "
            "AND status IN " + str(_PAID_STATUSES)
        )
        clause, params = _period_clause(period)
        sql += clause + " GROUP BY counterparty_pc, profit_center "
        sql += "ORDER BY amount_eur DESC"
        rows = conn.execute(sql, tuple(params)).fetchall()
        # Translate to canonical PC for display consistency
        out = []
        for r in rows:
            d = dict(r)
            d["pc_from"] = pc_codes.to_canonical(d["pc_from"]) or d["pc_from"]
            d["pc_to"] = pc_codes.to_canonical(d["pc_to"]) or d["pc_to"]
            d["amount_eur"] = round(float(d["amount_eur"] or 0), 2)
            out.append(d)
        return out
    finally:
        conn.close()


def intercompany_total(period: Optional[str] = None,
                       pc: Optional[str] = None) -> float:
    """Sum of elimination amounts; if pc given, only rows where pc_from or
    pc_to is that PC. Helps the by-stream view show how much was netted out
    per stream."""
    pairs = by_pair(period)
    if not pc:
        return round(sum(p["amount_eur"] for p in pairs), 2)
    canonical = pc_codes.to_canonical(pc) or pc
    return round(sum(p["amount_eur"] for p in pairs
                     if p["pc_from"] == canonical or p["pc_to"] == canonical), 2)


def consolidated_pnl(period: Optional[str] = None,
                     pc: Optional[str] = None) -> Dict[str, Any]:
    """Build the consolidated P&L per FIO Governance SOP §4.2.

    Returns:
      {
        period: 'YYYY-MM' | 'ALL',
        pc:     'AA' | 'ALL',
        raw_revenue: float,
        raw_expense: float,
        eliminations: {
          intercompany_total: float,
          pairs: [{pc_from, pc_to, amount_eur}, ...]
        },
        consolidated_revenue: float,
        consolidated_expense: float,
        consolidated_net: float,
        by_stream: [{pc, raw_revenue, raw_expense, eliminated, consolidated_net}, ...],
        by_ledger_group: {...}
      }
    """
    conn = db.get_connection()
    try:
        # ── 1. Raw expense rollup (documents.amount summed per PC) ──
        pc_filter, pc_params = "", []
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            ph = ",".join("?" for _ in aliases)
            pc_filter = f" AND profit_center IN ({ph})"
            pc_params = list(aliases)
        period_clause, period_params = _period_clause(period)
        exp_rows = conn.execute(
            "SELECT profit_center, ledger_code, "
            "       SUM(COALESCE(amount, 0)) AS amt, "
            "       SUM(CASE WHEN counterparty_pc IS NOT NULL AND counterparty_pc != '' "
            "                THEN COALESCE(amount, 0) ELSE 0 END) AS eliminated "
            "FROM documents "
            "WHERE status IN " + str(_PAID_STATUSES) +
            period_clause + pc_filter + " GROUP BY profit_center, ledger_code",
            tuple(period_params + pc_params),
        ).fetchall()
        # ── 2. Raw revenue from revenue_receipts (cash basis) ──
        rev_pc_filter, rev_pc_params = "", []
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            ph = ",".join("?" for _ in aliases)
            rev_pc_filter = f" AND rd.profit_center IN ({ph})"
            rev_pc_params = list(aliases)
        rev_period_clause = ""
        rev_period_params: List[Any] = []
        if period:
            rev_period_clause = " AND substr(rr.received_at, 1, 7) = ?"
            rev_period_params = [period]
        rev_rows = conn.execute(
            "SELECT rd.profit_center, SUM(rr.amount_eur) AS amt "
            "FROM revenue_receipts rr "
            "JOIN revenue_documents rd ON rd.id = rr.revenue_doc_id "
            "WHERE 1=1" + rev_period_clause + rev_pc_filter +
            " GROUP BY rd.profit_center",
            tuple(rev_period_params + rev_pc_params),
        ).fetchall()
    finally:
        conn.close()

    # Load ledger schema for statement_group rollup
    import os, config as _cfg
    code_to_group: Dict[str, str] = {}
    try:
        with open(_cfg.LEDGER_FILE, "r", encoding="utf-8") as f:
            schema = json.load(f)
        for c in schema.get("codes", []):
            code_to_group[c["code"]] = c.get("group", c.get("statement", "Other"))
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    by_stream: Dict[str, Dict[str, float]] = {}
    by_ledger_group: Dict[str, Dict[str, float]] = {}
    raw_expense = 0.0
    eliminated = 0.0

    for r in exp_rows:
        raw_pc = r["profit_center"] or "?"
        canonical = pc_codes.to_canonical(raw_pc) or raw_pc
        amt = float(r["amt"] or 0)
        elim = float(r["eliminated"] or 0)
        raw_expense += amt
        eliminated += elim
        s = by_stream.setdefault(canonical, {"raw_revenue": 0.0, "raw_expense": 0.0,
                                              "eliminated": 0.0})
        s["raw_expense"] += amt
        s["eliminated"] += elim
        group = code_to_group.get(r["ledger_code"] or "", "Other")
        g = by_ledger_group.setdefault(group, {"revenue": 0.0, "expense": 0.0,
                                                "eliminated": 0.0})
        g["expense"] += amt
        g["eliminated"] += elim

    raw_revenue = 0.0
    for r in rev_rows:
        canonical = pc_codes.to_canonical(r["profit_center"]) or r["profit_center"] or "?"
        amt = float(r["amt"] or 0)
        raw_revenue += amt
        s = by_stream.setdefault(canonical, {"raw_revenue": 0.0, "raw_expense": 0.0,
                                              "eliminated": 0.0})
        s["raw_revenue"] += amt

    # Compute consolidated per stream
    by_stream_out = []
    for k, s in by_stream.items():
        consolidated_expense = s["raw_expense"] - s["eliminated"]
        by_stream_out.append({
            "pc": k,
            "label": pc_codes.label_of(k),
            "raw_revenue": round(s["raw_revenue"], 2),
            "raw_expense": round(s["raw_expense"], 2),
            "eliminated": round(s["eliminated"], 2),
            "consolidated_revenue": round(s["raw_revenue"], 2),
            "consolidated_expense": round(consolidated_expense, 2),
            "consolidated_net": round(s["raw_revenue"] - consolidated_expense, 2),
        })
    by_stream_out.sort(key=lambda r: -(r["raw_revenue"] + r["raw_expense"]))

    pairs = by_pair(period)

    consolidated_expense = raw_expense - eliminated
    return {
        "period": period or "ALL",
        "pc": pc or "ALL",
        "raw_revenue": round(raw_revenue, 2),
        "raw_expense": round(raw_expense, 2),
        "eliminations": {
            "intercompany_total": round(eliminated, 2),
            "pairs": pairs,
        },
        "consolidated_revenue": round(raw_revenue, 2),
        "consolidated_expense": round(consolidated_expense, 2),
        "consolidated_net": round(raw_revenue - consolidated_expense, 2),
        "by_stream": by_stream_out,
        "by_ledger_group": {
            k: {
                "revenue":             round(v["revenue"], 2),
                "raw_expense":         round(v["expense"], 2),
                "eliminated":          round(v["eliminated"], 2),
                "consolidated_expense": round(v["expense"] - v["eliminated"], 2),
            }
            for k, v in by_ledger_group.items()
        },
    }


def set_counterparty_pc(doc_id: str, counterparty_pc: Optional[str],
                        *, by: Optional[str] = None) -> bool:
    """Flag a document as intercompany so it gets eliminated.

    Pass `None` (or empty string) to clear the flag.
    """
    canonical = (pc_codes.to_canonical(counterparty_pc) or counterparty_pc) if counterparty_pc else None
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE documents SET counterparty_pc = ? WHERE id = ?",
            (canonical or None, doc_id),
        )
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    if ok:
        try:
            db.insert_audit_log(doc_id, "counterparty_pc_set",
                                {"counterparty_pc": canonical, "by": by})
        except Exception:  # noqa: BLE001
            pass
    return ok
