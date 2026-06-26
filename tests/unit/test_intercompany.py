"""Tests for services/intercompany.py — Phase 3 (G2)."""
from __future__ import annotations

from datetime import datetime

import pytest

from services import db, intercompany as ic, revenue, revenue_receipts


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'pytest_ic_%'")
        conn.execute("DELETE FROM revenue_receipts WHERE revenue_doc_id LIKE 'in_%' "
                     "AND revenue_doc_id IN (SELECT id FROM revenue_documents "
                     "WHERE customer LIKE 'pytest_ic_%')")
        conn.execute("DELETE FROM revenue_documents WHERE customer LIKE 'pytest_ic_%'")
        conn.commit()
    finally:
        conn.close()
    yield


def _seed_doc(doc_id, pc, amount, counterparty_pc=None, status="paid",
              period="2026-06", ledger="OP00"):
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, uploaded_at, status, amount, "
            " currency, profit_center, ledger_code, period, counterparty_pc) "
            "VALUES (?, 'x.pdf', ?, ?, ?, 'EUR', ?, ?, ?, ?)",
            (doc_id, datetime.utcnow().isoformat(), status, amount, pc, ledger,
             period, counterparty_pc),
        )
        conn.commit()
    finally:
        conn.close()


def test_by_pair_lists_intercompany_aggregates():
    _seed_doc("pytest_ic_1", pc="AA", amount=1200, counterparty_pc="AG")
    _seed_doc("pytest_ic_2", pc="AA", amount=800, counterparty_pc="AG")
    _seed_doc("pytest_ic_3", pc="MN", amount=500, counterparty_pc="AG")
    _seed_doc("pytest_ic_4", pc="AA", amount=1000)  # external, no elim
    pairs = ic.by_pair(period="2026-06")
    by_key = {(p["pc_from"], p["pc_to"]): p for p in pairs}
    assert by_key[("AG", "AA")]["amount_eur"] == pytest.approx(2000)
    assert by_key[("AG", "AA")]["doc_count"] == 2
    assert by_key[("AG", "MN")]["amount_eur"] == pytest.approx(500)


def test_by_pair_excludes_external_rows():
    _seed_doc("pytest_ic_e1", pc="AA", amount=999)  # no counterparty_pc
    pairs = ic.by_pair(period="2026-06")
    # No pairs at all if all rows are external
    assert all(p["pc_from"] != p["pc_to"] for p in pairs)
    assert not any(p["pc_from"] is None for p in pairs)


def test_consolidated_pnl_eliminates_intercompany():
    # Setup: AG charged AA €1000 + AA had external €500 of expense
    _seed_doc("pytest_ic_ag", pc="AG", amount=1000)  # AG raw spend (the shared service)
    _seed_doc("pytest_ic_aa_int", pc="AA", amount=1000, counterparty_pc="AG")
    _seed_doc("pytest_ic_aa_ext", pc="AA", amount=500)
    result = ic.consolidated_pnl(period="2026-06")
    assert result["raw_expense"] == pytest.approx(2500)
    assert result["eliminations"]["intercompany_total"] == pytest.approx(1000)
    assert result["consolidated_expense"] == pytest.approx(1500)
    # Each PC's row should know its eliminated bit
    by_pc = {r["pc"]: r for r in result["by_stream"]}
    assert by_pc["AA"]["eliminated"] == pytest.approx(1000)
    assert by_pc["AA"]["consolidated_expense"] == pytest.approx(500)
    assert by_pc["AG"]["eliminated"] == pytest.approx(0)


def test_consolidated_pnl_includes_revenue():
    doc = revenue.create_doc({
        "kind": "invoice", "profit_center": "AA",
        "customer": "pytest_ic_X", "amount": 5000, "status": "sent",
    }, by="pytest")
    revenue_receipts.add_receipt(doc["id"], 5000, received_at="2026-06-15T12:00:00",
                                  by="pytest")
    _seed_doc("pytest_ic_aa_cost", pc="AA", amount=2000)
    result = ic.consolidated_pnl(period="2026-06")
    assert result["raw_revenue"] == pytest.approx(5000)
    assert result["consolidated_net"] == pytest.approx(3000)
    revenue.delete_doc(doc["id"], by="pytest")


def test_set_counterparty_pc_translates_legacy_to_canonical():
    _seed_doc("pytest_ic_legacy", pc="AA", amount=100)
    ic.set_counterparty_pc("pytest_ic_legacy", "SR")   # SR → SP
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT counterparty_pc FROM documents WHERE id = 'pytest_ic_legacy'"
        ).fetchone()
    finally:
        conn.close()
    assert row["counterparty_pc"] == "SP"


def test_intercompany_total_scoped_to_pc():
    _seed_doc("pytest_ic_aa", pc="AA", amount=1000, counterparty_pc="AG")
    _seed_doc("pytest_ic_mn", pc="MN", amount=500, counterparty_pc="AG")
    assert ic.intercompany_total(period="2026-06", pc="AA") == pytest.approx(1000)
    assert ic.intercompany_total(period="2026-06", pc="MN") == pytest.approx(500)
    assert ic.intercompany_total(period="2026-06", pc="AG") == pytest.approx(1500)
    assert ic.intercompany_total(period="2026-06") == pytest.approx(1500)
