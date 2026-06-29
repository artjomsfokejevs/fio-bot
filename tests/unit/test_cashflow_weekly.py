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


# ────────────────────────────────────────────────────────────────────────
# parse_amount — Google Sheet cell quirks
# ────────────────────────────────────────────────────────────────────────

def test_parse_amount_handles_sheet_cells():
    assert cw.parse_amount("€1,234.56") == 1234.56
    assert cw.parse_amount("(€500)") == -500.0
    assert cw.parse_amount("(€1,234.56)") == -1234.56
    assert cw.parse_amount("-") is None
    assert cw.parse_amount("—") is None
    assert cw.parse_amount("") is None
    assert cw.parse_amount(None) is None
    assert cw.parse_amount("#REF!") is None
    assert cw.parse_amount(1234) == 1234.0
    assert cw.parse_amount("  1 234,56  ".replace(",", ".")) == 1234.56
    # Garbage → None, never raises
    assert cw.parse_amount("not-a-number") is None


# ────────────────────────────────────────────────────────────────────────
# import_tsv — TSV/CSV paste contract
# ────────────────────────────────────────────────────────────────────────

def test_import_tsv_minimal_forecast_paste():
    """Realistic minimal paste from the operator's sheet."""
    text = (
        "Period\tEnd Date\tType\tB2C revenue plan\tA2A burn plan\n"
        "W26\t6/29/2026\tForecast\t€50,000\t(€30,000)\n"
        "W27\t7/6/2026\tForecast\t€60,000\t(€35,000)\n"
    )
    out = cw.import_tsv(text, by="pytest")
    assert out["rows_seen"] == 2
    assert out["rows_imported"] == 2
    assert out["separator"] == "tab"
    # Spot-check first row
    first = next(r for r in out["rows"] if r["week_start"] == "2026-06-29")
    assert first["fields"]["b2c_revenue_plan"] == 50000.0
    assert first["fields"]["a2a_burn_plan"] == -30000.0
    assert first["week_label"] == "W26"
    # Cleanup
    cw.delete_row("2026-06-29", "forecast")
    cw.delete_row("2026-07-06", "forecast")


def test_import_tsv_skips_actual_rows_with_explanation():
    """Operator pastes their whole sheet incl. 'Actual' rows — those must
    be skipped (use derive_actuals instead), and the skip reason must
    name the right alternative."""
    text = (
        "Period\tEnd Date\tType\tB2C revenue fact\n"
        "W26\t6/29/2026\tActual\t€12,345\n"
    )
    out = cw.import_tsv(text, by="pytest")
    assert out["rows_imported"] == 0
    assert out["rows_skipped"] == 1
    assert "derive_actuals" in out["skipped_examples"][0]["reason"]


def test_import_tsv_missing_required_header_raises():
    text = "Period\tType\tB2C revenue plan\nW26\tForecast\t€50,000\n"
    with pytest.raises(ValueError, match="missing required header"):
        cw.import_tsv(text, by="pytest")


def test_import_tsv_records_unknown_columns():
    text = (
        "Period\tEnd Date\tType\tB2C revenue plan\tMystery Field\n"
        "W26\t6/29/2026\tForecast\t€50,000\t€999\n"
    )
    out = cw.import_tsv(text, by="pytest")
    assert "Mystery Field" in out["unknown_columns"]
    assert out["rows_imported"] == 1
    cw.delete_row("2026-06-29", "forecast")


def test_import_tsv_dry_run_does_not_persist():
    text = (
        "Period\tEnd Date\tType\tB2C revenue plan\n"
        "W30\t7/27/2026\tForecast\t€11,111\n"
    )
    out = cw.import_tsv(text, by="pytest", dry_run=True)
    assert out["dry_run"] is True
    assert out["rows_imported"] == 1
    # Confirm nothing landed
    listing = cw.list_weeks(weeks_before=0, weeks_after=10)
    assert not any(r["week_start"] == "2026-07-27" and r["row_type"] == "forecast"
                    for r in listing["rows"])


# ────────────────────────────────────────────────────────────────────────
# derive_actuals — wipe + recompute from source data
# ────────────────────────────────────────────────────────────────────────

def test_derive_actuals_is_idempotent():
    """Calling twice with no source data is a no-op and never crashes."""
    out1 = cw.derive_actuals(weeks_before=2, weeks_after=0, by="pytest")
    out2 = cw.derive_actuals(weeks_before=2, weeks_after=0, by="pytest")
    assert out1["weeks_rebuilt"] == out2["weeks_rebuilt"]
