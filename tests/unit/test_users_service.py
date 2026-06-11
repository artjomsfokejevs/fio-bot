"""Regression tests for services/users.py (FIO-managed users roster)."""
from __future__ import annotations

import pytest
import sqlite3

from services import db, users


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM fio_users WHERE full_name LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM fio_users WHERE full_name LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()


def test_create_requires_full_name():
    with pytest.raises(ValueError, match="full_name"):
        users.create_user({"role": "uploader"})


def test_create_rejects_unknown_role():
    with pytest.raises(ValueError, match="role"):
        users.create_user({"full_name": "pytest_x", "role": "ghost"})


def test_create_persists_and_get_round_trip():
    u = users.create_user({"full_name": "pytest_alice", "role": "uploader",
                           "email": "alice@x.com", "profit_center": "AA"})
    assert u["full_name"] == "pytest_alice"
    assert u["active"] == 1
    again = users.get_user(u["id"])
    assert again["email"] == "alice@x.com"


def test_unique_full_name_constraint():
    users.create_user({"full_name": "pytest_dup", "role": "uploader"})
    with pytest.raises(sqlite3.IntegrityError):
        users.create_user({"full_name": "pytest_dup", "role": "approver"})


def test_update_partial_fields_only():
    u = users.create_user({"full_name": "pytest_bob", "role": "uploader"})
    updated = users.update_user(u["id"], {"email": "bob@x.com", "ignore_me": "x"})
    assert updated["email"] == "bob@x.com"
    assert updated["role"] == "uploader"  # untouched


def test_delete_is_soft_delete():
    u = users.create_user({"full_name": "pytest_carol", "role": "uploader"})
    users.delete_user(u["id"])
    # Still readable
    still_there = users.get_user(u["id"])
    assert still_there is not None
    assert still_there["active"] == 0


def test_list_filters_active_only():
    a = users.create_user({"full_name": "pytest_active", "role": "uploader"})
    b = users.create_user({"full_name": "pytest_inactive", "role": "uploader"})
    users.delete_user(b["id"])
    rows = users.list_users(active_only=True)
    names = {r["full_name"] for r in rows}
    assert "pytest_active" in names
    assert "pytest_inactive" not in names
