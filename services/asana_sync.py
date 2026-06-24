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


def _asana_post(path: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST to Asana with PAT auth. 2026-06-11 — added for chase-task auto-create."""
    url = _ASANA_BASE + path
    body = json.dumps({"data": payload}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "FIO/1.0 (Amitours Holding)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError("Asana HTTP %d: %s" % (exc.code, body_txt)) from exc
    return json.loads(raw)


def create_task(
    name: str,
    notes: str,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    assignee_gid: Optional[str] = None,
    due_on: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a single task in Asana. Returns {gid, permalink_url, ...}.

    Requires either workspace_id (creates a "My Tasks" task) OR project_id.
    `due_on` is a YYYY-MM-DD date string (Asana 'due_on' field).
    Raises RuntimeError on Asana error so the caller can surface to UI.

    Added 2026-06-11 for the month-close chase-task auto-create flow.
    2026-06-23 — extended for the bulk-chase flow: `due_on` parameter.
    """
    token = _resolve_token()
    if not (workspace_id or project_id):
        raise RuntimeError("either workspace_id or project_id is required")

    payload: Dict[str, Any] = {"name": name, "notes": notes}
    if workspace_id:
        payload["workspace"] = workspace_id
    if project_id:
        payload["projects"] = [project_id]
    if assignee_gid:
        payload["assignee"] = assignee_gid
    if due_on:
        payload["due_on"] = due_on

    resp = _asana_post("/tasks", token, payload)
    return resp.get("data", {})


def _resolve_token() -> str:
    """Single token-lookup helper used by every Asana caller."""
    token = (config.ASANA_PAT or "").strip() if hasattr(config, "ASANA_PAT") else ""
    if not token:
        token = (os.environ.get("ASANA_PAT") or "").strip()
    if not token:
        raise RuntimeError("ASANA_PAT not configured")
    return token


# 2026-06-23 — bake BT4YOU's default workspace ID so users never see a
# prompt(). The Holding has one Asana workspace; override only if needed
# via Fly secret ASANA_WORKSPACE_ID.
_DEFAULT_WORKSPACE_ID = "145603020643986"   # Amitours Holding workspace (BT4YOU canonical)


def upload_attachment(task_gid: str, file_path: str,
                      name: Optional[str] = None) -> Dict[str, Any]:
    """Attach a local file to an existing Asana task via /tasks/<gid>/attachments.

    Asana's attachment endpoint requires multipart/form-data; urllib's stdlib
    multipart is awkward so we hand-roll the body (small files, stdlib only).
    Returns the {gid, name, ...} attachment record, or raises RuntimeError.

    Added 2026-06-24 — for chase tasks to ship the original bank statement /
    matched invoice with the chase, so the stakeholder has the artefact in hand.
    """
    import io
    import mimetypes
    import uuid
    token = _resolve_token()
    if not os.path.isfile(file_path):
        raise RuntimeError("attachment file not found: " + file_path)
    base = name or os.path.basename(file_path)
    ctype, _ = mimetypes.guess_type(file_path)
    ctype = ctype or "application/octet-stream"
    boundary = "----FIO" + uuid.uuid4().hex
    with open(file_path, "rb") as fh:
        file_bytes = fh.read()
    body = io.BytesIO()
    crlf = b"\r\n"
    body.write(("--" + boundary + "\r\n").encode())
    body.write(('Content-Disposition: form-data; name="file"; filename="' +
                base + '"\r\n').encode())
    body.write(("Content-Type: " + ctype + "\r\n\r\n").encode())
    body.write(file_bytes)
    body.write(crlf)
    body.write(("--" + boundary + "--\r\n").encode())
    payload = body.getvalue()
    req = urllib.request.Request(
        _ASANA_BASE + "/tasks/" + task_gid + "/attachments",
        data=payload, method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
            "User-Agent": "FIO/1.0 (Amitours Holding)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError("Asana attachment HTTP %d: %s" % (exc.code, body_txt)) from exc
    return (json.loads(raw) or {}).get("data", {})


def resolve_workspace_id() -> str:
    """Resolve the workspace ID for the holding.

    Lookup order:
      1. config.ASANA_WORKSPACE_ID  (Fly secret if explicitly set)
      2. os.environ['ASANA_WORKSPACE_ID']
      3. baked-in default (`_DEFAULT_WORKSPACE_ID`) — Amitours Holding's
         workspace shared with BT4YOU Executive Bot.
    Always returns a non-empty string — never raises.
    """
    ws = (getattr(config, "ASANA_WORKSPACE_ID", "") or "").strip()
    if not ws:
        ws = (os.environ.get("ASANA_WORKSPACE_ID") or "").strip()
    return ws or _DEFAULT_WORKSPACE_ID


# ── 2026-06-23 — listing helpers for the rich chase-task creator UI ───────

def list_projects(workspace_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return projects in a workspace as [{gid, name, team}, ...].

    Paginated up to 10 pages × `limit` (default 1000 projects).
    """
    token = _resolve_token()
    if not workspace_id:
        raise RuntimeError("workspace_id required")
    out: List[Dict[str, Any]] = []
    offset: Optional[str] = None
    for _ in range(10):
        params = {
            "limit": str(limit),
            "workspace": workspace_id,
            "opt_fields": "name,team.name,archived",
        }
        if offset:
            params["offset"] = offset
        page = _asana_get("/projects", token, params=params)
        for p in page.get("data") or []:
            if p.get("archived"):
                continue
            out.append({
                "gid": p.get("gid"),
                "name": p.get("name") or "",
                "team": (p.get("team") or {}).get("name") or "",
            })
        offset = (page.get("next_page") or {}).get("offset")
        if not offset:
            break
    out.sort(key=lambda r: (r.get("team") or "", r.get("name") or ""))
    return out


def list_users_for_workspace(workspace_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return assignable users for the workspace as [{gid, name, email}, ...]."""
    token = _resolve_token()
    if not workspace_id:
        raise RuntimeError("workspace_id required")
    out: List[Dict[str, Any]] = []
    offset: Optional[str] = None
    for _ in range(20):  # cap: 20 × 100 = 2000 users
        params = {
            "limit": str(limit),
            "workspace": workspace_id,
            "opt_fields": "name,email,photo",
        }
        if offset:
            params["offset"] = offset
        page = _asana_get("/users", token, params=params)
        for u in page.get("data") or []:
            if not u.get("gid"):
                continue
            out.append({
                "gid": u["gid"],
                "name": u.get("name") or "",
                "email": u.get("email") or None,
                "photo": (u.get("photo") or {}).get("image_60x60"),
            })
        offset = (page.get("next_page") or {}).get("offset")
        if not offset:
            break
    out.sort(key=lambda r: (r.get("name") or "").lower())
    return out


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
