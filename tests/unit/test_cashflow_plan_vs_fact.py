"""Plan-vs-fact overlay on 13-week projection + Weekly Timeline summary.

Covers task #156 (2026-07-01):
  · cashflow_projection.plan_by_week() reads planning rows and buckets
    them into ar / ap / net per Monday-anchored week.
  · project() series carries plan_ar_in / plan_ap_out / plan_net /
    plan_running_balance + fact-minus-plan variances per week.
  · cashflow_weekly.plan_vs_fact_summary() returns per-week facts, plan,
    variance for the KPI card, plus a top-5 biggest-variance list.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services import (db, cashflow_projection as cp, cashflow_weekly as cw)


def _monday(d: datetime) -> str:
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _cleanup():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM cashflow_weekly WHERE source LIKE 'test:%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM cashflow_weekly WHERE source LIKE 'test:%'")
        conn.commit()
    finally:
        conn.close()


def _seed(week_start, row_type, **fields):
    if row_type in cw.WRITABLE_ROW_TYPES:
        cw.upsert_row(
            week_start=week_start,
            row_type=row_type,
            fields=fields,
            source="test:plan_vs_fact",
            by="pytest",
        )
        return
    # 'actual' rows are gated by upsert_row (derived, not writable). For
    # tests we insert directly so we can seed a fact-vs-plan comparison
    # without hitting the derive_actuals() pipeline.
    now = datetime.utcnow().isoformat()
    cols = ["week_start", "row_type", "source", "updated_at"] + list(fields.keys())
    vals = [week_start, row_type, "test:plan_vs_fact", now] + list(fields.values())
    ph = ",".join("?" for _ in cols)
    conn = db.get_connection()
    try:
        conn.execute(
            f"INSERT INTO cashflow_weekly ({','.join(cols)}) VALUES ({ph})",
            tuple(vals),
        )
        conn.commit()
    finally:
        conn.close()


def test_plan_by_week_aggregates_planning_rows():
    w1 = _monday(datetime.utcnow())
    w2 = _monday(datetime.utcnow() + timedelta(days=7))
    _seed(w1, "forecast", b2c_revenue_plan=100_000, a2a_burn_plan=-40_000,
          marketing_plan=-10_000, financing_inflow=20_000)
    _seed(w1, "plug", outstanding_ap_intercompany=-5_000)
    _seed(w2, "estimate", b2b_revenue_plan=50_000, a2a_cogs_plan=-30_000)
    out = cp.plan_by_week([w1, w2, _monday(datetime.utcnow() + timedelta(days=14))])
    # w1 AR = 100k (b2c) + 20k (fin_in) = 120k; AP = 40k + 10k + 5k = 55k
    assert out[w1]["ar"] == 120_000.0
    assert out[w1]["ap"] == 55_000.0
    assert out[w1]["net"] == 65_000.0
    assert out[w1]["row_count"] == 2
    # w2 AR = 50k; AP = 30k
    assert out[w2]["ar"] == 50_000.0
    assert out[w2]["ap"] == 30_000.0


def test_project_series_carries_plan_and_variance():
    w1 = _monday(datetime.utcnow())
    _seed(w1, "forecast", b2c_revenue_plan=200_000, a2a_burn_plan=-100_000)
    result = cp.project(weeks=4, opening_override=1_000_000.0)
    assert result["series"][0]["week_start"] == w1
    row0 = result["series"][0]
    # No AR/AP docs seeded → fact 0. Plan carries through.
    assert row0["plan_ar_in"] == 200_000.0
    assert row0["plan_ap_out"] == 100_000.0
    assert row0["plan_net"] == 100_000.0
    # Fact 0 − plan 100k = −100k net variance
    assert row0["net_variance"] == -100_000.0
    # Plan running: 1M + 100k = 1.1M
    assert row0["plan_running_balance"] == 1_100_000.0
    assert "ending_balance_plan_eur" in result


def test_plan_vs_fact_summary_returns_totals_and_biggest():
    # Use next week's Monday so the cleanup+seed doesn't collide with
    # tests that also seed the current-week row (unique index is on
    # (week_start, row_type) — same week + same row_type would conflict).
    w1 = _monday(datetime.utcnow() + timedelta(days=7))
    _seed(w1, "actual",   b2c_revenue_fact=180_000, a2a_burn_fact=-90_000)
    _seed(w1, "forecast", b2c_revenue_plan=200_000, a2a_burn_plan=-100_000)
    summary = cw.plan_vs_fact_summary(weeks_before=1, weeks_after=1)
    weeks = {w["week_start"]: w for w in summary["weeks"]}
    assert w1 in weeks
    wk = weeks[w1]
    assert wk["b2c_fact"] == 180_000.0
    assert wk["b2c_plan"] == 200_000.0
    assert wk["b2c_variance"] == -20_000.0
    # burn: fact 90k vs plan 100k → we underspent by 10k
    assert wk["burn_variance"] == -10_000.0
    # Biggest variances should surface b2c 20k (the largest abs delta)
    biggest = summary["biggest_variances"]
    assert any(b["metric"] == "B2C revenue" and b["abs"] == 20_000.0 for b in biggest)
    # Rollup: inflow variance = 180k − 200k = −20k
    assert summary["rollup"]["inflow_variance"] == -20_000.0


def test_empty_plan_returns_zero_overlay_not_error():
    result = cp.project(weeks=3, opening_override=500_000.0)
    for row in result["series"]:
        assert row["plan_ar_in"] == 0.0
        assert row["plan_ap_out"] == 0.0
        assert row["plan_net"] == 0.0
        assert row["plan_row_count"] == 0
