"""Regression tests for services/partial_payments.py (Phase 1 P1.5)."""
from __future__ import annotations

from datetime import datetime

import pytest

from services import db, partial_payments


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM partial_payments WHERE doc_id LIKE 'pytest_%'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM partial_payments WHERE doc_id LIKE 'pytest_%'")
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()


def _make_doc(doc_id="pytest_doc_1", amount=1000.0, is_internal=1):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, uploaded_at, status, amount, currency, is_internal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, "fixture.pdf", datetime.utcnow().isoformat(),
             "awaiting_payment", amount, "EUR", is_internal),
        )
        conn.commit()
    finally:
        conn.close()


def test_add_persists_and_total_paid():
    _make_doc(amount=500.0)
    partial_payments.add(doc_id="pytest_doc_1", amount_eur=200.0,
                         paid_at="2026-06-16", method="bank_transfer", by="pytest")
    assert partial_payments.total_paid("pytest_doc_1") == 200.0
    items = partial_payments.list_for_doc("pytest_doc_1")
    assert len(items) == 1
    assert items[0]["amount_eur"] == 200.0
    assert items[0]["method"] == "bank_transfer"


def test_remaining_decreases_with_payments():
    _make_doc(amount=1000.0)
    assert partial_payments.remaining("pytest_doc_1") == 1000.0
    partial_payments.add(doc_id="pytest_doc_1", amount_eur=300.0,
                         paid_at="2026-06-16", by="pytest")
    assert partial_payments.remaining("pytest_doc_1") == 700.0


def test_auto_transition_to_paid_when_sum_meets_total():
    _make_doc(amount=500.0)
    partial_payments.add(doc_id="pytest_doc_1", amount_eur=200.0,
                         paid_at="2026-06-16", by="pytest")
    doc = db.get_document("pytest_doc_1")
    assert doc["status"] == "awaiting_payment"
    partial_payments.add(doc_id="pytest_doc_1", amount_eur=300.0,
                         paid_at="2026-06-16", by="pytest")
    doc = db.get_document("pytest_doc_1")
    assert doc["status"] == "paid"


def test_delete_reduces_total():
    _make_doc(amount=500.0)
    p1 = partial_payments.add(doc_id="pytest_doc_1", amount_eur=100.0,
                              paid_at="2026-06-16", by="pytest")
    p2 = partial_payments.add(doc_id="pytest_doc_1", amount_eur=200.0,
                              paid_at="2026-06-17", by="pytest")
    assert partial_payments.total_paid("pytest_doc_1") == 300.0
    assert partial_payments.delete(p1["id"]) is True
    assert partial_payments.total_paid("pytest_doc_1") == 200.0


def test_reject_invalid_amount():
    _make_doc()
    with pytest.raises(ValueError, match="amount_eur"):
        partial_payments.add(doc_id="pytest_doc_1", amount_eur=0,
                             paid_at="2026-06-16", by="pytest")
    with pytest.raises(ValueError, match="amount_eur"):
        partial_payments.add(doc_id="pytest_doc_1", amount_eur=-5,
                             paid_at="2026-06-16", by="pytest")


def test_reject_unknown_doc():
    with pytest.raises(ValueError, match="not found"):
        partial_payments.add(doc_id="pytest_nonexistent", amount_eur=10,
                             paid_at="2026-06-16", by="pytest")


def test_reject_invalid_method():
    _make_doc()
    with pytest.raises(ValueError, match="method"):
        partial_payments.add(doc_id="pytest_doc_1", amount_eur=10,
                             paid_at="2026-06-16", method="bitcoin", by="pytest")


def test_list_is_sorted_by_paid_at():
    _make_doc(amount=5000.0)
    partial_payments.add(doc_id="pytest_doc_1", amount_eur=100.0,
                         paid_at="2026-06-17", by="pytest")
    partial_payments.add(doc_id="pytest_doc_1", amount_eur=200.0,
                         paid_at="2026-06-15", by="pytest")
    items = partial_payments.list_for_doc("pytest_doc_1")
    assert [i["paid_at"] for i in items] == ["2026-06-15", "2026-06-17"]
