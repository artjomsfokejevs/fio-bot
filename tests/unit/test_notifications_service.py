"""Regression tests for services/notifications.py (Phase 2)."""
from __future__ import annotations

import pytest

from services import db, notifications


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM notifications WHERE title LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM notifications WHERE title LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()


def test_create_persists():
    n = notifications.create(kind="ceo_approved_invoice", title="pytest_x",
                             recipient_role="bookkeeper", severity="info")
    assert n["id"] > 0
    assert n["read_at"] is None


def test_validate_severity():
    with pytest.raises(ValueError, match="severity"):
        notifications.create(kind="x", title="pytest_y", severity="critical")


def test_validate_required():
    with pytest.raises(ValueError, match="kind"):
        notifications.create(kind="", title="pytest_z")


def test_for_user_matches_role_broadcast():
    notifications.create(kind="ceo_approved_invoice", title="pytest_role_match",
                         recipient_role="bookkeeper")
    items = notifications.for_user(user_name="Anyone", role="bookkeeper")
    assert any(i["title"] == "pytest_role_match" for i in items)
    items_other = notifications.for_user(user_name="Anyone", role="admin")
    assert not any(i["title"] == "pytest_role_match" for i in items_other)


def test_for_user_matches_specific_user():
    notifications.create(kind="system", title="pytest_user_only",
                         recipient_user="Rita Petukhova")
    items = notifications.for_user(user_name="Rita Petukhova", role="bookkeeper")
    assert any(i["title"] == "pytest_user_only" for i in items)
    items_other = notifications.for_user(user_name="Someone Else", role="bookkeeper")
    assert not any(i["title"] == "pytest_user_only" for i in items_other)


def test_for_user_broadcast_to_all_when_role_and_user_null():
    notifications.create(kind="system", title="pytest_broadcast")
    items = notifications.for_user(user_name="Whoever", role="viewer")
    assert any(i["title"] == "pytest_broadcast" for i in items)


def test_mark_read_idempotent():
    n = notifications.create(kind="x", title="pytest_read", recipient_role="admin")
    assert notifications.mark_read(n["id"], by="pytest") is True
    # Second call returns False (already read)
    assert notifications.mark_read(n["id"], by="pytest") is False


def test_unread_count_drops_after_mark_read():
    notifications.create(kind="x", title="pytest_unread1", recipient_role="admin")
    notifications.create(kind="x", title="pytest_unread2", recipient_role="admin")
    before = notifications.unread_count("Anyone", "admin")
    assert before >= 2
    items = notifications.for_user("Anyone", "admin", only_unread=True)
    target = next(i for i in items if i["title"] == "pytest_unread1")
    notifications.mark_read(target["id"])
    after = notifications.unread_count("Anyone", "admin")
    assert after == before - 1


def test_mark_all_read_returns_count():
    notifications.create(kind="x", title="pytest_all1", recipient_role="admin")
    notifications.create(kind="x", title="pytest_all2", recipient_role="admin")
    notifications.mark_all_read("Anyone", "admin")
    assert notifications.unread_count("Anyone", "admin") == 0
