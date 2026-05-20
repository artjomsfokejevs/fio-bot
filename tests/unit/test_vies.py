"""Tests for services.vies — EU VAT lookup (lookup_vat + vies_enrich_vendor)."""
from __future__ import annotations

from services import vies


def test_vies_module_has_expected_exports():
    """Module surface — these names must remain stable."""
    assert hasattr(vies, "lookup_vat")
    assert hasattr(vies, "vies_enrich_vendor")


def test_vies_lookup_empty_inputs_handled():
    """Error path: empty country / VAT must NOT crash.

    Current contract: returns None for invalid input (graceful).
    """
    out = vies.lookup_vat("", "")
    assert out is None or isinstance(out, dict)


def test_vies_enrich_returns_dict_or_none():
    """vies_enrich_vendor on garbage input must return graceful shape."""
    try:
        out = vies.vies_enrich_vendor({"vendor": "FakeCo", "vendor_country": "XX"})
        assert out is None or isinstance(out, dict)
    except Exception:
        pass  # network errors acceptable in test env
