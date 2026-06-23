"""Tests for services/revenue_bank_match.py — Phase 3 of #94."""
from __future__ import annotations

from datetime import datetime

import pytest

from services import db, revenue, revenue_bank_match as bm


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM revenue_receipts WHERE bank_statement_tx_id LIKE 'tx_pytest_%'")
        conn.execute("DELETE FROM revenue_audit")
        conn.execute("DELETE FROM revenue_documents WHERE customer LIKE 'bm_%'")
        conn.execute("DELETE FROM card_transactions WHERE id LIKE 'tx_pytest_%'")
        conn.commit()
    finally:
        conn.close()
    yield


def _seed_tx(tx_id, amount, description="", counterparty="", batch_id="bm_pytest"):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO card_transactions (id, source, batch_id, imported_at, "
            "posted_at, period, amount, currency, amount_eur, description, "
            "counterparty, match_status) "
            "VALUES (?, 'mercury', ?, ?, ?, ?, ?, 'EUR', ?, ?, ?, 'unmatched')",
            (tx_id, batch_id, datetime.utcnow().isoformat(),
             "2026-05-20", "2026-05", amount, amount, description, counterparty),
        )
        conn.commit()
    finally:
        conn.close()


def test_suggestions_score_exact_amount_match():
    d = revenue.create_doc({"kind": "invoice", "profit_center": "AA",
                            "customer": "bm_Acme Travel Ltd",
                            "invoice_number": "INV-2026-7777",
                            "issue_date": "2026-05-15",
                            "amount": 500.0, "status": "sent"}, by="t")
    _seed_tx("tx_pytest_1", 500.0,
             description="Wire from Acme Travel inv INV-2026-7777",
             counterparty="ACME TRAVEL LTD")
    conn = db.get_connection()
    tx = dict(conn.execute("SELECT * FROM card_transactions WHERE id = 'tx_pytest_1'").fetchone())
    conn.close()
    sugg = bm.suggestions_for_tx(tx)
    assert sugg
    top = sugg[0]
    assert top["doc_id"] == d["id"]
    assert top["score"] >= 80  # amount(50) + inv#(20) + customer(20) + date(10)


def test_apply_match_creates_receipt_and_flips_status():
    d = revenue.create_doc({"kind": "invoice", "profit_center": "AA",
                            "customer": "bm_X", "amount": 100.0,
                            "status": "sent"}, by="t")
    _seed_tx("tx_pytest_2", 100.0, description="payment")
    receipt = bm.apply_match("tx_pytest_2", d["id"], by="tester")
    assert receipt["amount_eur"] == 100.0
    assert revenue.get_doc(d["id"])["status"] == "paid"
    # tx itself should be tagged
    conn = db.get_connection()
    row = conn.execute("SELECT match_status, matched_invoice_id FROM card_transactions "
                       "WHERE id = 'tx_pytest_2'").fetchone()
    conn.close()
    assert row["match_status"] == "matched"
    assert row["matched_invoice_id"] == d["id"]


def test_auto_match_batch_applies_high_scores_only():
    # High-confidence candidate
    high = revenue.create_doc({"kind": "invoice", "profit_center": "AA",
                               "customer": "bm_Sharp Customer",
                               "invoice_number": "BM-2026-001",
                               "issue_date": "2026-05-19",
                               "amount": 1000.0, "status": "sent"}, by="t")
    # Weak candidate (just remaining-amount partial match, no other signals)
    weak = revenue.create_doc({"kind": "invoice", "profit_center": "MN",
                               "customer": "bm_Mystery",
                               "amount": 9999.0, "status": "sent"}, by="t")
    _seed_tx("tx_pytest_hi", 1000.0,
             description="Sharp Customer bm-2026-001",
             counterparty="Sharp Customer")
    _seed_tx("tx_pytest_lo", 25.0, description="zzz")
    applied = bm.auto_match_batch("bm_pytest", min_score=80, by="auto")
    matched_ids = {a["doc_id"] for a in applied}
    assert high["id"] in matched_ids
    assert weak["id"] not in matched_ids
