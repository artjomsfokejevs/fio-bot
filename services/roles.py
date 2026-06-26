"""Role-based access control on top of HTTP-Basic shared password.

Two-layer model:
  1. Outer perimeter: HTTP Basic with a shared FIO_USER/FIO_PASS (set on Fly).
     Anyone in the company knows this. Already enforced in app.py:_demo_gate.
  2. Inner identity: each browser picks "who am I" from the BT4YOU people
     roster on first load (stored in localStorage as fio_signed_in_as).
     The browser sends this back as the X-FIO-User header on every API call.
     This module looks up the user's role and gates each endpoint / tab.

Roles (2026-06-16 — refreshed for Phase 1–3 capabilities):

  ── admin (Artjoms · technical owner) ────────────────────────────────────
     Sees: ALL tabs (Upload · Approve · Accounting · Bank Statement Audit ·
           Analytics · Confirm for Payment · Admin · FIO Legend · Policies & Limits)
     Can:  manage FIO users + paying accounts (CRUD + INLINE EDIT) ·
           manage policy rules (add / edit threshold / deactivate) ·
           set stream budgets · acknowledge X-alarms · run bank-statement
           archive re-check · send Slack test ping · delete partial payments ·
           edit chase task template.
     Receives: in-app bell for ALL urgent events; X-alarm emails via
           XALARM_OPS_EMAIL Fly secret.

  ── holding_ceo (Raitis) ─────────────────────────────────────────────────
     Sees: Upload · Approve · Accounting · Analytics · Confirm for Payment ·
           FIO Legend · Policies & Limits   (NO Card Audit, NO Admin)
     Can:  approve a week's payments (✓ Confirm for Payment) which triggers
           Rita's bookkeeper-bell + auto-fires X-alarm IF stream is over
           budget · acknowledge X-alarms · edit policy rule thresholds ·
           set stream budgets · approve policy violations as Accounting.
     Receives: Slack DM via BT4YOU Bot for every "Send to CEO" urgent
           payment + every X-alarm; email via XALARM_CEO_EMAIL Fly secret.

  ── bookkeeper (Rita) ────────────────────────────────────────────────────
     Sees: Upload · Approve · Accounting · Bank Statement Audit · Analytics ·
           Confirm for Payment · FIO Legend · Policies & Limits  (NO Admin)
     Can:  execute payments (mark-paid + paying account picker) · budget-validate
           docs · run bank-statement reconciliation + chase tasks · approve
           policy violations as Accounting · send urgent-payment Slack to CEO ·
           add partial payments on internal invoices · toggle is_internal /
           is_salary flags on docs · acknowledge X-alarms · re-check bank
           statement archives.
     Receives: in-app bell when CEO confirms ANY payment; X-alarm emails
           (looked up from fio_users WHERE role=bookkeeper).

  ── stream_owner (Serge / Katia / Rihards / Evgeny) ──────────────────────
     Sees: Upload · Approve · Accounting · Bank Statement Audit · Analytics ·
           FIO Legend   (frontend constrains data by their profit_center)
     Can:  approve docs for their own stream · view their stream's spend.
     Receives: X-alarm email when their stream goes over budget + Asana
           auto-task on the same trigger.

  ── viewer (anyone unmapped) ─────────────────────────────────────────────
     Sees: Upload · Approve · FIO Legend
     Can:  upload invoices · view own uploads.

Stored as JSON on the persistent volume so it survives redeploys.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

__all__ = [
    "ROLE_ADMIN",
    "ROLE_HOLDING_CEO",
    "ROLE_BOOKKEEPER",
    "ROLE_STREAM_OWNER",
    "ROLE_VIEWER",
    "ALL_ROLES",
    "TAB_ACCESS",
    "load_roles",
    "save_roles",
    "get_role",
    "set_role",
    "list_users_with_roles",
    "tabs_for_role",
    "role_can_tab",
    "user_can_tab",
    "seed_if_missing",
]

ROLE_ADMIN = "admin"
ROLE_HOLDING_CEO = "holding_ceo"
ROLE_BOOKKEEPER = "bookkeeper"
ROLE_STREAM_OWNER = "stream_owner"
ROLE_VIEWER = "viewer"

ALL_ROLES = [
    ROLE_ADMIN,
    ROLE_HOLDING_CEO,
    ROLE_BOOKKEEPER,
    ROLE_STREAM_OWNER,
    ROLE_VIEWER,
]

# Tab IDs match the data-tab attributes in static/index.html
# 2026-06-16 — added "policies" (Phase 1 P1.2 Internal Policies & Limits tab).
ALL_TABS = [
    "upload", "approve", "accounting", "card-audit",
    "analytics", "confirm-payment", "admin", "legend", "policies",
    "revenue",
]

# Role -> list of tab IDs that role can see.
# Updated 2026-06-02 per user spec:
#   - admin            : everything (manage users)
#   - holding_ceo      : approves weekly payments + sees all approve/accounting/analytics + Confirm + Legend
#                         (NO Card Audit — that's the bookkeeper's domain)
#   - bookkeeper       : executes payments + sees Confirm + Card Audit + all approve/accounting/analytics + Legend
#   - stream_owner     : sees their own stream only on approve/accounting/card-audit/analytics + Legend
#                         (frontend constrains by signed-in user's profit_center)
#   - viewer (default) : Upload + Approve + Legend
# 2026-06-16 — "policies" tab added to admin/holding_ceo/bookkeeper
# (CEO can change limits; bookkeeper sees + flags violations).
TAB_ACCESS: Dict[str, List[str]] = {
    ROLE_ADMIN:        ALL_TABS,  # everything
    ROLE_HOLDING_CEO:  ["upload", "approve", "accounting", "analytics",
                        "confirm-payment", "legend", "policies", "revenue"],
    ROLE_BOOKKEEPER:   ["upload", "approve", "accounting", "card-audit",
                        "analytics", "confirm-payment", "legend", "policies",
                        "revenue"],
    ROLE_STREAM_OWNER: ["upload", "approve", "accounting", "card-audit",
                        "analytics", "legend", "revenue"],
    ROLE_VIEWER:       ["upload", "approve", "legend"],
}

# Initial bootstrap: who's admin on day one.
_BOOTSTRAP = {
    "Artjoms Fokejevs": {"role": ROLE_ADMIN,      "profit_center": "AA",
                         "title": "CEO Alps2Alps · seeded admin"},
    "Rita":             {"role": ROLE_BOOKKEEPER, "profit_center": "AG",
                         "title": "Bookkeeper · seeded admin (gets admin tab too)"},
    "Raitis Bullits":   {"role": ROLE_HOLDING_CEO, "profit_center": "AG",
                         "title": "Holding CEO · approves weekly payments"},
}

# Rita is bookkeeper but ALSO admin per the spec ("админка, имеет право
# на запуск Артём Фокеев и Рита"). We model this by ALSO putting her in
# admin role -- only one role per user in this MVP, so we map Rita to admin
# (she keeps bookkeeper-flavoured Confirm-for-Payment access via admin's
# 'see-everything' grant).
_BOOTSTRAP["Rita"]["role"] = ROLE_ADMIN
_BOOTSTRAP["Rita"]["title"] = "Bookkeeper + Admin · seeded"


def _roles_path() -> str:
    return os.path.join(os.path.dirname(config.DB_PATH), "user_roles.json")


def _seed_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "seed", "user_roles.json")


def seed_if_missing() -> None:
    """Copy or write the initial roles file if data/user_roles.json doesn't
    exist on the volume."""
    target = _roles_path()
    if os.path.exists(target):
        return
    seed = _seed_path()
    if os.path.exists(seed):
        try:
            with open(seed, "r", encoding="utf-8") as src:
                payload = src.read()
            with open(target, "w", encoding="utf-8") as dst:
                dst.write(payload)
            logger.info("Seeded user_roles.json from image seed/")
            return
        except OSError as exc:
            logger.warning("Roles seed copy failed: %s", exc)
    # Fall back to in-code bootstrap
    save_roles(_BOOTSTRAP)
    logger.info("Initialised user_roles.json from in-code _BOOTSTRAP (%d users)",
                len(_BOOTSTRAP))


def load_roles() -> Dict[str, Dict[str, Any]]:
    """Return {user_name: {role, profit_center, title, ...}, ...}.
    Empty dict if file missing/corrupt."""
    path = _roles_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_roles(roles: Dict[str, Dict[str, Any]]) -> None:
    """Persist the roles dict to disk atomically."""
    path = _roles_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(roles, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_role(user_name: Optional[str]) -> str:
    """Return the role for a user, or ROLE_VIEWER if unknown / missing."""
    if not user_name:
        return ROLE_VIEWER
    roles = load_roles()
    entry = roles.get(user_name) or roles.get(user_name.strip())
    if not entry:
        return ROLE_VIEWER
    return entry.get("role") or ROLE_VIEWER


def set_role(user_name: str, role: str,
             profit_center: Optional[str] = None,
             title: Optional[str] = None,
             changed_by: Optional[str] = None) -> Dict[str, Any]:
    """Upsert a role assignment. Returns the updated entry."""
    if role not in ALL_ROLES:
        raise ValueError("Unknown role: %s" % role)
    roles = load_roles()
    existing = roles.get(user_name) or {}
    existing["role"] = role
    if profit_center is not None:
        existing["profit_center"] = profit_center
    if title is not None:
        existing["title"] = title
    existing["updated_at"] = datetime.utcnow().isoformat()
    existing["updated_by"] = changed_by or "system"
    roles[user_name] = existing
    save_roles(roles)
    return existing


def list_users_with_roles(people: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge BT4YOU people roster with role assignments.

    Every person from BT4YOU shows up here; unmapped users default to viewer.
    """
    roles = load_roles()
    out: List[Dict[str, Any]] = []
    for p in people:
        name = p.get("name")
        entry = roles.get(name) or {}
        out.append({
            "name": name,
            "title": p.get("title") or entry.get("title") or "",
            "asana_gid": p.get("asana_gid"),
            "profit_center": entry.get("profit_center") or p.get("profit_center"),
            "role": entry.get("role") or ROLE_VIEWER,
            "updated_at": entry.get("updated_at"),
            "updated_by": entry.get("updated_by"),
        })
    return out


