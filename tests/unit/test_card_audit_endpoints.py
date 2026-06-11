"""Regression tests for Top-9 Bank Statement Audit endpoints.

Covers the 4 endpoints added 2026-06-11 for the month-close workflow:
  GET  /api/card-audit/month-close-status
  GET  /api/card-audit/monthly-dashboard
  POST /api/card-audit/chase-missing
  POST /api/card-audit/transactions/<id>/suggest-owner
"""
from __future__ import annotations

import uuid

import pytest

from services import db


@pytest.fixture
def client(monkeypatch):
    import app as flask_app
    from services import roles as roles_svc
    flask_app.app.testing = True
    flask_app.db.init_db()
    monkeypatch.setattr(roles_svc, "get_role",
                        lambda name: roles_svc.ROLE_ADMIN)
    with flask_app.app.test_client() as c:
        yield c


_HDR = {"X-FIO-User": "pytest"}


def _seed_tx(period="2099-12", status="unmatched", amount_eur=100.0, **extra):
    """Insert one card_transaction directly via SQL (no API)."""
    tx_id = uuid.uuid4().hex[:12]
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO card_transactions
               (id, source, batch_id, imported_at, period, amount, currency,
                amount_eur, posted_at, description, counterparty, match_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx_id, "test", "batch_" + tx_id,
                "2026-06-11T00:00:00", period,
                amount_eur, "EUR", amount_eur,
                period + "-15", extra.get("description", "test tx"),
                extra.get("counterparty", "Test Vendor"), status,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return tx_id


def _cleanup_period(period):
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM card_transactions WHERE period = ?", (period,))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# month-close-status
# ─────────────────────────────────────────────────────────────────────

def test_month_close_status_empty_period(client):
    """No transactions → all checks False, can_close False."""
    r = client.get("/api/card-audit/month-close-status?period=2099-01",
                   headers=_HDR)
    assert r.status_code == 200
    d = r.get_json()
    assert d["total_transactions"] == 0
    assert d["checks"]["statements_imported"] is False
    assert d["can_close"] is False


def test_month_close_status_all_matched(client):
    """All tx matched → 2 of 3 checks True (no unmatched-without-owner)."""
    _cleanup_period("2099-02")
    _seed_tx(period="2099-02", status="manual")
    _seed_tx(period="2099-02", status="auto")
    try:
        r = client.get("/api/card-audit/month-close-status?period=2099-02",
                       headers=_HDR)
        d = r.get_json()
        assert d["total_transactions"] == 2
        assert d["matched_count"] == 2
        assert d["checks"]["statements_imported"] is True
        assert d["checks"]["all_reconciled"] is True
        assert d["can_close"] is True
    finally:
        _cleanup_period("2099-02")


def test_month_close_status_unmatched_no_owner(client):
    """Unmatched + no card_holder → all_reconciled False AND unmatched_have_owner False."""
    _cleanup_period("2099-03")
    _seed_tx(period="2099-03", status="unmatched")
    try:
        r = client.get("/api/card-audit/month-close-status?period=2099-03",
                       headers=_HDR)
        d = r.get_json()
        assert d["unmatched_count"] == 1
        assert d["unmatched_without_owner"] == 1
        assert d["checks"]["all_reconciled"] is False
        assert d["checks"]["unmatched_have_owner"] is False
        assert d["can_close"] is False
    finally:
        _cleanup_period("2099-03")


# ─────────────────────────────────────────────────────────────────────
# monthly-dashboard
# ─────────────────────────────────────────────────────────────────────

def test_monthly_dashboard_in_out_net(client):
    """+100 inflow + -250 outflow → in=100, out=250, net=-150."""
    _cleanup_period("2099-04")
    _seed_tx(period="2099-04", status="manual", amount_eur=100.0)
    _seed_tx(period="2099-04", status="manual", amount_eur=-250.0)
    try:
        r = client.get("/api/card-audit/monthly-dashboard?period=2099-04",
                       headers=_HDR)
        d = r.get_json()
        assert d["tx_count"] == 2
        assert d["sum_in"] == 100.0
        assert d["sum_out"] == 250.0
        assert d["net"] == -150.0
        assert d["matched_count"] == 2
        assert d["pending_count"] == 0
    finally:
        _cleanup_period("2099-04")


def test_monthly_dashboard_by_stream(client):
    """Per-stream breakdown distinguishes pending from matched."""
    _cleanup_period("2099-05")
    conn = db.get_connection()
    try:
        # AA stream: 1 matched out -150
        conn.execute("""INSERT INTO card_transactions
            (id, source, batch_id, imported_at, period, amount, currency,
             amount_eur, posted_at, description, counterparty, match_status,
             profit_center)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("aaaaa11111", "test", "b", "2026-06-11T00:00:00", "2099-05",
             -150.0, "EUR", -150.0, "2099-05-15", "x", "y", "manual", "AA"))
        # BK stream: 1 unmatched out -50
        conn.execute("""INSERT INTO card_transactions
            (id, source, batch_id, imported_at, period, amount, currency,
             amount_eur, posted_at, description, counterparty, match_status,
             profit_center)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("bbbbb22222", "test", "b", "2026-06-11T00:00:00", "2099-05",
             -50.0, "EUR", -50.0, "2099-05-15", "x", "y", "unmatched", "BK"))
        conn.commit()
    finally:
        conn.close()
    try:
        d = client.get("/api/card-audit/monthly-dashboard?period=2099-05",
                       headers=_HDR).get_json()
        streams = {s["profit_center"]: s for s in d["by_stream"]}
        assert "AA" in streams
        assert streams["AA"]["matched"] == 1
        assert streams["AA"]["pending"] == 0
        assert streams["BK"]["matched"] == 0
        assert streams["BK"]["pending"] == 1
    finally:
        _cleanup_period("2099-05")


# ─────────────────────────────────────────────────────────────────────
# chase-missing
# ─────────────────────────────────────────────────────────────────────

def test_chase_missing_zero_unmatched(client):
    """No unmatched → empty items."""
    _cleanup_period("2099-06")
    r = client.post("/api/card-audit/chase-missing?period=2099-06",
                    headers=_HDR, json={})
    d = r.get_json()
    assert d["total_unmatched"] == 0
    assert d["items"] == []


def test_chase_missing_renders_template(client):
    """Each item has rendered task_title + task_body."""
    _cleanup_period("2099-07")
    _seed_tx(period="2099-07", status="unmatched",
             amount_eur=-42.50, description="Anthropic API",
             counterparty="Anthropic")
    try:
        d = client.post("/api/card-audit/chase-missing?period=2099-07",
                        headers=_HDR, json={}).get_json()
        assert d["total_unmatched"] == 1
        item = d["items"][0]
        assert "Missing invoice" in item["task_title"]
        assert "42.50" in item["task_title"] or "42.5" in item["task_title"]
        assert item["suggested_profit_center"]  # at least "AA" fallback
        assert "Action:" in item["task_body"]
    finally:
        _cleanup_period("2099-07")


# ─────────────────────────────────────────────────────────────────────
# suggest-owner
# ─────────────────────────────────────────────────────────────────────

def test_suggest_owner_404_unknown_tx(client):
    r = client.post("/api/card-audit/transactions/__nope__/suggest-owner",
                    headers=_HDR, json={})
    assert r.status_code == 404


def test_suggest_owner_returns_pc(client):
    """Even with no signal, falls back to AA + 'no strong signal' reason."""
    _cleanup_period("2099-08")
    tx_id = _seed_tx(period="2099-08", status="unmatched",
                     counterparty="Random Vendor Inc")
    try:
        r = client.post(f"/api/card-audit/transactions/{tx_id}/suggest-owner",
                        headers=_HDR, json={})
        assert r.status_code == 200
        d = r.get_json()
        assert d["suggested_profit_center"]
        assert d["reason"]
    finally:
        _cleanup_period("2099-08")
