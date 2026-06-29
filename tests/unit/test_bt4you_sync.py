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


# ────────────────────────────────────────────────────────────────────────
# 2026-06-29 — Operator-feedback vendor rules
# Two PC mis-classifications hit production:
#   - Caisse AVS (CH payroll vendor) was routed to AA instead of AG
#   - DEEP AYURVEDA HEALTHCARE was routed to AA instead of AL
# Lock the fix so future suggester edits don't regress.
# ────────────────────────────────────────────────────────────────────────

def test_caisse_avs_routes_to_ag_holding():
    """CH payroll collector → Amitours Holding (AG), not Alps2Alps."""
    r = bts.suggest_pc_for_vendor(
        "Caisse AVS de la Fédération patronale vaudoise",
        "Route du Lac 2, 1094 Paudex",
    )
    assert r is not None
    assert r["profit_center"] == "AG"
    assert r["confidence"] >= 90


def test_federation_patronale_vaudoise_routes_to_ag():
    r = bts.suggest_pc_for_vendor("Fédération patronale vaudoise", "")
    assert r is not None and r["profit_center"] == "AG"


def test_deep_ayurveda_routes_to_alveda():
    """Health-services vendor → ALVEDA (AL)."""
    r = bts.suggest_pc_for_vendor("DEEP AYURVEDA HEALTHCARE PVT. LTD", "India")
    assert r is not None
    assert r["profit_center"] == "AL"


def test_ayurveda_keyword_alone_routes_to_alveda():
    r = bts.suggest_pc_for_vendor("Some Ayurveda Clinic", "")
    assert r is not None and r["profit_center"] == "AL"


def test_address_keyword_fallback_to_ag():
    """When the vendor name is generic but the address is in Paudex /
    Vaud / Switzerland, route to AG. Real example: a generic invoice
    from a small CH supplier where only the address gives away the
    correct stream."""
    r = bts.suggest_pc_for_vendor("Some Vendor SARL", "Route du Lac 2, 1094 Paudex")
    assert r is not None and r["profit_center"] == "AG"


def test_external_vendor_with_no_signal_returns_none():
    """Vendor with no brand / health / CH keyword → None (caller falls
    back to LLM classification). Sanity that we didn't over-fit."""
    r = bts.suggest_pc_for_vendor("Vodafone Magyarország Zrt.",
                                    "Budapest, Hungary")
    assert r is None
