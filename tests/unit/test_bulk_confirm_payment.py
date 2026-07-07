"""Bulk Approve-to-pay for Holding CEO (2026-07-02 op-feedback).

The CEO can approve N invoices to pay in one call, stamping the same
shared note on every doc ("Pay Wednesday"). Failures are per-doc, so
one bad row does not sink the whole batch.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from services import db, roles as roles_svc


@pytest.fixture
def client(monkeypatch):
    import app as flask_app
    flask_app.app.testing = True
    flask_app.db.init_db()
    monkeypatch.setattr(roles_svc, "get_role",
                        lambda name: roles_svc.ROLE_HOLDING_CEO)
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'test_bulk_%'")
        conn.commit()
    finally:
        conn.close()


HDR = {"X-FIO-User": "pytest-ceo"}


def _seed_doc(doc_id, *, status="budget_validated", amount=100.0, pc="AA"):
    conn = db.get_connection()
    now = datetime.utcnow().isoformat()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, file_size, uploaded_at, "
            " uploaded_by, status, amount, currency, currency_orig, "
            " profit_center, vendor) "
            "VALUES (?, 'x.pdf', 0, ?, 'pytest', ?, ?, 'EUR', 'EUR', ?, 'Test Vendor')",
            (doc_id, now, status, amount, pc),
        )
        conn.commit()
    finally:
        conn.close()


def test_bulk_confirm_happy_path(client):
    for i in range(5):
        _seed_doc(f"test_bulk_ok_{i}", amount=100 * (i + 1))
    doc_ids = [f"test_bulk_ok_{i}" for i in range(5)]
    resp = client.post(
        "/api/documents/bulk-confirm-payment",
        headers=HDR,
        json={"doc_ids": doc_ids, "note": "Pay Wednesday",
              "confirmed_by": "pytest-ceo"},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["summary"]["confirmed_count"] == 5
    assert body["summary"]["failed_count"] == 0
    assert body["summary"]["total_amount_eur"] == pytest.approx(1500.0)
    for doc_id in doc_ids:
        d = db.get_document(doc_id)
        assert d["status"] == "confirmed_to_pay"
        assert d["confirmed_to_pay_note"] == "Pay Wednesday"
        assert d["confirmed_to_pay_by"] == "pytest-ceo"


def test_bulk_mixed_batch_confirms_valid_reports_bad(client):
    _seed_doc("test_bulk_valid_1", status="budget_validated")
    _seed_doc("test_bulk_valid_2", status="approved")
    _seed_doc("test_bulk_early", status="parsed")
    resp = client.post(
        "/api/documents/bulk-confirm-payment",
        headers=HDR,
        json={"doc_ids": ["test_bulk_valid_1", "test_bulk_valid_2",
                            "test_bulk_early", "test_bulk_missing_xyz"]},
    )
    body = resp.get_json()
    assert body["summary"]["confirmed_count"] == 2
    assert body["summary"]["failed_count"] == 2
    failed_ids = {f["doc_id"] for f in body["failed"]}
    assert failed_ids == {"test_bulk_early", "test_bulk_missing_xyz"}


def test_bulk_rejects_empty_batch(client):
    resp = client.post("/api/documents/bulk-confirm-payment",
                        headers=HDR, json={"doc_ids": []})
    assert resp.status_code == 400


def test_bulk_rejects_oversized_batch(client):
    resp = client.post(
        "/api/documents/bulk-confirm-payment",
        headers=HDR,
        json={"doc_ids": [f"x{i}" for i in range(101)]},
    )
    assert resp.status_code == 400
    assert "batch too large" in resp.get_json()["error"]


def test_bulk_null_note_is_stored_as_none(client):
    _seed_doc("test_bulk_nonote_1")
    resp = client.post(
        "/api/documents/bulk-confirm-payment",
        headers=HDR,
        json={"doc_ids": ["test_bulk_nonote_1"], "note": ""},
    )
    assert resp.status_code == 200
    d = db.get_document("test_bulk_nonote_1")
    assert d["status"] == "confirmed_to_pay"
    assert d["confirmed_to_pay_note"] in (None, "")
