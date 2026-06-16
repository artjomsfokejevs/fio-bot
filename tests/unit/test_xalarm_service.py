"""Regression tests for services/xalarm.py (Phase 3 — minimal coverage,
side-effects deliberately untested: email + Asana have their own
graceful-degraded paths)."""
from __future__ import annotations

from datetime import datetime

import pytest

from services import db, stream_budgets as sb, xalarm


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM xalarm_log WHERE profit_center LIKE 'PX_%'")
        conn.execute("DELETE FROM stream_budgets WHERE profit_center LIKE 'PX_%'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_xa_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM xalarm_log WHERE profit_center LIKE 'PX_%'")
        conn.execute("DELETE FROM stream_budgets WHERE profit_center LIKE 'PX_%'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_xa_%'")
        conn.commit()
    finally:
        conn.close()


def _seed(doc_id, pc, period, amount, status="paid"):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, uploaded_at, status, "
            " amount, currency, profit_center, period) "
            "VALUES (?, ?, ?, ?, ?, 'EUR', ?, ?)",
            (doc_id, "x.pdf", datetime.utcnow().isoformat(),
             status, amount, pc, period),
        )
        conn.commit()
    finally:
        conn.close()


def test_no_alarm_when_under_budget():
    sb.set_budget(pc="PX_AA", period="2026-06", eur=1000.0, by="pytest")
    _seed("pytest_xa_1", "PX_AA", "2026-06", 500.0)
    res = xalarm.fire_if_overrun(doc_id="pytest_xa_1",
                                 triggering_action="confirm_payment",
                                 actor="pytest")
    assert res is None


def test_alarm_fires_when_over_budget():
    sb.set_budget(pc="PX_AA", period="2026-06", eur=1000.0, by="pytest")
    _seed("pytest_xa_2", "PX_AA", "2026-06", 1500.0)
    res = xalarm.fire_if_overrun(doc_id="pytest_xa_2",
                                 triggering_action="confirm_payment",
                                 actor="pytest")
    assert res is not None
    assert res["overrun_eur"] == 500.0
    assert res["dedup_hit"] is False
    # email_status is the graceful 'not_configured' in test env
    assert res["email_status"] in ("sent", "not_configured", "no_recipients", "error")


def test_alarm_dedups_within_24h():
    sb.set_budget(pc="PX_AA", period="2026-06", eur=1000.0, by="pytest")
    _seed("pytest_xa_3", "PX_AA", "2026-06", 1500.0)
    r1 = xalarm.fire_if_overrun(doc_id="pytest_xa_3",
                                triggering_action="confirm_payment",
                                actor="pytest")
    # Same trigger fires again — should update existing row, not create new
    r2 = xalarm.fire_if_overrun(doc_id="pytest_xa_3",
                                triggering_action="confirm_payment",
                                actor="pytest")
    assert r1 is not None and r2 is not None
    assert r2["dedup_hit"] is True
    rows = xalarm.list_log(pc="PX_AA", period="2026-06")
    assert len(rows) == 1


def test_acknowledge_marks_and_is_idempotent():
    sb.set_budget(pc="PX_AA", period="2026-06", eur=1000.0, by="pytest")
    _seed("pytest_xa_4", "PX_AA", "2026-06", 1500.0)
    xalarm.fire_if_overrun(doc_id="pytest_xa_4",
                           triggering_action="confirm_payment", actor="pytest")
    rows = xalarm.list_log(pc="PX_AA", period="2026-06")
    xid = rows[0]["id"]
    assert xalarm.acknowledge(xid, by="pytest") is True
    assert xalarm.acknowledge(xid, by="pytest") is False  # already acked
