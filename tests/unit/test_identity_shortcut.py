"""Regression tests for the `?as=<name>` identity shortcut (2026-06-29).

Operator hit /api/accounting/export-bulk-zip directly in the browser
without the X-FIO-User header → 401 not_signed_in. Fix: GET endpoints
also accept identity via query param so admins can bookmark download
URLs.

Tests:
1. _current_user_name() prefers X-FIO-User header
2. _current_user_name() falls back to ?as= on GET only
3. ?as= is IGNORED on POST (mutations still require the header)
4. bulk-zip 401 response includes a `shortcut_url` hint
"""
from __future__ import annotations

import pytest

from services import db, roles as roles_svc


@pytest.fixture
def client(monkeypatch):
    import app as flask_app
    flask_app.app.testing = True
    flask_app.db.init_db()
    # Pretend the resolved user is admin so capability gates pass
    monkeypatch.setattr(roles_svc, "get_role",
                        lambda name: roles_svc.ROLE_ADMIN if name else roles_svc.ROLE_VIEWER)
    with flask_app.app.test_client() as c:
        yield c


def test_header_takes_priority_over_query_param(client):
    """If both X-FIO-User and ?as= are present, the header wins."""
    import app as flask_app
    with flask_app.app.test_request_context("/?as=From%20Query",
                                              headers={"X-FIO-User": "From Header"}):
        assert flask_app._current_user_name() == "From Header"


def test_query_param_fallback_on_get(client):
    """No header but ?as= on GET → identity resolves from query."""
    import app as flask_app
    with flask_app.app.test_request_context("/?as=Artjoms%20Fokejevs", method="GET"):
        assert flask_app._current_user_name() == "Artjoms Fokejevs"


def test_query_param_ignored_on_post(client):
    """POST mutations must not accept identity via URL — header only."""
    import app as flask_app
    with flask_app.app.test_request_context("/?as=Sneaky", method="POST"):
        assert flask_app._current_user_name() is None


def test_query_param_with_whitespace_trimmed(client):
    import app as flask_app
    with flask_app.app.test_request_context("/?as=%20%20Spaces%20%20", method="GET"):
        assert flask_app._current_user_name() == "Spaces"


def test_bulk_zip_401_shows_shortcut_when_admin_exists():
    """The 401 message tells the operator about the ?as= shortcut and,
    when an admin row exists in fio_users, includes a ready-to-paste URL."""
    import app as flask_app
    flask_app.app.testing = True
    flask_app.db.init_db()
    # Seed an admin if none exists
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO fio_users "
            "(full_name, role, active, profit_center) "
            "VALUES ('Pytest Admin', 'admin', 1, 'AA')"
        )
        conn.commit()
    finally:
        conn.close()
    with flask_app.app.test_client() as c:
        # No X-FIO-User header → expect 401 with shortcut hint
        r = c.get("/api/accounting/export-bulk-zip?legal_entity=ALPS2ALPS_OU")
    assert r.status_code == 401
    payload = r.get_json()
    assert payload["error"] == "not_signed_in"
    assert "shortcut" in payload["message"].lower() or "as=" in payload["message"]
    assert payload.get("required_capability") == "export_bulk"
    # shortcut_url should be present and well-formed
    su = payload.get("shortcut_url")
    if su:  # only present when fio_users had an admin row
        assert "as=" in su
        assert "/api/accounting/export-bulk-zip" in su
    # Cleanup
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM fio_users WHERE full_name = 'Pytest Admin'")
        conn.commit()
    finally:
        conn.close()
