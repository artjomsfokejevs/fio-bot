"""Canonical profit-center codes — single source of truth (per BT4YOU ledger).

Synced with Artjoms + colleague feedback (2026-06-22):
  AA  Alps2Alps
  AH  Amitours Holding OÜ  ← NOT Mountly (corrected)
  AG  Amitours Group (holding ops bucket)
  AL  ALVEDA
  CF  MyPeak Finance
  MN  Mountly                ← was MT or AH historically
  MT  Medical Travel         ← new (separate stream from Mountly)
  SP  Skipasser              ← was SR
  RR  Rock2Rock

Legacy codes that may still appear in historical documents:
  SR  → SP (Skipasser)
  BK  Skibookers (decommissioned 2026-06-16; rows preserved, no new uploads)
  MT  Mountly (deprecated meaning; new MT = Medical Travel; old Mountly rows kept)
  AH  Mountly (deprecated meaning; new AH = Amitours Holding; old Mountly rows kept)
  PK  MyPeak Finance (alias for CF)

API stance (approach B — read-time mapping, DB untouched):
  • UI dropdowns expose CANONICAL codes only — no SR / BK in pickers.
  • Reads (analytics, exports, dashboards) translate legacy codes to
    canonical on-the-fly via `to_canonical()` — historical filtering works.
  • Writes use canonical codes always.
  • `legacy_aliases_of(canonical)` returns the list of historical codes
    that should also match when querying by a canonical code.

This file is the SINGLE place to consult when adding/renaming a stream.
"""
from __future__ import annotations

from typing import Dict, List, Optional

CANONICAL: Dict[str, str] = {
    "AA": "Alps2Alps",
    "AH": "Amitours Holding OÜ",
    "AG": "Amitours Group",
    "AL": "ALVEDA",
    "CF": "MyPeak Finance",
    "MN": "Mountly",
    "MT": "Medical Travel",
    "SP": "Skipasser",
    "RR": "Rock2Rock",
}

# legacy → canonical (read-time translation; not destructive)
# NOTE: "AH" and "MT" used to mean Mountly historically. We CANNOT auto-translate
# them here because the new meanings (Amitours Holding / Medical Travel) collide.
# Manual migration utility (scripts/migrate_pc_codes.py) is the disambiguation
# path — for now, AH and MT in historical rows keep their legacy meaning by
# inspection (e.g. via vendor patterns) rather than blind mapping.
LEGACY_TO_CANONICAL: Dict[str, str] = {
    "SR": "SP",   # Skipasser rename
    "PK": "CF",   # MyPeak alias
    # BK (Skibookers) decommissioned — kept as-is for historical visibility
    # but absent from CANONICAL above (no new uploads accepted).
}


def to_canonical(code: Optional[str]) -> Optional[str]:
    """Translate a legacy code to its canonical equivalent.

    Returns the canonical code if a translation exists; otherwise returns the
    input unchanged (canonical codes pass through; truly-unknown codes also
    pass through so debugging is possible).
    """
    if not code:
        return code
    return LEGACY_TO_CANONICAL.get(code, code)


def legacy_aliases_of(canonical: str) -> List[str]:
    """Return all historical codes that, after canonicalization, equal `canonical`.

    Useful for backward-compatible DB queries: when the user filters by "SP",
    the API should match rows with profit_center IN ('SP', 'SR').
    """
    out = [canonical]
    for legacy, target in LEGACY_TO_CANONICAL.items():
        if target == canonical:
            out.append(legacy)
    return out


def label_of(code: Optional[str]) -> str:
    """Human-readable name for any code (canonical or legacy)."""
    if not code:
        return "—"
    canonical = to_canonical(code)
    return CANONICAL.get(canonical, code)


def canonical_codes() -> List[str]:
    """Ordered list of canonical codes — for dropdowns + iteration."""
    return ["AA", "AH", "AG", "AL", "CF", "MN", "MT", "SP", "RR"]
