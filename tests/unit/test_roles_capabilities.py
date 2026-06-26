"""Tests for FB-L granular sub-permissions in services/roles.py."""
from __future__ import annotations

import pytest

from services import db, roles


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


def _make_user(name, role, permissions=None, pc_scope=None):
    from services import users as users_svc
    return users_svc.create_user({
        "full_name": name,
        "role": role,
        "permissions": permissions,
        "pc_scope": pc_scope,
    })


def test_user_with_no_permissions_inherits_role_defaults(monkeypatch):
    _make_user("pytest_rita_full", "bookkeeper")
    # Stub roles file to map name → role
    monkeypatch.setattr(roles, "get_role", lambda n: "bookkeeper" if n == "pytest_rita_full" else "viewer")
    caps = roles.user_capabilities("pytest_rita_full")
    assert "approve_budget" in caps
    assert "mark_paid" in caps
    assert "post_to_pnl" in caps


def test_user_with_restricted_permissions_only_gets_those(monkeypatch):
    _make_user("pytest_olga_limited", "bookkeeper", permissions="mark_paid,view_revenue")
    monkeypatch.setattr(roles, "get_role", lambda n: "bookkeeper" if n == "pytest_olga_limited" else "viewer")
    caps = roles.user_capabilities("pytest_olga_limited")
    assert "mark_paid" in caps
    assert "view_revenue" in caps
    assert "approve_budget" not in caps          # role default but excluded
    assert roles.has_capability("pytest_olga_limited", "mark_paid")
    assert not roles.has_capability("pytest_olga_limited", "approve_budget")


def test_pc_scope_restricts_pcs(monkeypatch):
    _make_user("pytest_dima_scoped", "bookkeeper", pc_scope="CF,SP")
    monkeypatch.setattr(roles, "get_role", lambda n: "bookkeeper" if n == "pytest_dima_scoped" else "viewer")
    assert roles.pc_in_scope("pytest_dima_scoped", "CF")
    assert roles.pc_in_scope("pytest_dima_scoped", "SP")
    assert not roles.pc_in_scope("pytest_dima_scoped", "AA")
    assert not roles.pc_in_scope("pytest_dima_scoped", "MN")
    # Legacy translation: SR → SP
    assert roles.pc_in_scope("pytest_dima_scoped", "SR")


def test_pc_scope_blank_means_unrestricted(monkeypatch):
    _make_user("pytest_rita_unrest", "admin")
    monkeypatch.setattr(roles, "get_role", lambda n: "admin")
    assert roles.user_pc_scope("pytest_rita_unrest") is None
    assert roles.pc_in_scope("pytest_rita_unrest", "AA")
    assert roles.pc_in_scope("pytest_rita_unrest", "MN")


def test_viewer_has_only_self_service_capabilities(monkeypatch):
    """Viewer can mark own notifications read; nothing else."""
    monkeypatch.setattr(roles, "get_role", lambda n: "viewer")
    caps = roles.user_capabilities("anybody")
    assert caps == {"use_notifications"}
    assert not roles.has_capability("anybody", "approve_budget")
    assert roles.has_capability("anybody", "use_notifications")


def test_unknown_user_treated_as_viewer():
    # No fixture, no monkeypatch — just confirm graceful default
    caps = roles.user_capabilities("does_not_exist")
    assert caps == {"use_notifications"}
