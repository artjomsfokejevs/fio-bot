"""Regression tests for services/policy_rules.py (Phase 1 P1.2)."""
from __future__ import annotations

import pytest

from services import db, policy_rules


@pytest.fixture(autouse=True)
def _clean():
    db.init_db()
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM policy_rules WHERE code LIKE 'pytest_%'")
        conn.execute("DELETE FROM policy_rules_history WHERE rule_code LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()
    yield
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM policy_rules WHERE code LIKE 'pytest_%'")
        conn.execute("DELETE FROM policy_rules_history WHERE rule_code LIKE 'pytest_%'")
        conn.commit()
    finally:
        conn.close()


def _payload(**over):
    base = {
        "code": "pytest_office_max",
        "policy_name": "office_supplies",
        "field": "max_per_item",
        "level": "red",
        "threshold_eur": 500.0,
        "unit": "per_invoice",
        "owner": "CEO",
    }
    base.update(over)
    return base


def test_create_persists_and_history_entry():
    r = policy_rules.create_rule(_payload(), by="pytest")
    assert r["id"] > 0
    assert r["code"] == "pytest_office_max"
    hist = policy_rules.history_for(r["id"])
    assert any(h["change_type"] == "create" for h in hist)


def test_validate_rejects_bad_level():
    with pytest.raises(ValueError):
        policy_rules.create_rule(_payload(level="purple"), by="pytest")


def test_validate_rejects_negative_threshold():
    with pytest.raises(ValueError):
        policy_rules.create_rule(_payload(threshold_eur=-1), by="pytest")


def test_update_changes_threshold_and_bumps_cache():
    r = policy_rules.create_rule(_payload(), by="pytest")
    eff_before = policy_rules.get_effective_policies()
    assert eff_before["office_supplies"]["max_per_item"] == 500.0
    policy_rules.update_rule(r["id"], {"threshold_eur": 999.0}, by="pytest")
    eff_after = policy_rules.get_effective_policies()
    assert eff_after["office_supplies"]["max_per_item"] == 999.0


def test_delete_is_soft():
    r = policy_rules.create_rule(_payload(), by="pytest")
    assert policy_rules.delete_rule(r["id"], by="pytest") is True
    got = policy_rules.get_rule(r["id"])
    assert got["active"] == 0
    active = [x for x in policy_rules.list_rules(active_only=True)
              if x["code"] == "pytest_office_max"]
    assert active == []
