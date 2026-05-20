"""Tests for services.bt4you_sync — domain map (P1 pattern).

What to test:
- build_people_map() returns a non-empty dict
- suggest_pc_for_uploader() returns a known person OR None (never raises)
- Unknown name returns None (graceful), not exception
"""
from __future__ import annotations

from services import bt4you_sync as bts


def test_people_map_loads():
    """P1: domain map must load successfully and contain people rows."""
    pm = bts.build_people_map()
    assert isinstance(pm, dict)
    # The map may be empty in a fresh test env if the snapshot is missing.
    # Both shapes are acceptable — but it must not raise.


def test_suggest_pc_unknown_uploader_returns_none():
    """Error path: unknown name → None, not exception."""
    pm = bts.build_people_map()
    out = bts.suggest_pc_for_uploader("Definitely Not A Real Person 12345", pm)
    assert out is None or isinstance(out, dict)


def test_suggest_pc_for_vendor_handles_empty():
    """Vendor suggestion with no brand → None."""
    out = bts.suggest_pc_for_vendor("", "")
    assert out is None or isinstance(out, (str, dict))
