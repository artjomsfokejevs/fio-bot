"""Tests for services/revenue.py and services/revenue_receipts.py — #94 Phase 1."""
from __future__ import annotations

import pytest

from services import db, revenue, revenue_receipts


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        # CASCADE drops revenue_receipts; revenue_audit cleared via FK-less DELETE
        conn.execute("DELETE FROM revenue_receipts WHERE revenue_doc_id IN "
                     "(SELECT id FROM revenue_documents WHERE customer LIKE 'pytest_%' OR id LIKE 'pf_%' OR id LIKE 'in_%' OR id LIKE 'cn_%')")
        conn.execute("DELETE FROM revenue_audit WHERE revenue_doc_id IN "
                     "(SELECT id FROM revenue_documents WHERE customer LIKE 'pytest_%' OR id LIKE 'pf_%' OR id LIKE 'in_%' OR id LIKE 'cn_%')")
        conn.execute("DELETE FROM revenue_documents WHERE customer LIKE 'pytest_%' OR id LIKE 'pf_%' OR id LIKE 'in_%' OR id LIKE 'cn_%'")
        conn.commit()
    finally:
        conn.close()
    yield


def test_create_doc_defaults_amount_eur_from_amount():
    d = revenue.create_doc({
        "kind": "invoice",
        "profit_center": "AA",
        "customer": "pytest_Acme",
        "amount": 1234.56,
        "currency": "EUR",
    }, by="tester")
    assert d["id"].startswith("in_")
    assert d["amount_eur"] == pytest.approx(1234.56)
    assert d["status"] == "draft"


def test_create_validates_required_fields():
    with pytest.raises(ValueError):
        revenue.create_doc({"kind": "invoice"}, by="t")  # no PC
    with pytest.raises(ValueError):
        revenue.create_doc({"kind": "bogus", "profit_center": "AA"}, by="t")


def test_status_machine_via_receipts():
    d = revenue.create_doc({
        "kind": "invoice", "profit_center": "MN",
        "customer": "pytest_X", "amount": 1000.0, "status": "sent",
    }, by="t")
    revenue_receipts.add_receipt(d["id"], 400.0, method="bank_transfer", by="t")
    assert revenue.get_doc(d["id"])["status"] == "partially_paid"
    revenue_receipts.add_receipt(d["id"], 600.0, method="bank_transfer", by="t")
    assert revenue.get_doc(d["id"])["status"] == "paid"
    assert revenue_receipts.remaining(d["id"]) == 0.0


def test_overpay_clamps_to_paid():
    d = revenue.create_doc({
        "kind": "invoice", "profit_center": "AA",
        "customer": "pytest_OP", "amount": 100.0, "status": "sent",
    }, by="t")
    revenue_receipts.add_receipt(d["id"], 150.0, by="t")
    assert revenue.get_doc(d["id"])["status"] == "paid"


def test_delete_receipt_demotes_status():
    d = revenue.create_doc({
        "kind": "invoice", "profit_center": "AA",
        "customer": "pytest_D", "amount": 500.0, "status": "sent",
    }, by="t")
    rcpt = revenue_receipts.add_receipt(d["id"], 500.0, by="t")
    assert revenue.get_doc(d["id"])["status"] == "paid"
    revenue_receipts.delete_receipt(rcpt["id"], by="t")
    # Recompute should knock it back down (paid → sent because received now 0)
    assert revenue.get_doc(d["id"])["status"] in ("sent", "draft")


def test_convert_proforma_to_invoice():
    pf = revenue.create_doc({
        "kind": "proforma", "profit_center": "AL",
        "customer": "pytest_Conv", "amount": 250.0,
    }, by="t")
    inv = revenue.convert_proforma_to_invoice(pf["id"], {
        "invoice_number": "ALV-2026-0001",
        "issue_date": "2026-06-23",
        "status": "sent",
    }, by="t")
    assert inv["kind"] == "invoice"
    assert inv["proforma_id"] == pf["id"]
    assert inv["customer"] == "pytest_Conv"
    # Proforma is cancelled
    assert revenue.get_doc(pf["id"])["status"] == "cancelled"


def test_convert_rejects_non_proforma():
    inv = revenue.create_doc({
        "kind": "invoice", "profit_center": "AA",
        "customer": "pytest_NP", "amount": 10.0,
    }, by="t")
    with pytest.raises(ValueError):
        revenue.convert_proforma_to_invoice(inv["id"], {}, by="t")


def test_list_filters_period_pc_kind_status():
    revenue.create_doc({"kind":"invoice","profit_center":"AA",
                        "customer":"pytest_F1","amount":10.0,
                        "issue_date":"2026-05-15","status":"sent"}, by="t")
    revenue.create_doc({"kind":"invoice","profit_center":"MN",
                        "customer":"pytest_F2","amount":20.0,
                        "issue_date":"2026-06-01","status":"paid"}, by="t")
    rows = revenue.list_docs(period="2026-05")
    ids = [r["id"] for r in rows if r["customer"] == "pytest_F1"]
    assert len(ids) == 1
    rows = revenue.list_docs(pc="MN")
    assert any(r["customer"] == "pytest_F2" for r in rows)
    rows = revenue.list_docs(status="paid")
    assert all(r["status"] == "paid" for r in rows)


def test_legacy_pc_translates_to_canonical_on_create():
    # SR is legacy for SP per pc_codes.LEGACY_TO_CANONICAL
    d = revenue.create_doc({
        "kind":"invoice", "profit_center":"SR",
        "customer":"pytest_LEG", "amount":1.0,
    }, by="t")
    assert d["profit_center"] == "SP"


def test_update_status_writes_audit_entry():
    d = revenue.create_doc({"kind":"invoice","profit_center":"AA",
                            "customer":"pytest_AUD","amount":5.0}, by="t")
    revenue.update_status(d["id"], "sent", by="tester")
    audit = revenue.audit_for(d["id"])
    actions = [a["action"] for a in audit]
    assert "status_changed" in actions
    assert "created" in actions


def test_delete_doc_cascades_receipts():
    d = revenue.create_doc({"kind":"invoice","profit_center":"AA",
                            "customer":"pytest_DEL","amount":50.0,
                            "status":"sent"}, by="t")
    revenue_receipts.add_receipt(d["id"], 25.0, by="t")
    assert len(revenue_receipts.list_receipts(d["id"])) == 1
    revenue.delete_doc(d["id"], by="t")
    assert revenue.get_doc(d["id"]) is None
    assert revenue_receipts.list_receipts(d["id"]) == []
