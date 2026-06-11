"""Regression tests for Top-10 P2.2 self-review:
generic /mark-as-already-paid endpoint accepts payment_method without
forcing card_holder lie.

Uses Flask test client + an in-memory document to verify the contract.
"""
from __future__ import annotations

import json
import pytest

from services import db


@pytest.fixture
def client():
    import app as flask_app
    flask_app.app.testing = True
    flask_app.db.init_db()
    with flask_app.app.test_client() as c:
        yield c


def _seed_doc(status="approved"):
    """Insert one approvable doc and return its id."""
    import uuid
    doc_id = uuid.uuid4().hex[:12]
    db.insert_document({
        "id": doc_id,
        "filename": "smoke.pdf",
        "original_name": "smoke.pdf",
        "file_type": "pdf",
        "file_size": 1,
        "uploaded_at": "2026-06-11T00:00:00",
        "uploaded_by": "test",
        "status": status,
    })
    return doc_id


def test_mark_as_already_paid_bank_no_card_holder_lie(client):
    """The generic endpoint must NOT set paid_card_holder when method=bank."""
    doc_id = _seed_doc()
    r = client.post(
        f"/api/documents/{doc_id}/mark-as-already-paid",
        json={"payment_method": "bank"},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    doc = body.get("document") or {}
    assert doc.get("payment_method") == "bank"
    assert doc.get("already_paid_by_card") in (0, None), (
        "already_paid_by_card flag must be 0 for bank payment"
    )
    assert doc.get("paid_card_holder") is None, (
        "Must not invent a cardholder for a bank transfer."
    )
    assert doc.get("status") == "paid"


def test_mark_as_already_paid_card_keeps_legacy_semantics(client):
    """When method=card, already_paid_by_card flag is set + paid_by
    becomes the card_holder."""
    doc_id = _seed_doc()
    r = client.post(
        f"/api/documents/{doc_id}/mark-as-already-paid",
        json={"payment_method": "card", "paid_by": "Artjoms Fokejevs"},
    )
    assert r.status_code == 200, r.get_json()
    doc = r.get_json()["document"]
    assert doc["payment_method"] == "card"
    assert doc["already_paid_by_card"] == 1
    assert doc["paid_card_holder"] == "Artjoms Fokejevs"


def test_legacy_endpoint_still_requires_card_holder(client):
    """Backward-compat endpoint must still 400 without card_holder."""
    doc_id = _seed_doc()
    r = client.post(
        f"/api/documents/{doc_id}/mark-already-paid-by-card",
        json={},  # no card_holder
    )
    assert r.status_code == 400
    assert "card_holder" in (r.get_json() or {}).get("error", "")


def test_mark_as_already_paid_rejects_unknown_method(client):
    """payment_method enum is enforced."""
    doc_id = _seed_doc()
    r = client.post(
        f"/api/documents/{doc_id}/mark-as-already-paid",
        json={"payment_method": "crypto"},
    )
    assert r.status_code == 400
    assert "payment_method" in (r.get_json() or {}).get("error", "")
