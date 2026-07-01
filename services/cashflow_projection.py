"""13-week rolling cash-flow projection — Phase 3 (G1) of FIO Governance SOP.

Combines existing data with no external dependencies:

  IN  — revenue_documents.amount_eur where status in (sent, partially_paid)
        and due_date falls in the projection window
  OUT — documents.amount where status in (budget_validated, confirmed_to_pay)
        and desired_payment_date falls in the projection window
  OPEN — latest bank_account_balances row per PC

A row is emitted per ISO week × PC, plus a consolidated "ALL" row. The
output is the input to the Slack daily digest, the Analytics cashflow
chart, and (later) the X-alarm on runway < 90 days.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from services import db
from services import pc_codes

logger = logging.getLogger(__name__)

__all__ = [
    "project",
    "opening_balance_for",
    "set_opening_balance",
    "list_opening_balances",
    "plan_by_week",
]


# 2026-07-01 — the operator uploads the Amitours Weekly Cashflow Timeline
# CSV, which lands in cashflow_weekly with row_type in
# ('forecast','estimate','plug'). We overlay those planned inflows /
# outflows on top of the 13-week projection so operators see plan vs fact
# side by side and can spot variances the same week they happen.
#
# Column mapping — cashflow_weekly numeric fields → plan-AR vs plan-AP
# buckets. We treat b2c/b2b revenue plans + financing_inflow + holdback +
# portfolio_inflows as inflows; all *_plan burn/COGS/marketing +
# financing_outflow + outstanding_ap_intercompany as outflows.  Signs in
# the sheet are already negative for outflows, so we take abs() on the AP
# side to keep both series positive-magnitude (net = ar - ap).
_PLAN_AR_FIELDS = (
    "b2c_revenue_plan",
    "b2b_revenue_plan",
    "financing_inflow",
    "holdback",
    "portfolio_inflows",
)
_PLAN_AP_FIELDS = (
    "a2a_burn_plan",
    "a2a_cogs_plan",
    "a2a_cogs_ap",
    "marketing_plan",
    "portfolio_burn_plan",
    "holding_royalty",
    "financing_outflow",
    "outstanding_ap_intercompany",
)


def plan_by_week(week_starts: List[str]) -> Dict[str, Dict[str, float]]:
    """Read cashflow_weekly for the given Monday-anchored week list and
    aggregate every plan-tagged row into a per-week {ar, ap, net} bag.

    Multiple planning rows can land on the same week (forecast + plug + …);
    we sum them so the operator's total picture is preserved.
    """
    if not week_starts:
        return {}
    placeholders = ",".join("?" for _ in week_starts)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM cashflow_weekly "
            f"WHERE week_start IN ({placeholders}) "
            f"AND row_type IN ('forecast','estimate','plug')",
            tuple(week_starts),
        ).fetchall()
    finally:
        conn.close()
    by_week: Dict[str, Dict[str, float]] = {
        w: {"ar": 0.0, "ap": 0.0, "net": 0.0, "row_count": 0} for w in week_starts
    }
    for r in rows:
        d = dict(r)
        w = d.get("week_start")
        if w not in by_week:
            continue
        by_week[w]["row_count"] += 1
        for f in _PLAN_AR_FIELDS:
            try:
                by_week[w]["ar"] += float(d.get(f) or 0)
            except (TypeError, ValueError):
                pass
        for f in _PLAN_AP_FIELDS:
            try:
                by_week[w]["ap"] += abs(float(d.get(f) or 0))
            except (TypeError, ValueError):
                pass
    for w, v in by_week.items():
        v["ar"] = round(v["ar"], 2)
        v["ap"] = round(v["ap"], 2)
        v["net"] = round(v["ar"] - v["ap"], 2)
    return by_week


def _week_starts(start: datetime, weeks: int) -> List[str]:
    """Return ISO YYYY-MM-DD strings for the Monday of each week."""
    monday = start - timedelta(days=start.weekday())
    return [(monday + timedelta(days=7 * i)).strftime("%Y-%m-%d") for i in range(weeks)]


def opening_balance_for(pc: Optional[str] = None) -> float:
    """Latest snapshot of bank balance per PC (or all PCs if pc=None).

    Returns EUR sum of the most recent balance per paying_account.
    """
    conn = db.get_connection()
    try:
        # Per-account latest row: subselect MAX(as_of_date) per account
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            placeholders = ",".join("?" for _ in aliases)
            sql = (
                f"SELECT SUM(balance_eur) AS total FROM bank_account_balances b "
                f"WHERE pc IN ({placeholders}) AND as_of_date = ("
                f"  SELECT MAX(as_of_date) FROM bank_account_balances "
                f"  WHERE COALESCE(paying_account_id,0) = COALESCE(b.paying_account_id,0) "
                f"  AND pc IN ({placeholders})"
                f")"
            )
            row = conn.execute(sql, tuple(aliases + aliases)).fetchone()
        else:
            row = conn.execute(
                "SELECT SUM(balance_eur) AS total FROM bank_account_balances b "
                "WHERE as_of_date = ("
                "  SELECT MAX(as_of_date) FROM bank_account_balances "
                "  WHERE COALESCE(paying_account_id,0) = COALESCE(b.paying_account_id,0)"
                ")"
            ).fetchone()
        return float(row["total"] or 0.0) if row else 0.0
    finally:
        conn.close()


def set_opening_balance(*, pc: str, balance_eur: float,
                        as_of_date: Optional[str] = None,
                        paying_account_id: Optional[int] = None,
                        legal_entity: Optional[str] = None,
                        balance_orig: Optional[float] = None,
                        currency: Optional[str] = None,
                        source: str = "manual",
                        by: Optional[str] = None) -> Dict[str, Any]:
    """Record a balance snapshot. Multiple snapshots per (pc, account) OK —
    `project()` reads the latest as_of_date."""
    pc_canonical = pc_codes.to_canonical(pc) or pc
    now = datetime.utcnow().isoformat()
    as_of = (as_of_date or now)[:10]
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO bank_account_balances "
            "(paying_account_id, pc, legal_entity, balance_eur, balance_orig, "
            " currency, as_of_date, source, recorded_at, recorded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (paying_account_id, pc_canonical, legal_entity,
             float(balance_eur), balance_orig, currency,
             as_of, source, now, by),
        )
        conn.commit()
        return {"id": cur.lastrowid, "pc": pc_canonical,
                "balance_eur": float(balance_eur), "as_of_date": as_of}
    finally:
        conn.close()


def list_opening_balances(pc: Optional[str] = None,
                          limit: int = 200) -> List[Dict[str, Any]]:
    """List recorded balance snapshots (newest first)."""
    conn = db.get_connection()
    try:
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            placeholders = ",".join("?" for _ in aliases)
            sql = (f"SELECT * FROM bank_account_balances WHERE pc IN ({placeholders}) "
                   f"ORDER BY as_of_date DESC, id DESC LIMIT ?")
            rows = conn.execute(sql, tuple(aliases) + (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bank_account_balances "
                "ORDER BY as_of_date DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _ar_inflows_by_week(week_starts: List[str], pc: Optional[str]) -> Dict[str, float]:
    """Sum of (amount_eur - already-received) per ISO week from revenue_documents.due_date."""
    if not week_starts:
        return {}
    window_start = week_starts[0]
    window_end = (datetime.strptime(week_starts[-1], "%Y-%m-%d")
                  + timedelta(days=7)).strftime("%Y-%m-%d")
    conn = db.get_connection()
    try:
        pc_filter, pc_params = "", []
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            ph = ",".join("?" for _ in aliases)
            pc_filter = f" AND profit_center IN ({ph})"
            pc_params = list(aliases)
        rows = conn.execute(
            "SELECT due_date, COALESCE(amount_eur, amount, 0) AS amount, id "
            "FROM revenue_documents "
            "WHERE status IN ('sent','partially_paid') "
            "AND due_date IS NOT NULL "
            f"AND due_date >= ? AND due_date < ?{pc_filter}",
            (window_start, window_end, *pc_params),
        ).fetchall()
        receipts = {row["revenue_doc_id"]: float(row["total_received"] or 0) for row in
                    conn.execute(
                        "SELECT revenue_doc_id, SUM(amount_eur) AS total_received "
                        "FROM revenue_receipts GROUP BY revenue_doc_id"
                    ).fetchall()}
    finally:
        conn.close()
    by_week: Dict[str, float] = {w: 0.0 for w in week_starts}
    for r in rows:
        due = r["due_date"][:10]
        amount = float(r["amount"] or 0) - receipts.get(r["id"], 0.0)
        if amount <= 0:
            continue
        # Find the week-start that contains this due date
        d = datetime.strptime(due, "%Y-%m-%d")
        monday = d - timedelta(days=d.weekday())
        key = monday.strftime("%Y-%m-%d")
        if key in by_week:
            by_week[key] += amount
    return by_week


def _ap_outflows_by_week(week_starts: List[str], pc: Optional[str]) -> Dict[str, float]:
    """Sum of amount per ISO week from documents.desired_payment_date,
    where the doc is committed (budget_validated or confirmed_to_pay)."""
    if not week_starts:
        return {}
    window_start = week_starts[0]
    window_end = (datetime.strptime(week_starts[-1], "%Y-%m-%d")
                  + timedelta(days=7)).strftime("%Y-%m-%d")
    conn = db.get_connection()
    try:
        pc_filter, pc_params = "", []
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            ph = ",".join("?" for _ in aliases)
            pc_filter = f" AND profit_center IN ({ph})"
            pc_params = list(aliases)
        rows = conn.execute(
            "SELECT desired_payment_date, COALESCE(amount, 0) AS amount, profit_center "
            "FROM documents "
            "WHERE status IN ('budget_validated', 'confirmed_to_pay') "
            "AND desired_payment_date IS NOT NULL "
            f"AND desired_payment_date >= ? AND desired_payment_date < ?{pc_filter}",
            (window_start, window_end, *pc_params),
        ).fetchall()
    finally:
        conn.close()
    by_week: Dict[str, float] = {w: 0.0 for w in week_starts}
    for r in rows:
        d_s = r["desired_payment_date"][:10]
        try:
            d = datetime.strptime(d_s, "%Y-%m-%d")
        except ValueError:
            continue
        monday = d - timedelta(days=d.weekday())
        key = monday.strftime("%Y-%m-%d")
        if key in by_week:
            by_week[key] += float(r["amount"] or 0)
    return by_week


def project(weeks: int = 13, pc: Optional[str] = None,
            opening_override: Optional[float] = None) -> Dict[str, Any]:
    """Build the rolling projection.

    Returns:
      {
        weeks: 13,
        pc: 'AA' | None,
        opening_balance_eur: 380000.00,
        series: [
          {week_start, ar_in, ap_out, net, running_balance},
          ...
        ],
        runway_weeks: int | None,   # weeks until running_balance < 0; None = stays positive
      }
    """
    if weeks < 1 or weeks > 52:
        raise ValueError("weeks must be between 1 and 52")
    week_starts = _week_starts(datetime.utcnow(), weeks)
    in_by_week = _ar_inflows_by_week(week_starts, pc)
    out_by_week = _ap_outflows_by_week(week_starts, pc)
    # 2026-07-01 — overlay the operator's imported plan so each row shows
    # both fact and plan side by side, letting the UI colour variances.
    # Plan is entity-wide (Amitours unified), so we do not scope it by PC.
    plan_bw = plan_by_week(week_starts)
    opening = (opening_override
               if opening_override is not None
               else opening_balance_for(pc))

    series: List[Dict[str, Any]] = []
    running = opening
    running_plan = opening
    runway: Optional[int] = None
    for idx, w in enumerate(week_starts):
        ar = round(in_by_week.get(w, 0.0), 2)
        ap = round(out_by_week.get(w, 0.0), 2)
        net = ar - ap
        running = round(running + net, 2)
        p = plan_bw.get(w) or {"ar": 0.0, "ap": 0.0, "net": 0.0, "row_count": 0}
        plan_ar = round(p["ar"], 2)
        plan_ap = round(p["ap"], 2)
        plan_net = round(p["net"], 2)
        running_plan = round(running_plan + plan_net, 2)
        if running < 0 and runway is None:
            runway = idx + 1
        series.append({
            "week_start": w,
            "ar_in": ar,
            "ap_out": ap,
            "net": round(net, 2),
            "running_balance": running,
            # Plan overlay (from Weekly Cashflow Timeline import)
            "plan_ar_in":  plan_ar,
            "plan_ap_out": plan_ap,
            "plan_net":    plan_net,
            "plan_running_balance": running_plan,
            "plan_row_count":       int(p["row_count"]),
            # Fact − plan variance. Positive = fact beat plan (good on AR,
            # bad on AP). UI flips the sign on AP variance for red/green.
            "ar_variance":  round(ar - plan_ar, 2),
            "ap_variance":  round(ap - plan_ap, 2),
            "net_variance": round(net - plan_net, 2),
        })
    return {
        "weeks": weeks,
        "pc": pc or "ALL",
        "opening_balance_eur": round(opening, 2),
        "series": series,
        "runway_weeks": runway,
        "ending_balance_eur": series[-1]["running_balance"] if series else round(opening, 2),
        "ending_balance_plan_eur": (series[-1]["plan_running_balance"]
                                    if series else round(opening, 2)),
    }
