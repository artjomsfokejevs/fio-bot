"""Weekly cashflow timeline — Amitours UNIFIED CASH TIMELINE.

Mirrors the operator's planning Google Sheet (Actuals from TxData,
Forecast from Assumptions). Two read paths:

  - list_weeks(weeks_before, weeks_after) — returns merged actual /
    forecast / estimate / plug rows ordered by week_start.
  - totals(period_start, period_end) — aggregates for the cashflow
    overview card (total revenue, COGS, burn, AP, AR, liquidity).

Two write paths (operator-facing, for forecast/estimate/plug rows only):

  - upsert_row(week_start, row_type, **fields) — UNIQUE(week_start,
    row_type), so the same week can carry one Actual + one Forecast row
    side by side.
  - delete_row(week_start, row_type).

This module is the read/write contract; the UI editor + Google-Sheet
import path land in a follow-up phase.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "VALID_ROW_TYPES",
    "WRITABLE_ROW_TYPES",
    "FIELD_GROUPS",
    "list_weeks",
    "totals",
    "upsert_row",
    "delete_row",
    "monday_of",
]

VALID_ROW_TYPES = ("actual", "forecast", "estimate", "plug")
# Actuals are derived from source data on every recompute, so writing
# them by hand is a foot-gun. Restrict mutators to the operator-driven
# planning row types.
WRITABLE_ROW_TYPES = ("forecast", "estimate", "plug")

# Grouped for the UI — the operator's sheet renders columns in this
# order; we keep the same grouping so the future editor is one-glance.
FIELD_GROUPS = {
    "revenue": [
        "b2c_revenue_fact", "b2c_revenue_plan",
        "b2b_revenue_fact", "b2b_revenue_plan",
        "holdback",
    ],
    "financing": ["financing_inflow", "financing_outflow"],
    "alps2alps": [
        "a2a_burn_fact", "a2a_burn_plan",
        "marketing_plan",
        "a2a_cogs_fact", "a2a_cogs_plan", "a2a_cogs_ap",
    ],
    "portfolio": [
        "holding_royalty",
        "portfolio_burn_fact", "portfolio_burn_plan",
        "portfolio_inflows",
    ],
    "other": ["outstanding_ap_intercompany"],
    "computed": ["net_eur", "balance_eop_eur"],
}

_ALL_NUMERIC_FIELDS = [f for g in FIELD_GROUPS.values() for f in g]


def monday_of(d: Optional[date] = None) -> str:
    """Return the ISO YYYY-MM-DD of the Monday that anchors ``d``'s week.
    Default ``d`` = today (UTC)."""
    d = d or datetime.utcnow().date()
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def _row_to_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "id":          row.get("id"),
        "week_start":  row.get("week_start"),
        "week_label":  row.get("week_label"),
        "row_type":    row.get("row_type"),
        "note":        row.get("note"),
        "source":      row.get("source"),
        "updated_at":  row.get("updated_at"),
        "updated_by":  row.get("updated_by"),
        "fields":      {f: row.get(f) for f in _ALL_NUMERIC_FIELDS},
    }
    return out


def list_weeks(weeks_before: int = 8,
                weeks_after: int = 13,
                row_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return weekly rows centred on the current ISO Monday.

    Default window: 8 weeks past + 13 weeks future (matches the operator's
    planning view).
    """
    if weeks_before < 0 or weeks_after < 0:
        raise ValueError("weeks_before / weeks_after must be non-negative")
    today_monday = datetime.strptime(monday_of(), "%Y-%m-%d").date()
    start = (today_monday - timedelta(days=7 * weeks_before)).strftime("%Y-%m-%d")
    end = (today_monday + timedelta(days=7 * weeks_after)).strftime("%Y-%m-%d")

    types = tuple(row_types) if row_types else VALID_ROW_TYPES
    placeholders = ",".join("?" for _ in types)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM cashflow_weekly "
            f"WHERE week_start >= ? AND week_start <= ? "
            f"AND row_type IN ({placeholders}) "
            f"ORDER BY week_start ASC, row_type ASC",
            (start, end, *types),
        ).fetchall()
    finally:
        conn.close()

    payload_rows = [_row_to_payload(dict(r)) for r in rows]
    return {
        "window_start": start,
        "window_end":   end,
        "today_monday": today_monday.strftime("%Y-%m-%d"),
        "rows":         payload_rows,
        "count":        len(payload_rows),
    }


