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


def test_unknown_currency_returns_none_not_one(monkeypatch):
    """C7 (2026-07-08): an unknown currency with no live rate AND no
    static fallback must return rate=None / source='unavailable', never a
    silent 1.0 (which booked a 10,000 AED invoice as EUR 10,000).
    """
    # Force the live ECB fetch to fail so we exercise the no-fallback path.
    import urllib.request
    def _boom(*a, **k):
        raise OSError("network disabled in test")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    rate, date, source = fx.get_rate("ZZZ")
    assert rate is None
    assert source == "unavailable"


def test_convert_unknown_currency_flags_manual_fx(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no net")))
    out = fx.convert_to_eur(10000.0, "ZZZ")
    assert out["amount_eur"] is None
    assert out["needs_manual_fx"] is True
    assert out["amount_orig"] == 10000.0


def test_get_rate_returns_three_tuple():
    """Contract: always returns (rate, date_str, source_str)."""
    out = fx.get_rate("USD")
    assert isinstance(out, tuple) and len(out) == 3
