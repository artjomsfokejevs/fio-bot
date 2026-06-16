"""Regression tests for services/stream_budgets.py (Phase 3)."""
from __future__ import annotations

from datetime import datetime

import pytest

from services import db, stream_budgets as sb


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM stream_budgets WHERE profit_center LIKE 'PT_%'")
        conn.execute("DELETE FROM stream_budget_history WHERE pc LIKE 'PT_%'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_sb_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM stream_budgets WHERE profit_center LIKE 'PT_%'")
        conn.execute("DELETE FROM stream_budget_history WHERE pc LIKE 'PT_%'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_sb_%'")
        conn.commit()
    finally:
        conn.close()


def test_set_and_get_round_trip():
    b = sb.set_budget(pc="PT_AA", period="2026-06", eur=10000.0,
                      agreed_by_ceo="CEO", by="pytest", reason="initial")
    assert b["budget_eur"] == 10000.0
    got = sb.get_budget("PT_AA", "2026-06")
    assert got["agreed_by_ceo"] == "CEO"


def test_update_writes_history():
    sb.set_budget(pc="PT_AA", period="2026-06", eur=10000.0, by="pytest")
    sb.set_budget(pc="PT_AA", period="2026-06", eur=12000.0, by="pytest",
                  reason="Q3 peak")
    hist = sb.history_for(pc="PT_AA", period="2026-06")
    assert len(hist) == 2
    assert hist[0]["new_eur"] == 12000.0
    assert hist[0]["old_eur"] == 10000.0
    assert hist[0]["reason"] == "Q3 peak"


def test_validate_period_format():
    with pytest.raises(ValueError, match="period"):
        sb.set_budget(pc="PT_AA", period="2026/06", eur=100, by="pytest")


def test_validate_negative_budget():
    with pytest.raises(ValueError, match=">= 0"):
        sb.set_budget(pc="PT_AA", period="2026-06", eur=-10, by="pytest")


def _seed_doc(doc_id, pc, period, amount, status):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, uploaded_at, status, "
            " amount, currency, profit_center, period) "
            "VALUES (?, ?, ?, ?, ?, 'EUR', ?, ?)",
            (doc_id, doc_id + ".pdf", datetime.utcnow().isoformat(),
             status, amount, pc, period),
        )
        conn.commit()
    finally:
        conn.close()


def test_actuals_sums_committed_only():
    _seed_doc("pytest_sb_1", "PT_AA", "2026-06", 1000.0, "paid")
    _seed_doc("pytest_sb_2", "PT_AA", "2026-06", 500.0, "confirmed_to_pay")
    _seed_doc("pytest_sb_3", "PT_AA", "2026-06", 999.0, "pending")  # excluded
    assert sb.actuals_for("PT_AA", "2026-06") == 1500.0


def test_is_over_detects_overrun():
    sb.set_budget(pc="PT_AA", period="2026-06", eur=1000.0, by="pytest")
    _seed_doc("pytest_sb_a", "PT_AA", "2026-06", 1500.0, "paid")
    st = sb.is_over("PT_AA", "2026-06")
    assert st["over"] is True
    assert st["overrun_eur"] == 500.0
    assert st["overrun_pct"] == 50.0


def test_is_over_handles_no_budget():
    _seed_doc("pytest_sb_b", "PT_BK", "2026-06", 100.0, "paid")
    st = sb.is_over("PT_BK", "2026-06")
    assert st["over"] is False
    assert st["has_budget"] is False
    assert st["actual_eur"] == 100.0
