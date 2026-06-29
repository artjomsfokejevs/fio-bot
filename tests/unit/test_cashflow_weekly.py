"""Tests for services.cashflow_weekly — weekly cashflow timeline (2026-06-29).

Covers: monday_of(), upsert/delete contract (writable types only),
list_weeks() window math, totals() aggregation.
"""
from __future__ import annotations

from datetime import date

import pytest

from services import db, cashflow_weekly as cw


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM cashflow_weekly WHERE source = 'pytest'")
        conn.commit()
    finally:
        conn.close()
    yield


def test_monday_of_known_dates():
    # 2026-06-29 is a Monday
    assert cw.monday_of(date(2026, 6, 29)) == "2026-06-29"
    # 2026-07-03 (Friday) → 2026-06-29 Monday
    assert cw.monday_of(date(2026, 7, 3)) == "2026-06-29"
    # 2026-07-05 (Sunday) → 2026-06-29 Monday
    assert cw.monday_of(date(2026, 7, 5)) == "2026-06-29"


def test_writable_row_types_excludes_actual():
    assert "actual" not in cw.WRITABLE_ROW_TYPES
    for t in ("forecast", "estimate", "plug"):
        assert t in cw.WRITABLE_ROW_TYPES


def test_upsert_forecast_then_update_in_place():
    rec = cw.upsert_row(
        week_start="2026-06-29", row_type="forecast",
        fields={"b2c_revenue_plan": 50000, "a2a_burn_plan": -30000},
        week_label="W26", source="pytest", by="pytest",
    )
    assert rec["row_type"] == "forecast"
    assert rec["fields"]["b2c_revenue_plan"] == 50000.0
    # Re-upsert same (week_start, row_type) — should UPDATE, not insert a 2nd
    rec2 = cw.upsert_row(
        week_start="2026-06-29", row_type="forecast",
        fields={"b2c_revenue_plan": 75000},
        source="pytest", by="pytest",
    )
    assert rec2["id"] == rec["id"]
    assert rec2["fields"]["b2c_revenue_plan"] == 75000.0


def test_upsert_refuses_actual_row_type():
    with pytest.raises(ValueError, match="not writable"):
        cw.upsert_row(week_start="2026-06-29", row_type="actual",
                       fields={"b2c_revenue_fact": 10000})


def test_upsert_refuses_invalid_week_start():
    with pytest.raises(ValueError, match="ISO YYYY-MM-DD"):
        cw.upsert_row(week_start="2026/06/29", row_type="forecast", fields={})


def test_upsert_ignores_unknown_fields():
    """Defensive: unknown keys in fields → ignored, no crash."""
    rec = cw.upsert_row(
        week_start="2026-06-29", row_type="forecast",
        fields={"b2c_revenue_plan": 1000, "totally_made_up_field": "junk"},
        source="pytest", by="pytest",
    )
    assert "totally_made_up_field" not in rec["fields"]
    assert rec["fields"]["b2c_revenue_plan"] == 1000.0


def test_delete_row_writable_only():
    cw.upsert_row(
        week_start="2026-06-29", row_type="estimate",
        fields={"b2c_revenue_plan": 1234}, source="pytest", by="pytest",
    )
    assert cw.delete_row("2026-06-29", "estimate") is True
    assert cw.delete_row("2026-06-29", "estimate") is False  # gone


def test_delete_refuses_actual():
    with pytest.raises(ValueError, match="not deletable"):
        cw.delete_row("2026-06-29", "actual")


def test_list_weeks_returns_window_metadata():
    out = cw.list_weeks(weeks_before=4, weeks_after=8)
    # Window bounds are 12 weeks apart (84 days)
    assert "window_start" in out and "window_end" in out
    assert out["today_monday"] == cw.monday_of()
    assert out["count"] == len(out["rows"])


def test_totals_sums_writable_fields():
    cw.upsert_row(
        week_start="2026-06-29", row_type="forecast",
        fields={"b2c_revenue_plan": 50000, "a2a_burn_plan": -30000},
        source="pytest", by="pytest",
    )
    cw.upsert_row(
        week_start="2026-07-06", row_type="forecast",
        fields={"b2c_revenue_plan": 60000, "a2a_burn_plan": -35000},
        source="pytest", by="pytest",
    )
    t = cw.totals(weeks_before=2, weeks_after=4)
    assert t["b2c_revenue_plan"] == pytest.approx(110000.0)
    assert t["a2a_burn_plan"] == pytest.approx(-65000.0)
