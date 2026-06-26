"""Tests for services/cashflow_projection.py — Phase 3 (G1)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services import db, cashflow_projection as cp, revenue


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM bank_account_balances WHERE recorded_by = 'pytest'")
        conn.execute("DELETE FROM revenue_documents WHERE customer LIKE 'pytest_cp_%'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_cp_%'")
        conn.commit()
    finally:
        conn.close()
    yield


def _next_monday(days_ahead: int) -> str:
    """Returns a date string for `days_ahead` days from today, snapped to that calendar day."""
    return (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def test_opening_balance_returns_latest_per_account():
    cp.set_opening_balance(pc="AA", balance_eur=100_000, as_of_date="2026-01-15",
                            paying_account_id=1, by="pytest")
    cp.set_opening_balance(pc="AA", balance_eur=150_000, as_of_date="2026-02-15",
                            paying_account_id=1, by="pytest")
    assert cp.opening_balance_for("AA") == pytest.approx(150_000)


def test_opening_balance_sums_across_accounts():
    cp.set_opening_balance(pc="MN", balance_eur=50_000, as_of_date="2026-02-01",
                            paying_account_id=10, by="pytest")
    cp.set_opening_balance(pc="MN", balance_eur=30_000, as_of_date="2026-02-01",
                            paying_account_id=11, by="pytest")
    assert cp.opening_balance_for("MN") == pytest.approx(80_000)


def test_project_returns_correct_shape():
    cp.set_opening_balance(pc="SP", balance_eur=200_000, by="pytest")
    result = cp.project(weeks=4, pc="SP")
    assert result["pc"] == "SP"
    assert result["weeks"] == 4
    assert len(result["series"]) == 4
    assert result["opening_balance_eur"] == pytest.approx(200_000)
    # No AR/AP seeded for SP — running balance should be flat
    assert result["series"][-1]["running_balance"] == pytest.approx(200_000)


def test_project_consumes_ap_outflows_in_window():
    cp.set_opening_balance(pc="AA", balance_eur=10_000, by="pytest")
    # Seed a doc with desired_payment_date 7 days out
    pay_date = _next_monday(7)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, uploaded_at, status, amount, "
            " currency, profit_center, desired_payment_date) "
            "VALUES ('pytest_cp_ap1', 'x.pdf', ?, 'budget_validated', 3000, 'EUR', "
            " 'AA', ?)",
            (datetime.utcnow().isoformat(), pay_date),
        )
        conn.commit()
    finally:
        conn.close()
    result = cp.project(weeks=4, pc="AA")
    total_out = sum(s["ap_out"] for s in result["series"])
    assert total_out == pytest.approx(3000)
    # Final running balance dropped by 3000
    assert result["series"][-1]["running_balance"] == pytest.approx(7000)


def test_project_runway_warning_when_balance_goes_negative():
    cp.set_opening_balance(pc="MT", balance_eur=1000, by="pytest")
    pay_date = _next_monday(3)
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, uploaded_at, status, amount, "
            " currency, profit_center, desired_payment_date) "
            "VALUES ('pytest_cp_ap2', 'x.pdf', ?, 'confirmed_to_pay', 5000, 'EUR', "
            " 'MT', ?)",
            (datetime.utcnow().isoformat(), pay_date),
        )
        conn.commit()
    finally:
        conn.close()
    result = cp.project(weeks=4, pc="MT")
    assert result["runway_weeks"] is not None
    assert result["runway_weeks"] <= 2  # runs out within the first two weeks
    assert result["ending_balance_eur"] < 0


def test_project_includes_ar_inflows():
    cp.set_opening_balance(pc="AL", balance_eur=5000, by="pytest")
    due = _next_monday(10)
    rev_doc = revenue.create_doc({
        "kind": "invoice", "profit_center": "AL",
        "customer": "pytest_cp_AR", "amount": 8000,
        "due_date": due, "status": "sent",
    }, by="pytest")
    result = cp.project(weeks=4, pc="AL")
    total_in = sum(s["ar_in"] for s in result["series"])
    assert total_in == pytest.approx(8000)
    # cleanup
    revenue.delete_doc(rev_doc["id"], by="pytest")
