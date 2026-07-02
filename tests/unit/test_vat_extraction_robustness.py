"""VAT extraction survives OCR-broken spacing (2026-07-02 op-feedback).

Operator's ExpertAssist / Sipalto invoices had valid VATs
(LV45403022922) that our parser missed because the PDF text layer
carried stray spaces between the digits. The normalisation pass
collapses spaces inside digit runs before running the strict format
regexes.
"""
from __future__ import annotations

import pytest

from services.parser import _extract_vat_details, _normalise_digit_runs


@pytest.mark.parametrize("raw,expected_vat", [
    ("VAT LV45403022922 issued in Riga",             "LV45403022922"),
    ("VAT LV 45403022922 issued in Riga",            "LV45403022922"),
    ("PVN LV  45403022922 issued",                   "LV45403022922"),
    ("VAT LV 4540 3022 922",                         "LV45403022922"),
    ("VAT LV 45 40 30 22 92 2",                      "LV45403022922"),
    ("VAT PL 5252344078",                            "PL5252344078"),
    ("EE 100931558",                                 "EE100931558"),
    ("DE 811115368",                                 "DE811115368"),
])
def test_vat_extracted_across_spacing_variants(raw, expected_vat):
    out = _extract_vat_details(raw)
    assert out.get("vat_number") == expected_vat, out


def test_normalise_collapses_multiple_spaces_between_digits():
    got = _normalise_digit_runs("LV 4 5  4  0 3 0 2 2 9 2 2  end")
    assert "LV45403022922" in got


def test_normalise_leaves_non_digit_text_alone():
    got = _normalise_digit_runs("Riga LV-1010, Brivibas iela 12")
    assert "Riga" in got and "Brivibas" in got


def test_no_vat_returns_empty():
    out = _extract_vat_details("no tax number here, just prose")
    assert "vat_number" not in out