def tabs_for_role(role: str) -> List[str]:
    return list(TAB_ACCESS.get(role, TAB_ACCESS[ROLE_VIEWER]))


def role_can_tab(role: str, tab: str) -> bool:
    return tab in TAB_ACCESS.get(role, [])


def user_can_tab(user_name: Optional[str], tab: str) -> bool:
    return role_can_tab(get_role(user_name), tab)


# ─────────────────────────────────────────────────────────────────────
# 2026-06-24 FB-L enforcement — granular sub-permissions + PC scope.
# Lookups read fio_users.permissions (CSV cap codes) and fio_users.pc_scope
# (CSV canonical PC codes). Blank = role default = full.
#
# Default capabilities per role: if a user has NO `permissions` field, they
# inherit ALL capabilities listed below for their role. If they DO have a
# `permissions` string, ONLY those listed capabilities are granted (intersect
# with role defaults).
#
# Capability vocabulary (the canonical set — keep small):
#   approve_budget   — Budget Check stage actions
#   approve_payment  — Holding-CEO Awaiting-CEO stage approval
#   mark_paid        — Bookkeeper Mark-paid stage
#   post_to_pnl      — Approve & Post in Approve tab
#   manage_payees    — Admin → Paying Accounts CRUD
#   manage_users     — Admin → FIO-managed users CRUD
#   view_revenue     — 💵 Revenue tab read
#   create_revenue   — 💵 Revenue Add proforma/invoice / receipts
#   export_bulk      — Accounting bulk ZIP export
# ─────────────────────────────────────────────────────────────────────
ROLE_DEFAULT_CAPS: Dict[str, set] = {
    "admin":         {"approve_budget", "approve_payment", "mark_paid", "post_to_pnl",
                       "manage_payees", "manage_users", "view_revenue",
                       "create_revenue", "export_bulk"},
    "holding_ceo":   {"approve_payment", "post_to_pnl", "view_revenue"},
    "bookkeeper":    {"approve_budget", "mark_paid", "post_to_pnl",
                       "manage_payees", "view_revenue", "create_revenue",
                       "export_bulk"},
    "stream_owner": {"approve_budget", "post_to_pnl", "view_revenue", "create_revenue"},
    "viewer":        set(),
}


