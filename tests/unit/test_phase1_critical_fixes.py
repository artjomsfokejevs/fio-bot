"""Phase 1 «Stop-crank» regression tests (2026-07-08).

Covers the critical fixes from the engineering review:
  C1 — idempotent approve (no double-post to P&L)
  C6 — policy check uses EUR amount not original currency
  C8 — money parser distinguishes thousands vs decimal separator
  C9 — SQLite WAL + foreign_keys pragmas active
"""
from __future__ import annotations

import pytest

from services import db
from services.money import parse_money


# ── C8: money parser ────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", 1234.56),   # US
    ("1.234,56", 1234.56),   # EU
    ("1,500", 1500.0),       # US thousands, no decimals — the €1,500→€1.50 bug
    ("1.500", 1500.0),       # EU thousands, no decimals
    ("1,50", 1.50),          # EU decimal
    ("1500.00", 1500.0),
    ("(€1,234)", -1234.0),   # parenthesised negative
    ("€1,234.56", 1234.56),
    ("-45.20", -45.2),
    ("1,234,567.89", 1234567.89),
    ("4.50", 4.5),
    ("", None),
    ("-", None),
    ("#REF!", None),
])
def test_money_parser_thousands_vs_decimal(raw, expected):
    got = parse_money(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


# ── C9: SQLite pragmas ──────────────────────────────────────────────────
def test_connection_has_wal_and_fk_pragmas():
    conn = db.get_connection()
    try:
        jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()
    assert str(jm).lower() == "wal"
    assert fk == 1


# ── C6: policy check uses EUR ───────────────────────────────────────────
def test_policy_uses_eur_amount_not_original():
    from services.classifier import check_expense_policy
    # 22,500 AMD ≈ €53 — must NOT trip the €500 business-dinner cap
    # because amount_eur is well under it.
    parsed = {
        "money": {"total_amount": 22500.0, "amount_eur": 53.0, "currency": "AMD"},
        "vendor": {"name": "Yerevan Bistro"},
        "line_items": [{"description": "business dinner team lunch"}],
    }
    warnings = check_expense_policy(parsed, {"codes": []})
    reds = [w for w in warnings if w.get("level") == "red"]
    # No RED purely from the amount cap (the €53 EUR value is under €200/€500).
    assert not any("xceed" in (w.get("message") or "").lower() for w in reds)


# ── C1: idempotent approve ──────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    import app as flask_app
    from services import roles as roles_svc
    flask_app.app.testing = True
    flask_app.db.init_db()
    monkeypatch.setattr(roles_svc, "get_role", lambda name: roles_svc.ROLE_ADMIN)
    monkeypatch.setattr(roles_svc, "has_capability", lambda user, cap: True)
    monkeypatch.setattr(roles_svc, "pc_in_scope", lambda user, pc: True)
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM documents WHERE id LIKE 'test_p1_%'")
        conn.commit()
    finally:
        conn.close()


def _seed(doc_id, status="classified"):
    conn = db.get_connection()
    from datetime import datetime
    try:
        conn.execute(
            "INSERT INTO documents (id, filename, file_size, uploaded_at, "
            " uploaded_by, status, amount, currency, profit_center, ledger_code, period, vendor) "
            "VALUES (?, 'x.pdf', 0, ?, 'pytest', ?, 100.0, 'EUR', 'AA', '6_1', '2026-07', 'V')",
            (doc_id, datetime.utcnow().isoformat(), status),
        )
        conn.commit()
    finally:
        conn.close()


def test_approve_is_idempotent(client):
    _seed("test_p1_approve", status="classified")
    r1 = client.post("/api/documents/test_p1_approve/approve",
                     headers={"X-FIO-User": "admin"}, json={})
    assert r1.status_code == 200
    # Second approve on the now-posted doc must be rejected (409), not
    # re-posted to P&L.
    r2 = client.post("/api/documents/test_p1_approve/approve",
                     headers={"X-FIO-User": "admin"}, json={})
    assert r2.status_code == 409
    assert r2.get_json()["error"] == "already_processed"


def test_delete_paid_doc_refused_without_force(client):
    _seed("test_p1_paid", status="paid")
    r = client.delete("/api/documents/test_p1_paid", headers={"X-FIO-User": "admin"})
    assert r.status_code == 409
    assert r.get_json()["error"] == "refuse_delete_paid"
    # With force it goes through
    r2 = client.delete("/api/documents/test_p1_paid?force=1", headers={"X-FIO-User": "admin"})
    assert r2.status_code == 200


def test_card_audit_blocks_non_finance_role(monkeypatch):
    import app as flask_app
    from services import roles as roles_svc
    flask_app.app.testing = True
    flask_app.db.init_db()
    monkeypatch.setattr(roles_svc, "get_role", lambda name: "viewer")
    with flask_app.app.test_client() as c:
        r = c.get("/api/card-audit/transactions", headers={"X-FIO-User": "someone"})
        assert r.status_code == 403
