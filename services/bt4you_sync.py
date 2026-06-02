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
    # Phase 3 — Governance integration
    "build_people_map",
    "load_governance_opex",
    "suggest_pc_for_uploader",
    "suggest_pc_for_vendor",
    "load_business_streams",
    "build_governance_index",
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


def _is_group_entry(name: str) -> bool:
    """Detect a holding_config 'person_name' that actually packs multiple
    humans into one row (BD Managers, BD Team, etc.).

    Heuristics:
      - contains ',' (comma-separated names)
      - contains '...' or '…' (truncation indicating more names)
      - contains ' & ' (English 'and')
      - contains ' и ' or ' und ' (other languages)
    """
    if not name:
        return False
    lower = name.lower()
    if "," in name:
        return True
    if "..." in name or "…" in name:
        return True
    if " & " in name or " and " in lower:
        return True
    if " и " in (" " + lower + " ") or " und " in lower:
        return True
    return False


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
    # We iterate twice: once to harvest individual identities packed inside
    # group entries (so e.g. 'Rita Petukhova, Olga Guk, Dmitriy' surfaces 3
    # separate people sharing the same node_id + title), then once for normal
    # single-name nodes. The unpacked entries are tagged from_group=True so
    # the UI can show a small hint and the admin can dismiss any mis-parse.
    raw_nodes = []
    for node in data["nodes"]:
        if not isinstance(node, dict):
            continue
        name = (node.get("person_name") or "").strip()
        if not name or name.upper() == "VACANT":
            continue
        raw_nodes.append((node, name))

    for node, name in raw_nodes:
        node_id = node.get("id", "")
        title = node.get("title", "")
        asana_gid = node.get("asana_gid", "")
        pc = "AG"
        for prefix, mapped_pc in prefix_to_pc.items():
            if node_id.startswith(prefix):
                pc = mapped_pc
                break

        # Group entry: split into separate individuals
        if _is_group_entry(name):
            parts = _split_group(name)
            for idx, part in enumerate(parts):
                if not part or part in seen_names:
                    continue
                seen_names.add(part)
                people.append({
                    "id": f"{node_id}::g{idx}",
                    "name": part,
                    "title": title,
                    "asana_gid": asana_gid if idx == 0 else "",
                    "profit_center": pc,
                    "from_group": True,
                })
            continue

        if name in seen_names:
            continue
        seen_names.add(name)
        people.append({
            "id": node_id,
            "name": name,
            "title": title,
            "asana_gid": asana_gid,
            "profit_center": pc,
            "from_group": False,
        })

    people.sort(key=lambda p: p["name"])
    logger.info("Loaded %d people from BT4YOU holding_config "
                "(%d unpacked from group entries)",
                len(people), sum(1 for p in people if p.get("from_group")))
    return people


def _split_group(name: str) -> List[str]:
    """Split a packed-group person_name into individual humans.

    Handles: 'Rita Petukhova, Olga Guk, Dmitriy' → ['Rita Petukhova', 'Olga Guk', 'Dmitriy']
    Strips trailing '...' / '…' and ' & ' / ' and ' / ' и ' / ' und '.
    Drops fragments shorter than 2 chars or that look like an ellipsis-only entry.
    """
    raw = (name or "").replace("…", ",").replace("...", ",")
    for sep in (" & ", " and ", " и ", " und "):
        raw = raw.replace(sep, ",")
    parts = []
    seen = set()
    for chunk in raw.split(","):
        cleaned = chunk.strip(" \t.,;")
        if not cleaned or len(cleaned) < 2:
            continue
        # Skip obvious non-name tokens
        low = cleaned.lower()
        if low in {"etc", "etc.", "team", "and others", "others"}:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        parts.append(cleaned)
    return parts


# ═══════════════════════════════════════════════════════════════
# Phase 3 — Governance integration (auto profit-center / policy)
# ═══════════════════════════════════════════════════════════════

# Node-id prefix → (stream key, profit center code). Stream comes from
# BT4YOU's business model template; PC is the 2-letter accounting code
# used by FIO ledger + classifier.
_PREFIX_TO_STREAM_PC = {
    # Holding tier
    "hold_":      ("holding",       "AG"),
    "amp_":       ("holding",       "AG"),   # AMP = Amitours Marketing Platform (group-level)
    # Alps2Alps (main operational stream)
    "a2a_":       ("alps2alps",     "AA"),
    "alps_":      ("alps2alps",     "AA"),
    # Venture Studio brands
    "vs_skibookers": ("skibookers", "BK"),
    "vs_skipasser":  ("skipasser",  "SR"),
    "vs_mountly":    ("mountly",    "AH"),
    "vs_mypeak":     ("mypeak",     "CF"),
    "vs_alveda":     ("alveda",     "AL"),
    "vs_root":       ("venture_studio", "AG"),  # VS overhead
    "vs_":           ("venture_studio", "AG"),  # fallback for any vs_ node
    # Rock2Rock / others (when nodes ever appear)
    "rr_":        ("rock2rock",     "RR"),
    "rock_":      ("rock2rock",     "RR"),
}


