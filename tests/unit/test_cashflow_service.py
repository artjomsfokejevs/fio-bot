"""Tests for services/cashflow.py — Phase 2 of #94."""
from __future__ import annotations

import pytest

from services import db, cashflow, revenue, revenue_receipts


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM revenue_receipts WHERE revenue_doc_id LIKE 'pf_%' OR revenue_doc_id LIKE 'in_%'")
        conn.execute("DELETE FROM revenue_audit")
        conn.execute("DELETE FROM revenue_documents WHERE customer LIKE 'cf_%'")
        conn.commit()
    finally:
        conn.close()
    yield


def _seed_doc_and_receipt(pc, amount, received_at):
    d = revenue.create_doc({
        "kind": "invoice", "profit_center": pc,
        "customer": f"cf_{pc}", "amount": amount, "status": "sent",
    }, by="t")
    revenue_receipts.add_receipt(d["id"], amount, received_at=received_at, by="t")
    return d


def test_monthly_series_aggregates_revenue():
    _seed_doc_and_receipt("AA", 100.0, "2026-05-15T12:00:00")
    _seed_doc_and_receipt("AA", 200.0, "2026-05-20T12:00:00")
    _seed_doc_and_receipt("AA",  50.0, "2026-06-01T12:00:00")
    series = cashflow.monthly_series("2026-05-01", "2026-06-30")
    months = {r["month"]: r for r in series}
    assert months["2026-05"]["revenue"] == pytest.approx(300.0)
    assert months["2026-06"]["revenue"] == pytest.approx(50.0)


def test_breakdown_by_stream_returns_per_pc_totals():
    _seed_doc_and_receipt("AA", 100.0, "2026-05-15T12:00:00")
    _seed_doc_and_receipt("MN", 250.0, "2026-05-20T12:00:00")
    rows = cashflow.breakdown_by_stream("2026-05-01", "2026-05-31")
    by_pc = {r["pc"]: r for r in rows}
    assert by_pc["AA"]["revenue"] == pytest.approx(100.0)
    assert by_pc["MN"]["revenue"] == pytest.approx(250.0)
    assert by_pc["MN"]["label"]  # human-readable PC label


def test_totals_for_period_sums_series():
    _seed_doc_and_receipt("AA", 100.0, "2026-05-15T12:00:00")
    _seed_doc_and_receipt("AA",  25.0, "2026-06-15T12:00:00")
    totals = cashflow.totals_for_period("2026-05-01", "2026-06-30")
    assert totals["revenue"] == pytest.approx(125.0)
    assert totals["months"] == 2


def test_pc_filter_translates_legacy_codes():
    # Seed with canonical SP; query with legacy SR should still find it.
    _seed_doc_and_receipt("SP", 75.0, "2026-05-15T12:00:00")
    series = cashflow.monthly_series("2026-05-01", "2026-05-31", pc="SR")
    assert series and series[0]["revenue"] == pytest.approx(75.0)
