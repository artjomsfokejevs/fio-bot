"""Integration test for the Card Audit Blueprint (Phase 7.1 refactor).

What to test:
- Blueprint is registered on the app
- All 7 routes (6 unique rules) resolve to the blueprint, not legacy inline funcs
"""
from __future__ import annotations


def test_card_audit_blueprint_registered():
    import app as fio_app
    rules = {str(r) for r in fio_app.app.url_map.iter_rules()
             if str(r).startswith("/api/card-audit")}
    expected = {
        "/api/card-audit/import",
        "/api/card-audit/transactions",
        "/api/card-audit/transactions/<tx_id>",
        "/api/card-audit/summary",
        "/api/card-audit/reconcile",
        "/api/card-audit/export",
    }
    assert expected.issubset(rules), f"missing routes: {expected - rules}"


def test_card_audit_endpoints_owned_by_blueprint():
    """Routes must come from `card_audit` blueprint, not from app.py inline."""
    import app as fio_app
    for r in fio_app.app.url_map.iter_rules():
        if str(r).startswith("/api/card-audit"):
            assert r.endpoint.startswith("card_audit."), \
                f"{r} is not owned by card_audit blueprint (endpoint={r.endpoint})"
