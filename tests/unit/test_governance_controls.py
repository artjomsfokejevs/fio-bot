"""SOP-FIN-001 Procedure 4 governance hard-controls (2026-07-09).

G1 invoice-splitting · G2 two-person SoD · G3 vendor bank-detail change.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from services import db, governance as gov, settings as settings_svc


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'tg_%'")
        conn.execute("DELETE FROM vendor_bank_details WHERE vendor_key LIKE '%tgvendor%' OR first_doc_id LIKE 'tg_%'")
        conn.execute("DELETE FROM fio_settings WHERE key = 'sod_mode'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'tg_%'")
        conn.execute("DELETE FROM vendor_bank_details WHERE first_doc_id LIKE 'tg_%'")
        conn.execute("DELETE FROM fio_settings WHERE key = 'sod_mode'")
        conn.commit()
    finally:
        conn.close()


def _seed(doc_id, *, vendor="TG Vendor", amount=900.0, legal_entity="ALPS2ALPS_OU",
          status="classified", iban=None, uploaded_at=None):
    parsed = {"vendor": {"name": vendor}}
    if iban:
        parsed["vendor"]["iban"] = iban
    conn = db.get_connection()
    now = uploaded_at or datetime.utcnow().isoformat()
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, file_size, uploaded_at, status, "
            " amount, currency, vendor, legal_entity, parsed_json) "
            "VALUES (?, 'x.pdf', 0, ?, ?, ?, 'EUR', ?, ?, ?)",
            (doc_id, now, status, amount, vendor, legal_entity,
             json.dumps(parsed)),
        )
        conn.commit()
    finally:
        conn.close()


# ── G1 ───────────────────────────────────────────────────────────────────
def test_tier_boundaries():
    assert gov.tier_for(999) == "T1"
    assert gov.tier_for(1000) == "T2"
    assert gov.tier_for(9999) == "T2"
    assert gov.tier_for(10000) == "T3"
    assert gov.tier_for(50000) == "T4"


def test_split_detected_when_aggregate_crosses_tier():
    # Three €900 invoices from one vendor = €2700 → T2, each alone T1.
    _seed("tg_s1", amount=900.0)
    _seed("tg_s2", amount=900.0)
    out = gov.detect_split("TG Vendor", "ALPS2ALPS_OU", 900.0, exclude_doc_id="tg_new")
    assert out["individual_tier"] == "T1"
    assert out["aggregate_eur"] == pytest.approx(2700.0)
    assert out["aggregate_tier"] == "T2"
    assert out["split_suspected"] is True


def test_no_split_for_lone_invoice():
    out = gov.detect_split("Lonely Vendor", "ALPS2ALPS_OU", 900.0)
    assert out["split_suspected"] is False
    assert out["sibling_count"] == 0


# ── G2 ───────────────────────────────────────────────────────────────────
def test_sod_conflict_detects_same_actor():
    doc = {"budget_validated_by": "Rita", "confirmed_to_pay_by": None}
    assert gov.sod_conflict(doc, "confirm_payment", "Rita") == "budget_validated_by"
    assert gov.sod_conflict(doc, "confirm_payment", "Raitis") is None
    # mark-paid conflicts with either prior stage
    doc2 = {"budget_validated_by": "Rita", "confirmed_to_pay_by": "Raitis"}
    assert gov.sod_conflict(doc2, "mark_paid", "Rita") == "budget_validated_by"
    assert gov.sod_conflict(doc2, "mark_paid", "Raitis") == "confirmed_to_pay_by"
    assert gov.sod_conflict(doc2, "mark_paid", "Olga") is None


def test_sod_mode_default_and_override():
    assert gov.sod_mode() == "warn"   # default
    settings_svc.set_("sod_mode", "enforce", by="test")
    assert gov.sod_mode() == "enforce"
    settings_svc.set_("sod_mode", "off", by="test")
    assert gov.sod_mode() == "off"


# ── G3 ───────────────────────────────────────────────────────────────────
def test_vendor_bank_change_flags_new_iban():
    d1 = {"id": "tg_b1", "vendor": "tgvendor bank co",
          "parsed_json": json.dumps({"vendor": {"name": "tgvendor bank co", "iban": "LV11 1111 1111 1111 111"}})}
    # First record → baseline
    gov.record_vendor_bank(d1, by="test")
    # Same IBAN → not changed
    same = gov.check_vendor_bank_change(d1)
    assert same["changed"] is False
    # Different IBAN → changed
    d2 = {"id": "tg_b2", "vendor": "tgvendor bank co",
          "parsed_json": json.dumps({"vendor": {"name": "tgvendor bank co", "iban": "DE22 2222 2222 2222 2222 22"}})}
    changed = gov.check_vendor_bank_change(d2)
    assert changed["changed"] is True
    assert changed["current_iban"].startswith("DE22")


def test_vendor_bank_no_history_not_flagged():
    d = {"id": "tg_b9", "vendor": "brand new vendor",
         "parsed_json": json.dumps({"vendor": {"name": "brand new vendor", "iban": "FR33 3333 3333"}})}
    out = gov.check_vendor_bank_change(d)
    assert out["changed"] is False   # no prior record → first payment allowed


# ── Endpoint smoke ───────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    import app as flask_app
    from services import roles as roles_svc
    flask_app.app.testing = True
    flask_app.db.init_db()
    monkeypatch.setattr(roles_svc, "get_role", lambda name: roles_svc.ROLE_ADMIN)
    monkeypatch.setattr(roles_svc, "has_capability", lambda u, c: True)
    monkeypatch.setattr(roles_svc, "pc_in_scope", lambda u, pc: True)
    with flask_app.app.test_client() as c:
        yield c


def test_governance_endpoint(client):
    _seed("tg_e1", amount=900.0)
    _seed("tg_e2", amount=900.0)
    _seed("tg_e3", amount=900.0)
    r = client.get("/api/documents/tg_e3/governance", headers={"X-FIO-User": "admin"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["split"]["split_suspected"] is True
    assert body["sod_mode"] == "warn"