def _resolve_stream(node_id: str) -> Dict[str, Any]:
    """Map a holding-tree node ID to its stream + profit center code."""
    if not node_id:
        return {"stream": None, "profit_center": None, "stream_confidence": 0}
    # Try longest prefix first (vs_skibookers before vs_)
    for prefix in sorted(_PREFIX_TO_STREAM_PC.keys(), key=len, reverse=True):
        if node_id.startswith(prefix):
            stream, pc = _PREFIX_TO_STREAM_PC[prefix]
            return {
                "stream": stream,
                "profit_center": pc,
                "stream_confidence": 95 if len(prefix) > 4 else 80,
            }
    return {"stream": None, "profit_center": "AG", "stream_confidence": 30}


def _split_people_names(raw: str) -> List[str]:
    """A node's person_name may be 'Edvīns Pribils, Rihards Feierabends' — split."""
    if not raw:
        return []
    raw = raw.replace(" / ", ", ").replace(" & ", ", ").replace(" and ", ", ")
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p and p.upper() not in ("VACANT", "—", "-", "TBD")]


def _walk_parent_chain(node: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> List[str]:
    """Return the chain of node IDs from this node up to root."""
    chain: List[str] = []
    cur = node
    seen = set()
    while cur and cur.get("id") and cur["id"] not in seen:
        seen.add(cur["id"])
        chain.append(cur["id"])
        parent_id = cur.get("parent")
        cur = by_id.get(parent_id) if parent_id else None
    return chain


# Department inference from title keywords — used when explicit dept tagging absent
_DEPT_KEYWORDS = (
    ("hr",  ("HR", "people", "talent", "recruiting", "L&D")),
    ("fa",  ("CFO", "finance", "accounting", "bookkeep", "controller", "treasury")),
    ("mkt", ("CMO", "marketing", "growth", "brand", "content", "PR ")),
    ("bd",  ("CCO", "BD", "business development", "sales", "enterprise", "upsales")),
    ("ops", ("COO", "operations", "dispatch", "planning", "logistics", "customer service")),
    ("pm",  ("CPO", "product", "design", "UX", "UI", "research", "data analy")),
    ("it",  ("CTO", "tech", "engineer", "developer", "devops")),
    ("legal", ("legal", "compliance", "GDPR", "M&A")),
    ("ceo", ("CEO", "MD")),
    ("admin", ("admin", "executive coord", "PA")),
)


def _infer_department(title: str) -> Optional[str]:
    if not title:
        return None
    t = title.lower()
    for dept, keywords in _DEPT_KEYWORDS:
        for kw in keywords:
            if kw.lower() in t:
                return dept
    return None


def build_people_map() -> Dict[str, Dict[str, Any]]:
    """Return a flat map: normalized_name → {profit_center, stream, role, dept, asana_gid, node_id, parent_chain}.

    Multi-person nodes are unpacked (1 person → 1 entry). Names normalised
    lowercase-no-spaces so vendor-side matching is forgiving.
    """
    data = _read_json(os.path.join(get_bt4you_data_path(), "holding_config.json"))
    if not data or "nodes" not in data:
        return {}
    nodes = [n for n in data["nodes"] if isinstance(n, dict)]
    by_id = {n.get("id"): n for n in nodes if n.get("id")}

    out: Dict[str, Dict[str, Any]] = {}
    for n in nodes:
        for name in _split_people_names(n.get("person_name") or ""):
            stream_info = _resolve_stream(n.get("id", ""))
            chain = _walk_parent_chain(n, by_id)
            entry = {
                "name":          name,
                "node_id":       n.get("id"),
                "title":         n.get("title", ""),
                "asana_gid":     n.get("asana_gid", ""),
                "department":    _infer_department(n.get("title", "")),
                "parent_chain":  chain,
                **stream_info,
            }
            # First occurrence wins (top-level role preferred over secondary)
            norm = _norm_key(name)
            if norm not in out:
                out[norm] = entry
            else:
                # If new entry is in a "higher" stream confidence, update
                if entry["stream_confidence"] > out[norm].get("stream_confidence", 0):
                    out[norm] = entry
    logger.info("build_people_map: %d people mapped to streams", len(out))
    return out


def _norm_key(name: str) -> str:
    import re
    return re.sub(r"[^a-z]", "", (name or "").lower())


def load_business_streams() -> List[Dict[str, Any]]:
    """Read business_model_template.json — list of 6+ streams with name/PC/owner."""
    data = _read_json(os.path.join(get_bt4you_data_path(), "business_model_template.json"))
    if not data or "streams" not in data:
        return []
    return data["streams"]


def load_governance_opex() -> List[Dict[str, Any]]:
    """Read holding_governance_opex.json — N categories with owner + budget hints.

    Format per category:
      {id, name, owner, monthly_eur, annual_eur, notes}
    Used by FIO to:
      - cross-check whether a recurring expense fits a budgeted category
      - flag invoices where amount > 30% of monthly category budget
      - suggest owner as approver
    """
    data = _read_json(os.path.join(get_bt4you_data_path(), "holding_governance_opex.json"))
    if not data:
        return []
    return data.get("categories", []) if isinstance(data, dict) else []


def build_governance_index() -> Dict[str, Any]:
    """Build a fast-lookup index of governance categories by keyword + owner."""
    cats = load_governance_opex()
    by_keyword: Dict[str, List[Dict[str, Any]]] = {}
    by_owner: Dict[str, List[Dict[str, Any]]] = {}
    for c in cats:
        # Index by name keywords (split into 2+ char tokens)
        name = (c.get("name") or "").lower()
        notes = (c.get("notes") or "").lower()
        tokens = set()
        for src in (name, notes):
            for tok in src.replace("(", " ").replace(")", " ").replace(",", " ").split():
                if len(tok) >= 4:
                    tokens.add(tok)
        for tok in tokens:
            by_keyword.setdefault(tok, []).append(c)
        owner = (c.get("owner") or "").strip()
        if owner and not owner.startswith("("):
            by_owner.setdefault(_norm_key(owner), []).append(c)
    return {
        "categories": cats,
        "by_keyword": by_keyword,
        "by_owner":   by_owner,
        "category_count": len(cats),
    }


# Vendor → stream cache (cleared on demand). FIO's classifier asks this for
# each uploaded invoice before deciding which PC to suggest.
_VENDOR_STREAM_HINTS = (
    # Direct brand names — strongest signal
    ("alps2alps",   "AA", 95),
    ("alps 2 alps", "AA", 95),
    ("rock2rock",   "RR", 95),
    ("skibookers",  "BK", 95),
    ("skibooker",   "BK", 90),
    ("skipasser",   "SR", 95),
    ("shreddo",     "SR", 90),
    ("mountly",     "AH", 95),
    ("alveda",      "AL", 95),
    ("mypeak",      "CF", 95),
    ("my peak",     "CF", 90),
    ("amitours",    "AG", 80),
    ("alexcursion", "AL", 85),
    ("global transfer", "GT", 95),
    ("dms",         "DS", 70),
    ("sinkopa",     "SN", 90),
    ("french cars", "FC", 95),
    ("eu transfer", "ET", 90),
    ("loyalty club","LC", 85),
)


def suggest_pc_for_vendor(vendor_name: Optional[str], vendor_address: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Map vendor's company name (and optional address) to a profit center.

    Returns {profit_center, confidence, reason} or None.
    """
    if not vendor_name:
        return None
    blob = (vendor_name + " " + (vendor_address or "")).lower()
    for needle, pc, conf in _VENDOR_STREAM_HINTS:
        if needle in blob:
            return {
                "profit_center": pc,
                "confidence": conf,
                "reason": f"Vendor name contains '{needle}' → {pc}",
                "source": "vendor_name",
            }
    return None


def suggest_pc_for_uploader(uploader_name: Optional[str], people_map: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Look up uploader in the BT4YOU people-map and return their stream's PC.

    Returns {profit_center, confidence, stream, department, reason} or None.
    """
    if not uploader_name:
        return None
    pm = people_map if people_map is not None else build_people_map()
    norm = _norm_key(uploader_name)
    entry = pm.get(norm)
    # Fallback: contains-match (e.g. uploader = "Mikhail" matches "Mikhail Iumashev")
    if not entry:
        for k, v in pm.items():
            if norm and (norm in k or k in norm):
                entry = v
                break
    if not entry:
        return None
    return {
        "profit_center": entry.get("profit_center"),
        "confidence":    entry.get("stream_confidence", 70),
        "stream":        entry.get("stream"),
        "department":    entry.get("department"),
        "node_id":       entry.get("node_id"),
        "title":         entry.get("title"),
        "reason":        f"Uploader '{entry.get('name')}' belongs to stream '{entry.get('stream')}' ({entry.get('node_id')})",
        "source":        "uploader",
    }