def _user_db_row(user_name: Optional[str]) -> Dict[str, Any]:
    """Fetch the fio_users row (or {}) for a name. Cached per-request? No —
    cheap enough at SQLite scale. Safe with stale rows."""
    if not user_name:
        return {}
    try:
        from services import db as _db
    except ImportError:
        return {}
    conn = _db.get_connection()
    try:
        row = conn.execute(
            "SELECT permissions, pc_scope, active FROM fio_users WHERE full_name = ?",
            (user_name,),
        ).fetchone()
        if not row:
            return {}
        return _db._row_to_dict(row)
    except Exception:  # noqa: BLE001 — fio_users may not exist on fresh DB
        return {}
    finally:
        conn.close()


def _parse_csv_list(s: Optional[str]) -> List[str]:
    if not s:
        return []
    return [p.strip() for p in str(s).split(",") if p.strip()]


def user_capabilities(user_name: Optional[str]) -> set:
    """Return the effective capability set for a user.

    Logic: role-defaults ∩ user.permissions (if user has any).
    If user.permissions blank → all role defaults.
    """
    role = get_role(user_name)
    role_caps = ROLE_DEFAULT_CAPS.get(role, set())
    user_caps_raw = _parse_csv_list(_user_db_row(user_name).get("permissions"))
    if not user_caps_raw:
        return set(role_caps)
    return role_caps & set(user_caps_raw)


def has_capability(user_name: Optional[str], cap: str) -> bool:
    """True iff user has this capability via their role + permissions."""
    return cap in user_capabilities(user_name)


def user_pc_scope(user_name: Optional[str]) -> Optional[List[str]]:
    """Return list of canonical PC codes the user is restricted to.

    `None` = unrestricted (use role default). `[]` (empty list) = restricted
    to nothing — locks the user out of all PC-filtered data.
    Used by list endpoints to prefilter results.
    """
    raw = _parse_csv_list(_user_db_row(user_name).get("pc_scope"))
    if not raw:
        return None
    # Translate legacy codes if any
    try:
        from services import pc_codes as _pc
        return [_pc.to_canonical(c) or c for c in raw]
    except ImportError:
        return raw


def pc_in_scope(user_name: Optional[str], pc: Optional[str]) -> bool:
    """True if `pc` is allowed for `user_name` (or no scope set, or PC unknown)."""
    if not pc:
        return True
    scope = user_pc_scope(user_name)
    if scope is None:
        return True
    try:
        from services import pc_codes as _pc
        return (_pc.to_canonical(pc) or pc) in scope
    except ImportError:
        return pc in scope
