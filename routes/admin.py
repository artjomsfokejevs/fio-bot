"""Admin tab + reference-data Blueprint.

Extracted from app.py 2026-06-11 (Phase 7.2 refactor) to consolidate every
endpoint that the Admin section / What's New modal / Mark Paid dropdown
needs. Twelve endpoints, ~225 LOC out of app.py.

Endpoint cluster:
    FIO users CRUD            /api/fio-users (4)
    Paying accounts CRUD      /api/paying-accounts (4)
    App settings KV           /api/settings (2)
    Legal entities reference  /api/legal-entities (1)
    What's New feed           /api/whats-new (1)

All write endpoints require admin/bookkeeper role via _require_role;
the role guard helpers come from app.py to avoid circular imports.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from flask import Blueprint, jsonify, request

from services import db
from services import users as users_svc
from services import paying_accounts as pa_svc
from services import settings as settings_svc
from services import roles as roles_svc

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/api")


# Local copies of the auth helpers — keeps the blueprint self-contained.
def _current_user_name():
    name = (request.headers.get("X-FIO-User") or "").strip()
    return name or None


def _require_role(*allowed_roles: str):
    user = _current_user_name()
    role = roles_svc.get_role(user)
    if role not in allowed_roles:
        return jsonify({
            "error": "forbidden",
            "message": "Role '%s' is not allowed here. Required: %s" %
                       (role, list(allowed_roles)),
            "you": user,
            "your_role": role,
        }), 403
    return None


# 2026-06-26 (D) — capability gate mirroring app._require_capability.
# Kept local to avoid circular import from app.py.
def _require_capability(cap: str):
    user = _current_user_name()
    if roles_svc.has_capability(user, cap):
        return None
    return jsonify({
        "error": "forbidden",
        "message": "You don't have capability '%s'. Ask Admin to grant it." % cap,
        "you": user,
        "your_role": roles_svc.get_role(user),
        "missing_capability": cap,
    }), 403


# ─────────────────────────────────────────────────────────────────────
# FIO users (HR / Bookkeeper-managed roster — drives Upload dropdown)
# ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/fio-users", methods=["GET"])
def fio_users_list() -> Any:
    active_only = (request.args.get("active") or "").lower() in ("true", "1", "yes")
    role = request.args.get("role") or None
    out = users_svc.list_users(active_only=active_only, role=role)
    return jsonify({"users": out, "roles": users_svc.ROLES})


@admin_bp.route("/fio-users", methods=["POST"])
def fio_users_create() -> Any:
    err = _require_capability("manage_users")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    try:
        u = users_svc.create_user(body, created_by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 — final boundary, log + propagate
        logger.exception("create_user failed")
        return jsonify({"error": str(exc)}), 500
    db.insert_audit_log("user:" + str(u["id"]), "fio_user_create",
                        {"full_name": u["full_name"], "role": u["role"]},
                        performed_by=actor)
    return jsonify(u), 201


@admin_bp.route("/fio-users/<int:user_id>", methods=["PATCH"])
def fio_users_update(user_id: int) -> Any:
    err = _require_capability("manage_users")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    try:
        u = users_svc.update_user(user_id, body, updated_by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not u:
        return jsonify({"error": "Not found"}), 404
    db.insert_audit_log("user:" + str(user_id), "fio_user_update",
                        {"fields": list(body.keys())},
                        performed_by=actor)
    return jsonify(u)


@admin_bp.route("/fio-users/<int:user_id>", methods=["DELETE"])
def fio_users_delete(user_id: int) -> Any:
    err = _require_capability("manage_users")
    if err:
        return err
    actor = _current_user_name() or "admin"
    users_svc.delete_user(user_id)
    db.insert_audit_log("user:" + str(user_id), "fio_user_deactivate",
                        {}, performed_by=actor)
    return jsonify({"status": "deactivated"})


# ─────────────────────────────────────────────────────────────────────
# Paying accounts (bank accounts the holding wires from)
# ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/paying-accounts", methods=["GET"])
def paying_accounts_list() -> Any:
    active_only = (request.args.get("active") or "").lower() in ("true", "1", "yes")
    legal_entity = request.args.get("legal_entity") or None
    return jsonify({"accounts": pa_svc.list_accounts(
        active_only=active_only, legal_entity=legal_entity)})


@admin_bp.route("/paying-accounts", methods=["POST"])
def paying_accounts_create() -> Any:
    err = _require_capability("manage_payees")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    try:
        a = pa_svc.create_account(body, created_by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("create paying_account failed")
        return jsonify({"error": str(exc)}), 500
    db.insert_audit_log("paying_account:" + str(a["id"]),
                        "paying_account_create",
                        {"label": a["label"]}, performed_by=actor)
    return jsonify(a), 201


@admin_bp.route("/paying-accounts/<int:acc_id>", methods=["PATCH"])
def paying_accounts_update(acc_id: int) -> Any:
    err = _require_capability("manage_payees")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    a = pa_svc.update_account(acc_id, body, updated_by=actor)
    if not a:
        return jsonify({"error": "Not found"}), 404
    db.insert_audit_log("paying_account:" + str(acc_id),
                        "paying_account_update",
                        {"fields": list(body.keys())}, performed_by=actor)
    return jsonify(a)


@admin_bp.route("/paying-accounts/<int:acc_id>", methods=["DELETE"])
def paying_accounts_delete(acc_id: int) -> Any:
    err = _require_capability("manage_payees")
    if err:
        return err
    actor = _current_user_name() or "admin"
    pa_svc.delete_account(acc_id)
    db.insert_audit_log("paying_account:" + str(acc_id),
                        "paying_account_deactivate",
                        {}, performed_by=actor)
    return jsonify({"status": "deactivated"})


# ─────────────────────────────────────────────────────────────────────
# App settings (editable copy — chase template etc.)
# ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/settings", methods=["GET"])
def settings_list() -> Any:
    return jsonify(settings_svc.list_all())


@admin_bp.route("/settings/<key>", methods=["POST"])
def settings_set(key: str) -> Any:
    err = _require_role(roles_svc.ROLE_ADMIN, roles_svc.ROLE_BOOKKEEPER)
    if err:
        return err
    if key not in settings_svc.DEFAULTS:
        return jsonify({"error": "unknown setting key",
                        "allowed": sorted(settings_svc.DEFAULTS.keys())}), 400
    body = request.get_json(silent=True) or {}
    value = body.get("value", "")
    actor = _current_user_name() or "admin"
    settings_svc.set_(key, value or "", by=actor)
    db.insert_audit_log("settings:" + key, "settings_set",
                        {"value_preview": (value or "")[:80]},
                        performed_by=actor)
    return jsonify({"status": "saved",
                    "key": key,
                    "value": settings_svc.get(key)})


# ─────────────────────────────────────────────────────────────────────
# Reference data (read-only static — legal entities + whats_new feed)
# ─────────────────────────────────────────────────────────────────────

def _serve_json(filename: str, empty_payload: dict, status_on_error: int = 200):
    """Read a JSON file from data/ and return it as a JSON response.

    Used for legal_entities + whats_new feeds. These ship as files in
    seed/ and get copied to data/ on first boot (M81 seed pattern).
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "data", filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data), 200
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        payload = dict(empty_payload)
        payload["error"] = str(exc)
        return jsonify(payload), status_on_error


@admin_bp.route("/legal-entities", methods=["GET"])
def legal_entities() -> Any:
    return _serve_json("legal_entities.json", {"entities": []},
                       status_on_error=500)


@admin_bp.route("/whats-new", methods=["GET"])
def whats_new() -> Any:
    response = _serve_json("whats_new.json", {"entries": []},
                           status_on_error=200)
    # Augment with latest_version convenience field
    data, status = response
    if status == 200:
        body = data.get_json()
        entries = body.get("entries", [])
        body["latest_version"] = entries[0].get("version") if entries else None
        return jsonify(body), 200
    return response
