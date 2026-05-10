"""BT4YOU integration: pull departments and people roster from the executive bot.

Reads BT4YOU's local JSON config files directly (single source of truth):
  - holding_config.json -- 81 nodes (people, roles, asana_gid, parent)
  - brand.json -- 6 departments with brand colors
  - hr_config.json + bd_config.json + products_config.json -- cohorts

These feed FIO with:
  - Profit-center to department mapping (for Alps2Alps and others)
  - Names autocomplete (uploaded_by field)
  - Department selector when AA / AG / etc. is chosen
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "get_bt4you_data_path",
    "load_departments",
    "load_people",
    "load_profit_center_departments",
]

# Default location of BT4YOU on this machine. Overridable via env.
_DEFAULT_BT4YOU_PATH = (
    "/Users/artjomsfokejevs/Library/Mobile Documents/"
    "com~apple~CloudDocs/CLAUDE/BT4YOU_MEETING_TRACKER"
)

# Profit-center -> default department mapping. AA (Alps2Alps) is the primary
# operational stream and exposes the full department list. Other streams have
# the same structure but smaller teams.
_PC_TO_DEPARTMENTS = {
    "AA": ["pm", "bd", "fa", "ops", "mkt", "hr"],  # Alps2Alps -- full list
    "AG": ["pm", "bd", "fa", "ops", "mkt", "hr"],  # Holding -- mirrors AA
    "RR": ["bd", "fa", "ops", "mkt"],
    "BK": ["bd", "fa", "ops", "mkt"],
    "SR": ["bd", "fa", "ops", "mkt"],
    "MT": ["bd", "fa", "ops"],
    "AH": ["bd", "fa", "ops"],
    "PK": ["fa", "ops"],
    "CF": ["fa", "ops"],
    "AL": ["bd", "fa", "ops", "mkt"],
}


def get_bt4you_data_path() -> str:
    """Return absolute path to BT4YOU/data directory."""
    base = os.environ.get("BT4YOU_PATH", _DEFAULT_BT4YOU_PATH)
    return os.path.join(base, "data")


def _read_json(path: str) -> Optional[Any]:
    """Read a JSON file; return None on any failure (logs warning)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("BT4YOU read failed for %s: %s", path, exc)
        return None


def load_departments() -> List[Dict[str, Any]]:
    """Return department spec from BT4YOU brand.json.

    Each department has: id, name, color (hex), icon.
    Returns hardcoded fallback if BT4YOU is unreachable.
    """
    data = _read_json(os.path.join(get_bt4you_data_path(), "brand.json"))
    if data and "departments" in data:
        out: List[Dict[str, Any]] = []
        for dept_id, info in data["departments"].items():
            out.append({
                "id": dept_id,
                "name": info.get("name", dept_id),
                "color": info.get("from", "#64748b"),
                "icon": info.get("icon", ""),
            })
        return out

    # Fallback when BT4YOU unreachable
    return [
        {"id": "pm",  "name": "Product",              "color": "#7c3aed", "icon": "\U0001F6E0"},
        {"id": "bd",  "name": "Business Development", "color": "#0ea5e9", "icon": "\U0001F91D"},
        {"id": "fa",  "name": "Finance & Accounting", "color": "#10b981", "icon": "\U0001F4BC"},
        {"id": "ops", "name": "Operations",           "color": "#f59e0b", "icon": "\U0001F680"},
        {"id": "mkt", "name": "Marketing",            "color": "#ec4899", "icon": "\U0001F4E3"},
        {"id": "hr",  "name": "HR",                   "color": "#14b8a6", "icon": "\U0001F465"},
    ]


def load_profit_center_departments() -> Dict[str, List[str]]:
    """Return profit-center -> [department_id, ...] mapping.

    Used by the UI to show only relevant departments per stream.
    """
    return dict(_PC_TO_DEPARTMENTS)


def load_people() -> List[Dict[str, Any]]:
    """Return the full BT4YOU people roster from holding_config.nodes.

    Each person has: id, name, title, asana_gid, profit_center (best-guess
    from node id prefix). Filters out vacant roles.
    """
    data = _read_json(os.path.join(get_bt4you_data_path(), "holding_config.json"))
    if not data or "nodes" not in data:
        logger.info("BT4YOU holding_config not found -- returning empty roster")
        return []

    # Map node id prefix to profit center
    prefix_to_pc = {
        "hold_": "AG", "a2a_": "AA", "alps_": "AA",
        "rr_": "RR", "rock_": "RR",
        "bk_": "BK", "ski_": "BK",
        "sr_": "SR", "skipasser_": "SR",
        "mt_": "MT", "mountly_": "MT", "ah_": "AH",
        "pk_": "PK", "cf_": "CF", "mypeak_": "PK",
        "al_": "AL", "alveda_": "AL",
    }

    seen_names = set()
    people: List[Dict[str, Any]] = []
    for node in data["nodes"]:
        if not isinstance(node, dict):
            continue
        name = (node.get("person_name") or "").strip()
        if not name or name.upper() == "VACANT":
            continue
        if name in seen_names:
            continue
        seen_names.add(name)

        node_id = node.get("id", "")
        pc = "AG"
        for prefix, mapped_pc in prefix_to_pc.items():
            if node_id.startswith(prefix):
                pc = mapped_pc
                break

        people.append({
            "id": node_id,
            "name": name,
            "title": node.get("title", ""),
            "asana_gid": node.get("asana_gid", ""),
            "profit_center": pc,
        })

    people.sort(key=lambda p: p["name"])
    logger.info("Loaded %d people from BT4YOU holding_config", len(people))
    return people
