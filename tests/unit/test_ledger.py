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
