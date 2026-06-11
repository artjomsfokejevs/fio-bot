"""Direct Asana sync (bypass BT4YOU snapshot).

BT4YOU's holding_config.json packs multiple humans into single rows
(e.g. 'Rita Petukhova, Olga Guk, Dmitriy' for the Accounting Team node).
Even with the _split_group heuristic, we get truncated last names and
guessed profit-center assignments. This module hits Asana's API directly
and writes a clean roster into data/asana_users.json so /api/people can
return it instead.

Usage:
  set Fly secret:  fly secrets set ASANA_PAT=<personal-access-token>
  optional:        fly secrets set ASANA_WORKSPACE_ID=<workspace-gid>
  trigger refresh: POST /api/people/refresh-from-asana   (admin only)

Asana API docs: https://developers.asana.com/reference/getusers
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

__all__ = ["fetch_users_from_asana", "load_asana_users", "asana_users_path"]

_ASANA_BASE = "https://app.asana.com/api/1.0"
_TIMEOUT = 12.0


def asana_users_path() -> str:
    return os.path.join(os.path.dirname(config.DB_PATH), "asana_users.json")


def _asana_get(path: str, token: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """One GET request to Asana with the PAT in the Authorization header."""
    url = _ASANA_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "FIO/1.0 (Amitours Holding)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError("Asana HTTP %d: %s" % (exc.code, body)) from exc
    return json.loads(raw)


# 2026-06-11 — G55 / I62 compliance. Asana's /users endpoint normally
# returns one record per gid (individual humans), so the group-entry
# anti-pattern that bit BT4YOU snapshots (e.g. "Rita Petukhova, Olga Guk,
# Dmitriy" packed in one name string) is unlikely to occur here. But we
# add the unpack-not-filter helper defensively — if a tenant ever stores
# shared-mailbox or distribution-list users in Asana with a packed name,
# we surface every human instead of dropping the row.
_GROUP_SEPARATORS = re.compile(r"\s*(?:,|;|/|…|&|\sand\s)\s*")


def _is_group_entry(name: str) -> bool:
    """True if `name` looks like several humans crammed in one cell.

    Heuristics: ≥ 2 commas/semicolons/'&'/' and ', OR the word "Team"
    near the end, OR an ellipsis. False on single-comma names like
    "Smith, John" (treat as one human).
    """
    if not name:
        return False
    lname = name.lower()
    if "team" in lname.split()[-2:] if lname.split() else []:
        return True
    if "…" in name:
        return True
    seps = len(_GROUP_SEPARATORS.findall(name))
    return seps >= 2


def _split_group_entry(name: str) -> List[str]:
    """Unpack 'A, B, C' / 'A; B; C' / 'A & B' into individual names.

    Always returns a list with at least one element. Strips whitespace
    and drops empty fragments. If `name` doesn't look like a group,
    returns `[name]` unchanged.
    """
    if not _is_group_entry(name):
        return [name] if name else []
    parts = [p.strip() for p in _GROUP_SEPARATORS.split(name)]
    return [p for p in parts if p]


def fetch_users_from_asana(
    token: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Pull all users from Asana, persist them, return a summary.

    If workspace_id is provided we hit /workspaces/<gid>/users; otherwise
    we list all workspaces the token can see and union their users.

    Saved to data/asana_users.json as:
      {
        "fetched_at": "...",
        "source": "asana",
        "users": [
          {"gid": "...", "name": "...", "email": "...", "workspace": "..."},
          …
        ]
      }
    """
    workspaces: List[Dict[str, Any]] = []
    if workspace_id:
        workspaces = [{"gid": workspace_id, "name": workspace_id}]
    else:
        ws_resp = _asana_get("/workspaces", token,
                             params={"limit": "100", "opt_fields": "name"})
        workspaces = ws_resp.get("data") or []

    users_by_gid: Dict[str, Dict[str, Any]] = {}
    for ws in workspaces:
        ws_gid = ws.get("gid")
        if not ws_gid:
            continue
        # Paginate users
        offset: Optional[str] = None
        for _ in range(20):  # safety cap: 20 pages × 100 = 2000 users
            params = {"limit": "100",
                      "workspace": ws_gid,
                      "opt_fields": "name,email,photo,resource_type"}
            if offset:
                params["offset"] = offset
            page = _asana_get("/users", token, params=params)
            for u in page.get("data") or []:
                gid = u.get("gid")
                if not gid:
                    continue
                existing = users_by_gid.get(gid, {})
                users_by_gid[gid] = {
                    "gid": gid,
                    "name": u.get("name") or existing.get("name") or "",
                    "email": u.get("email") or existing.get("email"),
                    "workspace": ws.get("name") or ws_gid,
                    "photo": (u.get("photo") or {}).get("image_60x60"),
                }
            offset = (page.get("next_page") or {}).get("offset")
            if not offset:
                break

    users = sorted(users_by_gid.values(), key=lambda u: (u.get("name") or "").lower())
    payload = {
        "fetched_at": datetime.utcnow().isoformat(),
        "source": "asana",
        "workspaces": [{"gid": w.get("gid"), "name": w.get("name")} for w in workspaces],
        "count": len(users),
        "users": users,
    }

    path = asana_users_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

    logger.info("Asana sync: pulled %d users across %d workspace(s)",
                len(users), len(workspaces))
    return payload


def load_asana_users() -> Optional[Dict[str, Any]]:
    """Return the cached Asana roster, or None if not yet synced."""
    path = asana_users_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
