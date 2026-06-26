"""Tests for services/xalarm.fire_if_low_runway — G-xalarm (2026-06-26)."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from services import db, xalarm
from services import cashflow_projection as cp


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM xalarm_log WHERE period='runway'")
        conn.execute("DELETE FROM bank_account_balances WHERE source='pytest_runway'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_runway_%'")
        conn.commit()
    finally:
        conn.close()
    yield


def _seed_doc(doc_id, amount, desired_date, pc="AA",
              status="confirmed_to_pay"):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, uploaded_at, status, amount, "
            " currency, profit_center, desired_payment_date) "
            "VALUES (?, 'x.pdf', ?, ?, ?, 'EUR', ?, ?)",
            (doc_id, datetime.utcnow().isoformat(), status, amount, pc, desired_date),
        )
        conn.commit()
    finally:
        conn.close()


def _stub_send(*a, **kw):
    return {"status": "stubbed"}


def test_runway_alarm_skips_when_runway_is_healthy():
    cp.set_opening_balance(pc="AA", balance_eur=1_000_000.0,
                            source="pytest_runway", by="pytest")
    with patch("services.email_send.send", side_effect=_stub_send):
        result = xalarm.fire_if_low_runway(threshold_weeks=4, pc="AA",
                                            actor="pytest")
    assert result is None


def test_runway_alarm_fires_when_runway_below_threshold():
    cp.set_opening_balance(pc="AA", balance_eur=1000.0,
                            source="pytest_runway", by="pytest")
    # AP of 600 EUR in week 1 and 600 EUR in week 2 → balance -200 by wk 2
    today = datetime.utcnow()
    next_week = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    week2 = (today + timedelta(days=10)).strftime("%Y-%m-%d")
    _seed_doc("pytest_runway_1", 600, next_week)
    _seed_doc("pytest_runway_2", 600, week2)
    with patch("services.email_send.send", side_effect=_stub_send):
        result = xalarm.fire_if_low_runway(threshold_weeks=4, pc="AA",
                                            actor="pytest")
    assert result is not None
    assert result["runway_weeks"] is not None
    assert result["runway_weeks"] <= 4
    assert result["threshold_weeks"] == 4
    assert result["pc"] == "AA"


def test_runway_alarm_dedup_within_24h():
    cp.set_opening_balance(pc="AA", balance_eur=500.0,
                            source="pytest_runway", by="pytest")
    today = datetime.utcnow()
    next_week = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    _seed_doc("pytest_runway_dd", 1000, next_week)
    with patch("services.email_send.send", side_effect=_stub_send):
        first = xalarm.fire_if_low_runway(threshold_weeks=4, pc="AA",
                                           actor="pytest")
        second = xalarm.fire_if_low_runway(threshold_weeks=4, pc="AA",
                                            actor="pytest")
    assert first is not None and second is not None
    # Same row (xalarm_log dedup window)
    assert second["dedup_hit"] is True
    assert second["id"] == first["id"]


def test_runway_alarm_skips_when_no_data():
    # No opening balance, no AR, no AP → projection is meaningless
    with patch("services.email_send.send", side_effect=_stub_send):
        result = xalarm.fire_if_low_runway(threshold_weeks=4, pc="AA",
                                            actor="pytest")
    assert result is None
