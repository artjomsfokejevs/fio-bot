"""Regression tests for Top-10 P1.1: Revolut Business CSV detection.

The 2026-06-11 Top-10 self-review caught that the Revolut signature was
based on imagined column names (`currency`, `started date`) instead of
the actual export (`Payment currency`, `Date started (UTC)`). One real
Revolut export now lives in the test as a fixture string so a future
detector refactor can't silently regress.
"""
from __future__ import annotations

from services import card_audit


REVOLUT_HEADER = (
    "Date started (UTC),Date completed (UTC),ID,Type,State,Description,"
    "Reference,Payer,Card number,Card label,Card state,Orig currency,"
    "Orig amount,Payment currency,Amount,Total amount,Exchange rate,Fee,"
    "Fee currency,Balance,Account,Beneficiary account number,"
    "Beneficiary sort code or routing number,Beneficiary IBAN,"
    "Beneficiary BIC,MCC,Related transaction id,Spend program,"
    "Card references"
)


def _headers_list() -> list:
    return [h.strip() for h in REVOLUT_HEADER.split(",")]


def test_revolut_detector_matches_real_export_header():
    """The actual Revolut Business CSV header must match the spec, not
    the docs/imagined version."""
    spec = card_audit.detect_format(_headers_list())
    assert spec["id"] == "revolut", (
        f"Detector picked '{spec['id']}' — Revolut signature drifted "
        f"from the real export. See retro 2026-06-11 Q93."
    )


def test_revolut_detector_does_not_fall_through_to_generic():
    """Safety net: if 'revolut' detection breaks, the row would fall to
    generic and lose column mapping. Pin the contract."""
    spec = card_audit.detect_format(_headers_list())
    assert spec["id"] != "generic"


def test_revolut_field_mappings_resolve_real_columns():
    """Each field in the spec must point to at least one column that
    actually exists in the real export (case-insensitive)."""
    spec = card_audit.detect_format(_headers_list())
    norm_headers = {h.strip().lower() for h in _headers_list()}
    for field_name, candidates in spec["fields"].items():
        if not isinstance(candidates, list):
            continue
        # At least one candidate must exist in the real headers
        assert any(c.lower() in norm_headers for c in candidates), (
            f"Field `{field_name}` maps to {candidates} — none of them "
            f"appear in the real Revolut export headers."
        )
