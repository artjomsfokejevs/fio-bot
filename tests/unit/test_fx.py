"""Tests for services.fx — ECB rate lookup with cache + fallback.

get_rate(from_currency, on_date=None) -> Tuple[float, str, str]
                                          (rate, effective_date, source)
"""
from __future__ import annotations

from services import fx


def test_eur_returns_identity():
    """Happy path: EUR → EUR is identity rate."""
    rate, date, source = fx.get_rate("EUR")
    assert rate == 1.0
    assert source in ("identity", "EUR", "ecb", "fallback")


def test_unknown_currency_falls_back_safely():
    """Error path: unknown currency must NOT crash; must return tuple shape.

    NOTE: real finding — fx.py defaults to 1.0 with a 'DANGER' warning for
    unknown codes. This is documented behaviour, but should be tightened
    in a follow-up. Test pins the contract.
    """
    rate, date, source = fx.get_rate("ZZZ")
    assert isinstance(rate, float)
    assert source in ("fallback", "ecb", "identity")


def test_get_rate_returns_three_tuple():
    """Contract: always returns (rate, date_str, source_str)."""
    out = fx.get_rate("USD")
    assert isinstance(out, tuple) and len(out) == 3
