"""Route chase tasks to the right stakeholder per profit center.

Reads the BT4YOU snapshot of `holding_config.json` (kept inside the
bt4you_snapshot/ directory of the FIO repo) and derives a Business
Owner (BO) per stream. When Rita generates chase tasks for unmatched
bank transactions, each task can be auto-addressed to that owner —
no more "to whom do I send this?" guesswork.

Added 2026-06-22 (#92).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from services import pc_codes

logger = logging.getLogger(__name__)

# id-prefix → canonical PC code. Order matters (longest prefix first).
_PREFIX_TO_PC = [
    ("hold_",          "AH"),  # Amitours Holding nodes
    ("amp_",           "AH"),  # Holding products (AI / TransferHub / Charter etc.)
    ("vs_a2a",         "AA"),
    ("a2a_",           "AA"),
    ("vs_alveda",      "AL"),
    ("vs_skipasser",   "SP"),
    ("vs_sp_",         "SP"),
    ("vs_mypeak",      "CF"),
    ("vs_mp_",         "CF"),
    ("vs_mountly",     "MN"),
    ("vs_mo_",         "MN"),
]

# Root-of-stream node id → considered the Business Owner.
_BO_ROOTS = {
    "hold_ceo":      "AH",
    "a2a_ceo":       "AA",
    "vs_alveda":     "AL",
    "vs_skipasser":  "SP",
    "vs_mypeak":     "CF",
    "vs_mountly":    "MN",
}

# Cache parsed nodes so we don't re-read the file on every request
_CACHE: Dict[str, Any] = {"map": None, "loaded": False}


def _holding_config_path() -> Optional[str]:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(here, "bt4you_snapshot", "data", "holding_config.json")
    return p if os.path.exists(p) else None


def _load() -> Dict[str, Dict[str, Any]]:
    """Return {canonical_pc: {name, title, asana_gid, source_node_id}}."""
    if _CACHE["loaded"]:
        return _CACHE["map"] or {}
    out: Dict[str, Dict[str, Any]] = {}
    path = _holding_config_path()
    if not path:
        logger.warning("chase_routing: holding_config.json not found at expected path")
        _CACHE.update({"loaded": True, "map": out})
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        logger.exception("chase_routing: failed to read holding_config.json")
        _CACHE.update({"loaded": True, "map": out})
        return out

    for node in data.get("nodes", []):
        nid = node.get("id", "")
        if nid in _BO_ROOTS:
            pc = _BO_ROOTS[nid]
            name = node.get("person_name") or ""
            if name and name != "VACANT" and name != "—":
                out[pc] = {
                    "name":          name,
                    "title":         node.get("title") or "",
                    "asana_gid":     node.get("asana_gid"),
                    "source_node":   nid,
                    "stream_label":  pc_codes.label_of(pc),
                }
    _CACHE.update({"loaded": True, "map": out})
    return out


def stakeholder_for(pc: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return {name, title, asana_gid, ...} for the Business Owner of `pc`.

    Accepts either canonical (AA, MN, SP, ...) or legacy (SR, MT, AH-as-Mountly)
    codes — translation is applied via pc_codes.to_canonical first.
    Returns None when the PC has no mapped owner.
    """
    if not pc:
        return None
    canonical = pc_codes.to_canonical(pc) or pc
    return _load().get(canonical)


def all_stakeholders() -> List[Dict[str, Any]]:
    """List every (pc, stakeholder) pair — for the FIO Legend / debug view."""
    m = _load()
    return [
        {"profit_center": pc, **info}
        for pc, info in sorted(m.items())
    ]


def invalidate_cache() -> None:
    """Clear the in-memory cache — call when holding_config.json is reloaded."""
    _CACHE.update({"loaded": False, "map": None})