def _safe_sum(rows: List[Dict[str, Any]], field: str) -> float:
    total = 0.0
    for r in rows:
        v = (r.get("fields") or {}).get(field)
        try:
            total += float(v or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def totals(weeks_before: int = 8, weeks_after: int = 13) -> Dict[str, Any]:
    """Aggregate numbers across the window — feeds the cashflow overview
    card and any "is the period healthy?" UI strip."""
    listing = list_weeks(weeks_before=weeks_before, weeks_after=weeks_after)
    rows = listing["rows"]
    return {
        "window_start": listing["window_start"],
        "window_end":   listing["window_end"],
        "b2c_revenue_fact": _safe_sum(rows, "b2c_revenue_fact"),
        "b2c_revenue_plan": _safe_sum(rows, "b2c_revenue_plan"),
        "b2b_revenue_fact": _safe_sum(rows, "b2b_revenue_fact"),
        "b2b_revenue_plan": _safe_sum(rows, "b2b_revenue_plan"),
        "a2a_burn_fact":    _safe_sum(rows, "a2a_burn_fact"),
        "a2a_burn_plan":    _safe_sum(rows, "a2a_burn_plan"),
        "a2a_cogs_fact":    _safe_sum(rows, "a2a_cogs_fact"),
        "a2a_cogs_plan":    _safe_sum(rows, "a2a_cogs_plan"),
        "marketing_plan":   _safe_sum(rows, "marketing_plan"),
        "portfolio_burn_fact": _safe_sum(rows, "portfolio_burn_fact"),
        "portfolio_burn_plan": _safe_sum(rows, "portfolio_burn_plan"),
        "financing_inflow":  _safe_sum(rows, "financing_inflow"),
        "financing_outflow": _safe_sum(rows, "financing_outflow"),
        "outstanding_ap":    _safe_sum(rows, "outstanding_ap_intercompany"),
        "holdback":          _safe_sum(rows, "holdback"),
        "row_count":         len(rows),
    }


def upsert_row(*, week_start: str, row_type: str,
                fields: Optional[Dict[str, Any]] = None,
                week_label: Optional[str] = None,
                note: Optional[str] = None,
                source: Optional[str] = None,
                by: Optional[str] = None) -> Dict[str, Any]:
    """Insert or replace one (week_start, row_type) row.

    ``fields`` is a dict of any subset of FIELD_GROUPS' numeric columns.
    Unknown keys are ignored (defensive — the API stays additive).
    """
    if row_type not in WRITABLE_ROW_TYPES:
        raise ValueError(
            f"row_type {row_type!r} is not writable (actuals are derived); "
            f"valid: {WRITABLE_ROW_TYPES}"
        )
    if not week_start or len(week_start) != 10:
        raise ValueError("week_start must be ISO YYYY-MM-DD")
    try:
        datetime.strptime(week_start, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("week_start must be ISO YYYY-MM-DD: " + str(exc))
    fields = dict(fields or {})
    cleaned_fields: Dict[str, Any] = {}
    for k, v in fields.items():
        if k not in _ALL_NUMERIC_FIELDS:
            continue
        if v is None or v == "":
            cleaned_fields[k] = None
            continue
        try:
            cleaned_fields[k] = float(v)
        except (TypeError, ValueError):
            cleaned_fields[k] = None

    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM cashflow_weekly WHERE week_start = ? AND row_type = ?",
            (week_start, row_type),
        ).fetchone()
        params = {
            "week_start": week_start,
            "week_label": week_label,
            "row_type":   row_type,
            "note":       note,
            "source":     source or "manual",
            "updated_at": now,
            "updated_by": by,
            **{f: cleaned_fields.get(f) for f in _ALL_NUMERIC_FIELDS},
        }
        if existing:
            set_clause = ", ".join(f"{k} = :{k}" for k in params if k != "row_type" and k != "week_start")
            params["_id"] = existing["id"]
            conn.execute(
                f"UPDATE cashflow_weekly SET {set_clause} WHERE id = :_id",
                params,
            )
            row_id = existing["id"]
        else:
            cols = ", ".join(params.keys())
            slots = ", ".join(f":{k}" for k in params.keys())
            cur = conn.execute(
                f"INSERT INTO cashflow_weekly ({cols}) VALUES ({slots})",
                params,
            )
            row_id = cur.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM cashflow_weekly WHERE id = ?", (row_id,),
        ).fetchone()
        return _row_to_payload(dict(row))
    finally:
        conn.close()


def delete_row(week_start: str, row_type: str) -> bool:
    if row_type not in WRITABLE_ROW_TYPES:
        raise ValueError(
            f"row_type {row_type!r} is not deletable (actuals are derived)"
        )
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM cashflow_weekly WHERE week_start = ? AND row_type = ?",
            (week_start, row_type),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
