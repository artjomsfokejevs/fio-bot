"""Regression tests for db.get_documents q/sort/date filters (Phase 1 P1.4)."""
from __future__ import annotations

import pytest

from services import db


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_filter_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_filter_%'")
        conn.commit()
    finally:
        conn.close()


def _seed():
    conn = db.get_connection()
    try:
        rows = [
            ("pytest_filter_a", "alps.pdf", "2026-06-10T10:00:00", "pending",  500.0, "Alps2Alps"),
            ("pytest_filter_b", "fedex.pdf", "2026-06-12T09:00:00", "pending", 1500.0, "FedEx Express"),
            ("pytest_filter_c", "linkedin.pdf", "2026-06-15T11:00:00", "approved", 200.0, "LinkedIn"),
            ("pytest_filter_d", "k2y.pdf", "2026-06-08T08:00:00", "pending",  530.30, "K2Y Advisers"),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO documents (id, filename, uploaded_at, status, amount, vendor, currency) "
                "VALUES (?, ?, ?, ?, ?, ?, 'EUR')",
                r,
            )
        conn.commit()
    finally:
        conn.close()


def _ids(docs):
    return [d["id"] for d in docs if d["id"].startswith("pytest_filter_")]


def test_search_q_matches_vendor():
    _seed()
    docs = db.get_documents(q="fedex")
    assert _ids(docs) == ["pytest_filter_b"]


def test_search_q_matches_amount_substring():
    _seed()
    docs = db.get_documents(q="530")
    assert _ids(docs) == ["pytest_filter_d"]


def test_sort_amount_desc():
    _seed()
    docs = db.get_documents(q=".pdf")
    docs = [d for d in docs if d["id"].startswith("pytest_filter_")]
    # Default sort = date_desc — pytest_filter_c is newest
    assert docs[0]["id"] == "pytest_filter_c"
    docs2 = db.get_documents(q=".pdf", sort="amount_desc")
    docs2 = [d for d in docs2 if d["id"].startswith("pytest_filter_")]
    assert docs2[0]["amount"] == 1500.0


def test_sort_amount_asc():
    _seed()
    docs = db.get_documents(q=".pdf", sort="amount_asc")
    docs = [d for d in docs if d["id"].startswith("pytest_filter_")]
    assert docs[0]["amount"] == 200.0


def test_date_range_filter():
    _seed()
    docs = db.get_documents(q=".pdf", date_from="2026-06-10", date_to="2026-06-13")
    ids = [d["id"] for d in docs if d["id"].startswith("pytest_filter_")]
    assert sorted(ids) == ["pytest_filter_a", "pytest_filter_b"]


def test_combined_search_and_status():
    _seed()
    docs = db.get_documents(status="pending", q=".pdf")
    ids = sorted(d["id"] for d in docs if d["id"].startswith("pytest_filter_"))
    assert ids == ["pytest_filter_a", "pytest_filter_b", "pytest_filter_d"]


def test_sort_vendor_alpha():
    _seed()
    docs = db.get_documents(q=".pdf", sort="vendor")
    ids = [d["id"] for d in docs if d["id"].startswith("pytest_filter_")]
    # Alphabetical: Alps2Alps, FedEx, K2Y, LinkedIn
    assert ids == ["pytest_filter_a", "pytest_filter_b", "pytest_filter_d", "pytest_filter_c"]
