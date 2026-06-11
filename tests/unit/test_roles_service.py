"""Regression tests for services/roles.py — role enum + tab visibility."""
from __future__ import annotations

import pytest

from services import roles


def test_all_roles_enum_includes_known_values():
    assert roles.ROLE_ADMIN in roles.ALL_ROLES
    assert roles.ROLE_BOOKKEEPER in roles.ALL_ROLES
    assert roles.ROLE_VIEWER in roles.ALL_ROLES


def test_get_role_unknown_user_defaults_to_viewer():
    assert roles.get_role("__nobody__") == roles.ROLE_VIEWER


def test_get_role_handles_none():
    """None / empty input must not crash."""
    assert roles.get_role(None) == roles.ROLE_VIEWER
    assert roles.get_role("") == roles.ROLE_VIEWER


def test_tabs_for_role_admin_sees_everything():
    tabs = roles.tabs_for_role(roles.ROLE_ADMIN)
    assert "upload" in tabs
    assert "approve" in tabs
    assert "admin" in tabs


def test_tabs_for_role_viewer_excludes_admin():
    tabs = roles.tabs_for_role(roles.ROLE_VIEWER)
    assert "admin" not in tabs


def test_tabs_for_role_bookkeeper_sees_card_audit_and_confirm():
    """Bookkeeper has the expanded month-close + payment scope."""
    tabs = roles.tabs_for_role(roles.ROLE_BOOKKEEPER)
    assert "card-audit" in tabs
    assert "confirm-payment" in tabs


def test_tabs_for_role_unknown_role_defaults_safe():
    """Unknown role string must NOT raise — fallback to a safe subset."""
    tabs = roles.tabs_for_role("ghost_role")
    assert isinstance(tabs, list)
