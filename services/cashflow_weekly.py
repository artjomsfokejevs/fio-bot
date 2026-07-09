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
import re
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
    "import_tsv",
    "import_from_gsheet_url",
    "gsheet_url_to_export_url",
    "derive_actuals",
    "parse_amount",
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


def plan_vs_fact_summary(weeks_before: int = 8,
                          weeks_after: int = 13) -> Dict[str, Any]:
    """Dashboard payload — side-by-side plan vs fact across the window,
    per-week deltas, and top-5 variances so the operator sees where the
    business is drifting off plan the moment they open the tab.

    2026-07-01 — feeds the ▲ Plan-vs-fact card on the Weekly Cashflow
    Timeline tab.
    """
    listing = list_weeks(weeks_before=weeks_before, weeks_after=weeks_after)
    rows = listing["rows"]

    # Bucket rows by week; a week can carry multiple rows (forecast +
    # actual + plug), so per-week aggregation keeps intent clear.
    from collections import defaultdict
    per_week: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {
            "b2c_fact": 0.0, "b2c_plan": 0.0,
            "b2b_fact": 0.0, "b2b_plan": 0.0,
            "burn_fact": 0.0, "burn_plan": 0.0,
            "cogs_fact": 0.0, "cogs_plan": 0.0,
            "mktg_plan": 0.0,
            "pf_burn_fact": 0.0, "pf_burn_plan": 0.0,
            "row_types": set(),
        }
    )
    for r in rows:
        w = r.get("week_start") or ""
        f = r.get("fields") or {}
        b = per_week[w]
        for pair in (("b2c_fact", "b2c_revenue_fact"),
                     ("b2c_plan", "b2c_revenue_plan"),
                     ("b2b_fact", "b2b_revenue_fact"),
                     ("b2b_plan", "b2b_revenue_plan"),
                     ("burn_fact", "a2a_burn_fact"),
                     ("burn_plan", "a2a_burn_plan"),
                     ("cogs_fact", "a2a_cogs_fact"),
                     ("cogs_plan", "a2a_cogs_plan"),
                     ("mktg_plan", "marketing_plan"),
                     ("pf_burn_fact", "portfolio_burn_fact"),
                     ("pf_burn_plan", "portfolio_burn_plan")):
            try:
                b[pair[0]] += float(f.get(pair[1]) or 0)
            except (TypeError, ValueError):
                pass
        b["row_types"].add(r.get("row_type") or "")

    weeks_out: List[Dict[str, Any]] = []
    for w in sorted(per_week.keys()):
        b = per_week[w]
        b2c_var  = round(b["b2c_fact"] - b["b2c_plan"], 2)
        b2b_var  = round(b["b2b_fact"] - b["b2b_plan"], 2)
        burn_var = round(abs(b["burn_fact"]) - abs(b["burn_plan"]), 2)
        cogs_var = round(abs(b["cogs_fact"]) - abs(b["cogs_plan"]), 2)
        pf_var   = round(abs(b["pf_burn_fact"]) - abs(b["pf_burn_plan"]), 2)
        weeks_out.append({
            "week_start":   w,
            "row_types":    sorted(b["row_types"]),
            "b2c_fact":     round(b["b2c_fact"], 2),
            "b2c_plan":     round(b["b2c_plan"], 2),
            "b2c_variance": b2c_var,
            "b2b_fact":     round(b["b2b_fact"], 2),
            "b2b_plan":     round(b["b2b_plan"], 2),
            "b2b_variance": b2b_var,
            "burn_fact":    round(b["burn_fact"], 2),
            "burn_plan":    round(b["burn_plan"], 2),
            "burn_variance": burn_var,  # positive = overspending burn
            "cogs_fact":    round(b["cogs_fact"], 2),
            "cogs_plan":    round(b["cogs_plan"], 2),
            "cogs_variance": cogs_var,
            "mktg_plan":    round(b["mktg_plan"], 2),
            "pf_burn_fact": round(b["pf_burn_fact"], 2),
            "pf_burn_plan": round(b["pf_burn_plan"], 2),
            "pf_burn_variance": pf_var,
        })

    # Top-5 biggest variances by absolute impact (b2c/b2b + burn + cogs
    # + pf) — these are the "where did we drift" bullets the operator
    # reads first.
    biggest: List[Dict[str, Any]] = []
    for w in weeks_out:
        for metric, label in (
            ("b2c_variance", "B2C revenue"),
            ("b2b_variance", "B2B revenue"),
            ("burn_variance", "A2A burn"),
            ("cogs_variance", "A2A COGS"),
            ("pf_burn_variance", "Portfolio burn"),
        ):
            v = w[metric]
            if v == 0:
                continue
            biggest.append({
                "week_start": w["week_start"],
                "metric":     label,
                "variance":   v,
                "abs":        abs(v),
            })
    biggest.sort(key=lambda x: x["abs"], reverse=True)
    biggest = biggest[:5]

    # Window totals (plan vs fact) — power the KPI strip on top.
    def _t(k):
        return round(sum(w[k] for w in weeks_out), 2)
    totals_out = {
        "b2c_fact":  _t("b2c_fact"),  "b2c_plan":  _t("b2c_plan"),
        "b2b_fact":  _t("b2b_fact"),  "b2b_plan":  _t("b2b_plan"),
        "burn_fact": _t("burn_fact"), "burn_plan": _t("burn_plan"),
        "cogs_fact": _t("cogs_fact"), "cogs_plan": _t("cogs_plan"),
        "mktg_plan": _t("mktg_plan"),
        "pf_burn_fact": _t("pf_burn_fact"),
        "pf_burn_plan": _t("pf_burn_plan"),
    }
    # Total inflow / outflow rollups
    inflow_fact  = round(totals_out["b2c_fact"] + totals_out["b2b_fact"], 2)
    inflow_plan  = round(totals_out["b2c_plan"] + totals_out["b2b_plan"], 2)
    outflow_fact = round(abs(totals_out["burn_fact"]) + abs(totals_out["cogs_fact"])
                          + abs(totals_out["pf_burn_fact"]), 2)
    outflow_plan = round(abs(totals_out["burn_plan"]) + abs(totals_out["cogs_plan"])
                          + abs(totals_out["pf_burn_plan"]) + abs(totals_out["mktg_plan"]), 2)

    return {
        "window_start": listing["window_start"],
        "window_end":   listing["window_end"],
        "today_monday": listing["today_monday"],
        "weeks": weeks_out,
        "totals": totals_out,
        "rollup": {
            "inflow_fact":  inflow_fact,
            "inflow_plan":  inflow_plan,
            "inflow_variance": round(inflow_fact - inflow_plan, 2),
            "outflow_fact": outflow_fact,
            "outflow_plan": outflow_plan,
            "outflow_variance": round(outflow_fact - outflow_plan, 2),
            "net_fact":     round(inflow_fact - outflow_fact, 2),
            "net_plan":     round(inflow_plan - outflow_plan, 2),
            "net_variance": round((inflow_fact - outflow_fact)
                                   - (inflow_plan - outflow_plan), 2),
        },
        "biggest_variances": biggest,
        "row_count": len(rows),
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
        if existing:
            # 2026-07-08 (H4) — the old UPDATE always SET all 17 numeric
            # columns (cleaned_fields.get(f) -> None for omitted keys), so
            # editing a single field wiped the other 16. Only SET the keys
            # actually supplied this call, plus metadata. Numeric columns
            # not in `fields` keep their stored value.
            update_params: Dict[str, Any] = {
                "row_type_v":  row_type,
                "source":      source or "manual",
                "updated_at":  now,
                "updated_by":  by,
            }
            set_parts = ["row_type = :row_type_v", "source = :source",
                         "updated_at = :updated_at", "updated_by = :updated_by"]
            # week_label / note: update only when explicitly provided.
            if week_label is not None:
                update_params["week_label"] = week_label
                set_parts.append("week_label = :week_label")
            if note is not None:
                update_params["note"] = note
                set_parts.append("note = :note")
            for f in cleaned_fields:  # only the numeric keys the caller sent
                update_params[f] = cleaned_fields[f]
                set_parts.append(f"{f} = :{f}")
            update_params["_id"] = existing["id"]
            conn.execute(
                "UPDATE cashflow_weekly SET " + ", ".join(set_parts) + " WHERE id = :_id",
                update_params,
            )
            row_id = existing["id"]
        else:
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


# ────────────────────────────────────────────────────────────────────────
# TSV import (2026-06-29) — paste from operator's Google Sheet.
#
# The operator's UNIFIED CASH TIMELINE has many spreadsheet-isms in its
# data (€ prefix, thousand-separators, (parentheses) for negatives, blank
# cells, double-hyphen as separator). This importer normalises all of
# that into the cashflow_weekly schema, batches as forecast/estimate/plug
# rows, and returns a structured summary so the operator sees exactly
# what landed without opening a SQL client.
#
# Mapping is positional by header — the first header row in the pasted
# block MUST contain at least: 'Period', 'End Date', 'Type' (case-
# insensitive, leading/trailing spaces tolerated). Any unknown column
# names are recorded in the result's `unknown_columns` list, not
# silently dropped — so we never lose a number without a warning.
# ────────────────────────────────────────────────────────────────────────

# Operator-friendly headers (lowercase, accents stripped) → DB column.
# Multiple labels can map to the same column (sheet drift tolerance).
_HEADER_MAP = {
    # Identity
    "period":            "_period",         # used to build week_label
    "end date":          "_end_date",       # parsed → week_start (ISO Monday)
    "type":              "_row_type",       # actual | forecast | estimate | plug
    # Revenue
    "b2c revenue fact":  "b2c_revenue_fact",
    "b2c revenue plan":  "b2c_revenue_plan",
    "b2b revenue fact":  "b2b_revenue_fact",
    "b2b revenue plan":  "b2b_revenue_plan",
    "hold back":         "holdback",
    "holdback":          "holdback",
    # Financing
    "financing inflows":  "financing_inflow",
    "financing inflow":   "financing_inflow",
    "financing outflow":  "financing_outflow",
    "financing outflows": "financing_outflow",
    # A2A
    "a2a burn fact":     "a2a_burn_fact",
    "a2a burn plan":     "a2a_burn_plan",
    "marketing-plan":    "marketing_plan",
    "marketing plan":    "marketing_plan",
    "a2a cogs - fact":   "a2a_cogs_fact",
    "a2a cogs - plan":   "a2a_cogs_plan",
    "a2a cogs fact":     "a2a_cogs_fact",
    "a2a cogs plan":     "a2a_cogs_plan",
    "a2a cogs - ap":     "a2a_cogs_ap",
    "a2a cogs ap":       "a2a_cogs_ap",
    "a2a cogs ap (dep/ bon)": "a2a_cogs_ap",
    # Holding / portfolio
    "holding royalty":   "holding_royalty",
    "portfolio burn fact": "portfolio_burn_fact",
    "portfolio burn plan": "portfolio_burn_plan",
    "portfolio inflows":   "portfolio_inflows",
    # Other / catch-all + computed
    "outstanding ap/ intercompany/ other": "outstanding_ap_intercompany",
    "outstanding ap":                       "outstanding_ap_intercompany",
    "net":               "net_eur",
    "balance (eop)":     "balance_eop_eur",
    "balance eop":       "balance_eop_eur",
}

_ROW_TYPE_MAP = {
    "actual": "actual", "factual": "actual", "fact": "actual",
    "forecast": "forecast", "fcst": "forecast",
    "estimate": "estimate", "est": "estimate",
    "plug": "plug",
}


def _norm_header(h: str) -> str:
    """Lower-case + collapse whitespace + drop wrapping quotes."""
    return " ".join((h or "").replace("\"", "").strip().lower().split())


def parse_amount(raw: Any) -> Optional[float]:
    """Parse a Google-Sheet cell into float or None.

    Tolerates: '€1,234.56' → 1234.56  ·  '(€500)' → -500  ·  '-' → None
    ·  '' → None  ·  '#REF!' → None  ·  '$' → None  ·  ' '  → None.

    Never raises — bad input becomes None so the importer can still
    upsert the rest of the row (operator gets a 'skipped_cells' count
    in the result instead of a 500 error).
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s or s in ("-", "—", "–", "#REF!", "#N/A", "N/A"):
        return None
    # Detect parenthesised negative: (€1,234) → -1234
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    # Drop currency symbols and thousand separators
    for ch in ("€", "$", "£", "¥", "₽", " ", " "):
        s = s.replace(ch, "")
    s = s.replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if negative else v


def _coerce_end_date_to_monday(raw: Any) -> Optional[str]:
    """The Sheet's 'End Date' column is the Sunday at the END of a week
    (e.g. 4/5/2025 means W ending May 4). We anchor to the ISO Monday
    that starts the week containing that end-date, so cashflow_weekly's
    week_start is consistent regardless of how the operator labelled
    their column."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Try a few common spreadsheet date formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y",
                "%Y/%m/%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            break
        except ValueError:
            continue
    else:
        return None
    return monday_of(d)


def import_tsv(text: str, *,
                default_row_type: str = "forecast",
                by: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, Any]:
    """Parse a TSV / CSV paste and upsert each row.

    Returns:
      {
        rows_seen, rows_imported, rows_skipped,
        skipped_examples: [...],         # first 5 with reason
        unknown_columns: [...],          # headers we didn't map
        rows: [{week_start, row_type, …upserted payload}, …],
        dry_run: bool,
      }
    """
    if not text or not isinstance(text, str):
        raise ValueError("text is required")
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        return {"rows_seen": 0, "rows_imported": 0, "rows_skipped": 0,
                "skipped_examples": [], "unknown_columns": [],
                "rows": [], "dry_run": dry_run}

    # Detect separator — operators paste from Sheets (TAB) or download
    # as CSV (comma). Pick the one with more occurrences on the first line.
    # 2026-06-30 — for comma-separated input use the csv module so quoted
    # cells like `"€41,500"` don't split on the embedded comma. TSV rarely
    # has quoted cells but csv handles both dialects safely.
    import csv as _csv
    import io as _io
    first = lines[0]
    sep = "\t" if first.count("\t") >= first.count(",") and "\t" in first else ","
    reader = _csv.reader(_io.StringIO("\n".join(lines)), delimiter=sep)
    parsed_rows = list(reader)
    if not parsed_rows:
        return {"rows_seen": 0, "rows_imported": 0, "rows_skipped": 0,
                "skipped_examples": [], "unknown_columns": [],
                "rows": [], "dry_run": dry_run}
    # 2026-07-01 — operators export sheets with a decorative preamble
    # (title / snapshot / KPIs) BEFORE the real header row. e.g. the
    # Amitours "Cash Timeline" CSV has 5 rows of preamble then the header
    # 'Period,End Date,Type,...' on row 6. Auto-detect the header row by
    # scanning up to the first 20 rows and picking the first one whose
    # normalised headers cover BOTH required fields.
    required = {"_end_date", "_row_type"}
    header_row_idx = 0
    mapped: Dict[int, str] = {}
    unknown: List[str] = []
    for candidate_idx in range(min(20, len(parsed_rows))):
        cand_raw = parsed_rows[candidate_idx]
        cand_headers = [_norm_header(h) for h in cand_raw]
        cand_mapped: Dict[int, str] = {}
        cand_unknown: List[str] = []
        for idx, h in enumerate(cand_headers):
            if not h:
                continue
            if h in _HEADER_MAP:
                cand_mapped[idx] = _HEADER_MAP[h]
            else:
                cand_unknown.append(cand_raw[idx].strip() or f"col{idx}")
        if required.issubset(set(cand_mapped.values())):
            header_row_idx = candidate_idx
            mapped = cand_mapped
            unknown = cand_unknown
            break
    if not required.issubset(set(mapped.values())):
        # Report what the FIRST row actually looked like so the operator
        # can eyeball the mismatch, not the empty result of a preamble scan.
        first_headers = [_norm_header(h) for h in parsed_rows[0]]
        first_mapped = sorted({_HEADER_MAP[h] for h in first_headers if h in _HEADER_MAP})
        raise ValueError(
            "missing required header(s); need 'End Date' + 'Type'. "
            f"Got: {first_mapped} (scanned {min(20, len(parsed_rows))} rows "
            "for a matching header)."
        )
    headers_raw = parsed_rows[header_row_idx]
    # Skip past the header row for the data-loop below
    parsed_rows = parsed_rows[header_row_idx:]

    rows_seen = 0
    rows_imported = 0
    skipped: List[Dict[str, Any]] = []
    upserted_rows: List[Dict[str, Any]] = []

    # ln_no reflects the real 1-indexed CSV line so skip-example
    # messages point the operator at the right row in their source file.
    for ln_no, cells in enumerate(parsed_rows[1:], start=header_row_idx + 2):
        rows_seen += 1
        raw_line = sep.join(cells)  # reconstructed for skip-example display
        rec: Dict[str, Any] = {}
        for idx, col in mapped.items():
            if idx >= len(cells):
                continue
            cell = cells[idx]
            if col == "_row_type":
                rec[col] = _ROW_TYPE_MAP.get(_norm_header(cell), _norm_header(cell))
            elif col == "_end_date":
                rec[col] = _coerce_end_date_to_monday(cell)
            elif col == "_period":
                rec[col] = cell.strip()
            else:
                rec[col] = parse_amount(cell)
        # Validate
        if not rec.get("_end_date"):
            if len(skipped) < 5:
                skipped.append({"line": ln_no, "reason": "unparseable end-date",
                                 "row": raw_line[:120]})
            continue
        row_type = rec.get("_row_type") or default_row_type
        if row_type not in WRITABLE_ROW_TYPES:
            # Sheets often have row_type='actual' — we skip those here
            # (use derive_actuals() to compute actual rows from source data).
            if len(skipped) < 5:
                skipped.append({"line": ln_no,
                                 "reason": f"row_type={row_type!r} is derived; "
                                            f"use derive_actuals() instead",
                                 "row": raw_line[:120]})
            continue
        fields = {k: v for k, v in rec.items()
                  if k in {f for g in FIELD_GROUPS.values() for f in g}}
        # Skip rows where every numeric cell is empty
        if not any(v is not None for v in fields.values()):
            if len(skipped) < 5:
                skipped.append({"line": ln_no, "reason": "all numeric cells empty",
                                 "row": raw_line[:120]})
            continue
        if dry_run:
            upserted_rows.append({
                "week_start": rec["_end_date"],
                "row_type": row_type,
                "week_label": rec.get("_period"),
                "fields": fields,
                "preview": True,
            })
            rows_imported += 1
            continue
        try:
            payload = upsert_row(
                week_start=rec["_end_date"],
                row_type=row_type,
                fields=fields,
                week_label=rec.get("_period"),
                source="import:tsv",
                by=by,
            )
            upserted_rows.append(payload)
            rows_imported += 1
        except ValueError as exc:
            if len(skipped) < 5:
                skipped.append({"line": ln_no, "reason": str(exc),
                                 "row": raw_line[:120]})
    return {
        "rows_seen":      rows_seen,
        "rows_imported":  rows_imported,
        "rows_skipped":   rows_seen - rows_imported,
        "skipped_examples": skipped,
        "unknown_columns":  unknown,
        "rows":           upserted_rows,
        "dry_run":        dry_run,
        "separator":      "tab" if sep == "\t" else "comma",
    }


# ────────────────────────────────────────────────────────────────────────
# Actuals derivation (2026-06-29) — compute row_type='actual' rows from
# source data: revenue_receipts (cash-in by received_at) + documents
# (paid expense by payment_executed_at, status in paid/posted) + the
# latest bank_account_balances snapshot per week as balance_eop. Idempo-
# tent: each call DELETEs prior 'actual' rows in the window and rebuilds.
# ────────────────────────────────────────────────────────────────────────

# 2026-07-08 (H10) — canonical POSTED set (posted, paid) from db.py.
_PAID_EXPENSE_STATUSES = db.POSTED_STATUSES


def derive_actuals(weeks_before: int = 26,
                    weeks_after: int = 0,
                    by: Optional[str] = None) -> Dict[str, Any]:
    """Recompute 'actual' rows for the past N weeks (default 26 = ~6 mo).

    Returns: {window_start, window_end, weeks_rebuilt, rows: [...]}
    Idempotent — wipes prior actuals in window first.
    """
    today_monday = datetime.strptime(monday_of(), "%Y-%m-%d").date()
    start = today_monday - timedelta(days=7 * weeks_before)
    end = today_monday + timedelta(days=7 * weeks_after)
    start_s = start.strftime("%Y-%m-%d")
    end_s = end.strftime("%Y-%m-%d")

    conn = db.get_connection()
    try:
        # Wipe prior actuals in window — commit immediately so the
        # following per-week INSERTs don't hit the old rows on UNIQUE.
        conn.execute(
            "DELETE FROM cashflow_weekly "
            "WHERE row_type = 'actual' AND week_start >= ? AND week_start <= ?",
            (start_s, end_s),
        )
        conn.commit()

        # B2C cash-in per week (revenue_receipts.received_at)
        rev_rows = conn.execute(
            "SELECT received_at, amount_eur FROM revenue_receipts "
            "WHERE received_at IS NOT NULL "
            "AND substr(received_at, 1, 10) >= ? "
            "AND substr(received_at, 1, 10) <= ?",
            (start_s, end_s),
        ).fetchall()

        # Paid expense per week (documents.payment_executed_at)
        exp_rows = conn.execute(
            "SELECT payment_executed_at, amount, profit_center "
            "FROM documents "
            "WHERE payment_executed_at IS NOT NULL "
            "AND status IN (" + ",".join("?" for _ in _PAID_EXPENSE_STATUSES) + ") "
            "AND substr(payment_executed_at, 1, 10) >= ? "
            "AND substr(payment_executed_at, 1, 10) <= ?",
            tuple(_PAID_EXPENSE_STATUSES) + (start_s, end_s),
        ).fetchall()

        # Latest bank balance per week (for balance_eop_eur)
        bal_rows = conn.execute(
            "SELECT pc, balance_eur, as_of_date FROM bank_account_balances "
            "WHERE as_of_date >= ? AND as_of_date <= ? "
            "ORDER BY as_of_date ASC",
            (start_s, end_s),
        ).fetchall()
    finally:
        conn.close()

    # Bucket by ISO Monday week
    weeks: Dict[str, Dict[str, float]] = {}

    def _bucket(d_iso: str) -> Optional[str]:
        try:
            d = datetime.strptime(d_iso[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return monday_of(d)

    for r in rev_rows:
        wk = _bucket(r["received_at"])
        if not wk:
            continue
        b = weeks.setdefault(wk, {})
        b["b2c_revenue_fact"] = b.get("b2c_revenue_fact", 0.0) + float(r["amount_eur"] or 0)

    # Operator's sheet uses split a2a vs portfolio fact lines. Without an
    # explicit "is this an A2A expense" tag on documents, treat profit_center
    # == 'AA' as A2A burn; everything else as portfolio burn. Keeps the
    # derivation explainable until a richer tagging lands.
    for r in exp_rows:
        wk = _bucket(r["payment_executed_at"])
        if not wk:
            continue
        b = weeks.setdefault(wk, {})
        amt = -float(r["amount"] or 0)  # expenses are negative in the timeline
        pc = (r["profit_center"] or "").strip().upper()
        if pc == "AA":
            b["a2a_burn_fact"] = b.get("a2a_burn_fact", 0.0) + amt
        else:
            b["portfolio_burn_fact"] = b.get("portfolio_burn_fact", 0.0) + amt

    # Group balances by week (LAST snapshot wins)
    bal_by_week: Dict[str, float] = {}
    for r in bal_rows:
        wk = _bucket(r["as_of_date"])
        if not wk:
            continue
        # sum balances across all PC snapshots for that week
        bal_by_week[wk] = bal_by_week.get(wk, 0.0) + float(r["balance_eur"] or 0)

    # Upsert one actual row per week we have data for
    rows_out = []
    for wk in sorted(weeks.keys() | bal_by_week.keys()):
        fields = weeks.get(wk, {}).copy()
        if wk in bal_by_week:
            fields["balance_eop_eur"] = round(bal_by_week[wk], 2)
        # Compute net = sum(positive) - sum(abs(negative)) → here we just
        # add everything since burns are already signed negative.
        net = sum(v for v in fields.values()
                  if isinstance(v, (int, float))
                  and v is not None)
        # Exclude balance_eop_eur from net (it's a snapshot, not a flow)
        if "balance_eop_eur" in fields:
            net -= fields["balance_eop_eur"]
        fields["net_eur"] = round(net, 2)
        # Bypass upsert_row's WRITABLE check by writing directly
        now = datetime.utcnow().isoformat()
        params = {
            "week_start": wk,
            "week_label": None,
            "row_type":   "actual",
            "note":       None,
            "source":     "derived",
            "updated_at": now,
            "updated_by": by or "derive_actuals",
            **{f: None for f in _ALL_NUMERIC_FIELDS},
            **{k: round(v, 2) if isinstance(v, float) else v
                for k, v in fields.items()
                if k in _ALL_NUMERIC_FIELDS},
        }
        conn = db.get_connection()
        try:
            cols = ", ".join(params.keys())
            slots = ", ".join(f":{k}" for k in params.keys())
            conn.execute(
                f"INSERT INTO cashflow_weekly ({cols}) VALUES ({slots})",
                params,
            )
            conn.commit()
        finally:
            conn.close()
        rows_out.append({"week_start": wk, "row_type": "actual", "fields": {
            k: v for k, v in fields.items() if k in _ALL_NUMERIC_FIELDS
        }})

    return {
        "window_start":   start_s,
        "window_end":     end_s,
        "weeks_rebuilt":  len(rows_out),
        "rows":           rows_out,
    }


# ────────────────────────────────────────────────────────────────────────
# Google Sheet URL → import (2026-06-30).
#
# Operator wanted "Chrome MCP" integration to skip the copy-paste step.
# The realistic shape: accept a published Google Sheet URL, fetch the
# sheet's TSV export, run the same import_tsv pipeline. No new auth
# in v1 — only PUBLISHED sheets (File → Share → Anyone with the link
# can view) are reachable from the server.
#
# Supported URL forms (all coerce to /export?format=tsv&gid=N):
#   1. https://docs.google.com/spreadsheets/d/<KEY>/edit#gid=<GID>
#   2. https://docs.google.com/spreadsheets/d/<KEY>/edit?gid=<GID>
#   3. https://docs.google.com/spreadsheets/d/<KEY>/edit
#      (no gid → defaults to gid=0, the first tab)
#   4. Already-exported …/export?format=tsv|csv&gid=… URL (passes through)
#
# Returns the same payload as import_tsv plus 'source_url' for the
# operator's audit-log breadcrumb.
# ────────────────────────────────────────────────────────────────────────

_GS_KEY_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_GS_GID_RE = re.compile(r"[#&?]gid=(\d+)")


def gsheet_url_to_export_url(url: str, *, fmt: str = "tsv") -> str:
    """Turn any Google Sheets URL the operator can paste into a clean
    server-fetchable export URL. Raises ValueError if the URL isn't
    recognisable as a Sheets link."""
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    url = url.strip()
    if not url:
        raise ValueError("url is required")
    # Already an export URL → trust the format param the caller chose
    if "/export" in url and ("format=tsv" in url or "format=csv" in url):
        return url
    m = _GS_KEY_RE.search(url)
    if not m:
        raise ValueError(
            "URL does not look like a Google Sheets link. Expected "
            "'https://docs.google.com/spreadsheets/d/<KEY>/edit...'"
        )
    key = m.group(1)
    gid_m = _GS_GID_RE.search(url)
    gid = gid_m.group(1) if gid_m else "0"
    if fmt not in ("tsv", "csv"):
        fmt = "tsv"
    return (
        f"https://docs.google.com/spreadsheets/d/{key}/export?format={fmt}&gid={gid}"
    )


def import_from_gsheet_url(url: str, *,
                            default_row_type: str = "forecast",
                            by: Optional[str] = None,
                            dry_run: bool = False,
                            fmt: str = "tsv",
                            timeout: float = 12.0) -> Dict[str, Any]:
    """Fetch a published Google Sheet and import it via import_tsv.

    Errors surface the exact failure mode (URL not a Sheet · sheet not
    published · network/timeout · sheet empty) so the operator never sees
    a generic 500.
    """
    import urllib.request
    import urllib.error

    export_url = gsheet_url_to_export_url(url, fmt=fmt)
    req = urllib.request.Request(
        export_url,
        headers={"User-Agent": "Keel/1.0 (cashflow importer)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            content_type = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise ValueError(
                "Sheet not publicly accessible. In Google Sheets: "
                "File → Share → 'Anyone with the link can view', then retry."
            )
        raise ValueError(f"fetch failed: HTTP {exc.code} on {export_url}")
    except urllib.error.URLError as exc:
        raise ValueError(f"network error fetching the Sheet: {exc.reason}")
    except TimeoutError:
        raise ValueError(f"Sheet fetch timed out after {timeout}s")

    # If Google bounced us to an HTML login page, content-type will be html
    if "text/html" in content_type:
        raise ValueError(
            "Sheet returned an HTML page (probably a sign-in redirect). "
            "Confirm the sheet is published with 'Anyone with the link'."
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="ignore")
    if not text.strip():
        raise ValueError("Sheet fetched OK but is empty (0 rows).")

    out = import_tsv(text=text, default_row_type=default_row_type,
                      by=by, dry_run=dry_run)
    out["source_url"] = url
    out["export_url"] = export_url
    out["fetched_bytes"] = len(raw)
    return out
