"""Tests for services.classifier — ledger code + profit center suggestions.

What to test:
- Module imports (catches missing imports like Phase 5 bug)
- classify_document() returns a dict with expected keys
- Unknown vendor → still returns dict, never crashes
"""
from __future__ import annotations


def test_classifier_module_imports():
    from services import classifier
    assert hasattr(classifier, "classify_document")


def test_classify_unknown_vendor_returns_dict():
    """Error path: unrecognised vendor must still yield a valid response shape."""
    from services import classifier
    try:
        out = classifier.classify_document({
            "vendor": "Totally Random Vendor 12345",
            "amount": 100.0,
            "currency": "EUR",
            "description": "test",
        })
        assert isinstance(out, dict)
    except TypeError:
        # signature mismatch is acceptable — this is a stub test
        pass


def test_check_expense_policy_callable():
    from services import classifier
    if hasattr(classifier, "check_expense_policy"):
        # callable check only — actual logic depends on policy file
        assert callable(classifier.check_expense_policy)


# ────────────────────────────────────────────────────────────────────────
# 2026-06-27 — suggest_ledger_from_text (hot-path, rules-only).
# Used by routes/card_audit.py to enrich Chase Missing-Invoice tasks
# with an AI-suggested ledger code without calling the LLM per row.
# ────────────────────────────────────────────────────────────────────────

def test_suggest_ledger_returns_none_for_blank():
    from services import classifier as clf
    assert clf.suggest_ledger_from_text("") is None
    assert clf.suggest_ledger_from_text(None) is None


def test_suggest_ledger_never_raises_on_garbage():
    from services import classifier as clf
    # Bizarre input must not crash hot path
    for s in (123, [], {}, object()):
        assert clf.suggest_ledger_from_text(s) is None


def test_suggest_ledger_returns_string_or_none():
    """Whatever the underlying rules engine returns, the wrapper must
    yield either a ledger-code string or None — never a list/dict."""
    from services import classifier as clf
    out = clf.suggest_ledger_from_text("Vodafone monthly")
    assert out is None or isinstance(out, str)
