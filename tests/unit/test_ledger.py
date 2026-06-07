"""Tests for services.ledger — 47-code schema (Phase 3.4) + actuals posting."""
from __future__ import annotations

import json
from pathlib import Path


def test_ledger_schema_has_47_codes():
    """Phase 3.4: schema must contain at least 47 codes (expanded from 35)."""
    root = Path(__file__).resolve().parent.parent.parent
    schema = json.loads((root / "data" / "ledger_schema.json").read_text())
    codes = schema["codes"]
    assert isinstance(codes, list)
    assert len(codes) >= 47, f"expected ≥47 ledger codes, got {len(codes)}"


def test_ledger_schema_contains_phase35_codes():
    """Phase 3.4 added HR / SUB / CON family codes — at least one of each must exist."""
    root = Path(__file__).resolve().parent.parent.parent
    schema = json.loads((root / "data" / "ledger_schema.json").read_text())
    codes = {c["code"] for c in schema["codes"]}
    families = {"HR_": False, "SUB_": False, "CON_": False, "CTR_": False}
    for code in codes:
        for prefix in families:
            if code.startswith(prefix):
                families[prefix] = True
    missing = [p for p, ok in families.items() if not ok]
    assert not missing, f"missing ledger families: {missing}"


def test_ledger_module_imports():
    """Catches the 'Response not imported' style bug."""
    from services import ledger
    assert hasattr(ledger, "post_to_actuals")
    assert hasattr(ledger, "AllocationValidationError")


# ---------------------------------------------------------------------------
# Allocation validator (Top-10 fix #4)
# ---------------------------------------------------------------------------


def test_allocations_empty_is_noop():
    """No allocations = single-PC posting, validator must not raise."""
    from services.ledger import _validate_allocations
    _validate_allocations([], 100.0)  # must not raise


def test_allocations_percentages_must_sum_to_100():
    from services.ledger import _validate_allocations, AllocationValidationError
    import pytest as _pt
    rows = [
        {"profit_center": "AA", "ledger_code": "SUB_SAAS", "percentage": 60},
        {"profit_center": "BK", "ledger_code": "SUB_SAAS", "percentage": 30},  # 90, not 100
    ]
    with _pt.raises(AllocationValidationError) as exc:
        _validate_allocations(rows, 100.0)
    assert "90" in str(exc.value.reasons) or "100" in str(exc.value.reasons)


def test_allocations_amounts_must_equal_total():
    from services.ledger import _validate_allocations, AllocationValidationError
    import pytest as _pt
    rows = [
        {"profit_center": "AA", "ledger_code": "SUB_SAAS", "amount": 60.0},
        {"profit_center": "BK", "ledger_code": "SUB_SAAS", "amount": 30.0},  # total = 90, doc = 100
    ]
    with _pt.raises(AllocationValidationError):
        _validate_allocations(rows, 100.0)


def test_allocations_row_with_neither_field_rejected():
    """Silent 0.0 fall-through was the actual bug — this test pins the fix."""
    from services.ledger import _validate_allocations, AllocationValidationError
    import pytest as _pt
    rows = [
        {"profit_center": "AA", "ledger_code": "SUB_SAAS", "percentage": 100},
        {"profit_center": "BK", "ledger_code": "SUB_SAAS"},  # no amount, no percentage
    ]
    with _pt.raises(AllocationValidationError):
        _validate_allocations(rows, 100.0)


def test_allocations_happy_path_percentages():
    from services.ledger import _validate_allocations
    rows = [
        {"profit_center": "AA", "ledger_code": "SUB_SAAS", "percentage": 60},
        {"profit_center": "BK", "ledger_code": "SUB_SAAS", "percentage": 40},
    ]
    _validate_allocations(rows, 100.0)  # must not raise


def test_allocations_happy_path_amounts_within_tolerance():
    """Rounding from % → € often leaves ~0.01€ wobble; tolerance must accept it."""
    from services.ledger import _validate_allocations
    rows = [
        {"profit_center": "AA", "ledger_code": "SUB_SAAS", "amount": 33.33},
        {"profit_center": "BK", "ledger_code": "SUB_SAAS", "amount": 33.33},
        {"profit_center": "AG", "ledger_code": "SUB_SAAS", "amount": 33.34},
    ]
    _validate_allocations(rows, 100.0)  # must not raise
