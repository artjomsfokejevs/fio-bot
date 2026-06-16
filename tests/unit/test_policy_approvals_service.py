"""Regression tests for services/policy_approvals.py (Phase 1 P1.3)."""
from __future__ import annotations

import pytest

from services import db, policy_approvals


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM policy_violation_approvals WHERE doc_id LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM policy_violation_approvals WHERE doc_id LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()


def test_violation_key_deterministic():
    k1 = policy_approvals.violation_key("pytest_doc1", "office_supplies", "red", "Exceeds 500")
    k2 = policy_approvals.violation_key("pytest_doc1", "office_supplies", "red", "Exceeds 500")
    assert k1 == k2
    assert len(k1) == 16


def test_violation_key_varies_on_inputs():
    k1 = policy_approvals.violation_key("pytest_doc1", "office_supplies", "red", "x")
    k2 = policy_approvals.violation_key("pytest_doc2", "office_supplies", "red", "x")
    assert k1 != k2


def test_approve_persists_and_is_approved():
    key = policy_approvals.violation_key("pytest_doc_a", "office_supplies", "red", "x")
    assert policy_approvals.is_approved(key) is False
    rec = policy_approvals.approve(
        key=key, doc_id="pytest_doc_a", policy_name="office_supplies",
        level="red", message="x", approved_by="Rita", role="bookkeeper",
        reason="vendor confirmed inclusive of VAT",
    )
    assert rec["approved_by"] == "Rita"
    assert policy_approvals.is_approved(key) is True


def test_approve_is_idempotent():
    key = policy_approvals.violation_key("pytest_doc_b", "business_travel", "yellow", "y")
    rec1 = policy_approvals.approve(
        key=key, doc_id="pytest_doc_b", policy_name="business_travel",
        level="yellow", message="y", approved_by="A", role="admin",
    )
    rec2 = policy_approvals.approve(
        key=key, doc_id="pytest_doc_b", policy_name="business_travel",
        level="yellow", message="y", approved_by="B", role="bookkeeper",
    )
    assert rec1["id"] == rec2["id"]
    assert rec2["approved_by"] == "A"  # first writer wins


def test_list_filters_by_doc_id():
    k1 = policy_approvals.violation_key("pytest_doc_c", "office_supplies", "red", "x")
    k2 = policy_approvals.violation_key("pytest_doc_d", "office_supplies", "red", "x")
    policy_approvals.approve(key=k1, doc_id="pytest_doc_c", policy_name="office_supplies",
                             level="red", message="x", approved_by="A")
    policy_approvals.approve(key=k2, doc_id="pytest_doc_d", policy_name="office_supplies",
                             level="red", message="x", approved_by="A")
    for_c = policy_approvals.list_approvals(doc_id="pytest_doc_c")
    assert len(for_c) == 1
    assert for_c[0]["doc_id"] == "pytest_doc_c"


def test_delete_removes():
    key = policy_approvals.violation_key("pytest_doc_e", "office_supplies", "red", "x")
    rec = policy_approvals.approve(
        key=key, doc_id="pytest_doc_e", policy_name="office_supplies",
        level="red", message="x", approved_by="A",
    )
    assert policy_approvals.delete_approval(rec["id"]) is True
    assert policy_approvals.is_approved(key) is False


def test_approve_requires_key():
    with pytest.raises(ValueError, match="violation_key"):
        policy_approvals.approve(key="", doc_id="x", policy_name="x", level="red",
                                 message="x", approved_by="A")
