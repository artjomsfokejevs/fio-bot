"""Regression tests for services/paying_accounts.py."""
from __future__ import annotations

import pytest
import sqlite3

from services import db, paying_accounts as pa


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM paying_accounts WHERE label LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM paying_accounts WHERE label LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()


def test_create_requires_label():
    with pytest.raises(ValueError, match="label"):
        pa.create_account({"bank": "Revolut"})


def test_create_round_trip_with_iban_currency():
    a = pa.create_account({
        "label": "pytest_acc1", "bank": "Revolut", "iban": "EE001",
        "currency": "eur", "legal_entity": "ALPS2ALPS_OU",
    })
    assert a["currency"] == "EUR"  # normalised upper-case
    assert a["active"] == 1


def test_create_unique_label():
    pa.create_account({"label": "pytest_dup"})
    with pytest.raises(sqlite3.IntegrityError):
        pa.create_account({"label": "pytest_dup"})


def test_soft_delete_preserves_row():
    a = pa.create_account({"label": "pytest_soft"})
    pa.delete_account(a["id"])
    again = pa.get_account(a["id"])
    assert again is not None
    assert again["active"] == 0


def test_list_filters_legal_entity():
    pa.create_account({"label": "pytest_aa1", "legal_entity": "ALPS2ALPS_OU"})
    pa.create_account({"label": "pytest_bk1", "legal_entity": "DMS"})
    rows = pa.list_accounts(legal_entity="ALPS2ALPS_OU")
    labels = {r["label"] for r in rows}
    assert "pytest_aa1" in labels
    assert "pytest_bk1" not in labels


def test_update_unknown_keys_silently_ignored():
    a = pa.create_account({"label": "pytest_upd", "bank": "X"})
    pa.update_account(a["id"], {"bank": "Y", "random_field": "garbage"})
    again = pa.get_account(a["id"])
    assert again["bank"] == "Y"
