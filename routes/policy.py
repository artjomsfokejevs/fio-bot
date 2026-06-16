"""Policy rules + Policy-violation approvals Blueprint.

Two clusters:
    Editable policy rules        /api/policy-rules (5)
    Violation approvals (audit)  /api/policy-violations/approve (3)

Added 2026-06-16 for Phase 1 P1.2 + P1.3.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from services import policy_rules as pr_svc
from services import policy_approvals as pva_svc
from services import roles as roles_svc

logger = logging.getLogger(__name__)

policy_bp = Blueprint("policy", __name__, url_prefix="/api")


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


# ─────────────────────────────────────────────────────────────
# Policy rules CRUD (Admin / CEO)
# ─────────────────────────────────────────────────────────────

_RULE_WRITE_ROLES = ("admin", roles_svc.ROLE_HOLDING_CEO if hasattr(roles_svc, "ROLE_HOLDING_CEO") else "holding_ceo",
                     "bookkeeper")


@policy_bp.route("/policy-rules", methods=["GET"])
def list_policy_rules() -> Any:
    active_only = (request.args.get("active") or "").lower() in ("true", "1", "yes")
    rows = pr_svc.list_rules(active_only=active_only)
    return jsonify({"rules": rows, "defaults": pr_svc.DEFAULTS_BY_POLICY})


@policy_bp.route("/policy-rules", methods=["POST"])
def create_policy_rule() -> Any:
    err = _require_role(*_RULE_WRITE_ROLES)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    try:
        rule = pr_svc.create_rule(body, by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_policy_rule failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(rule), 201


@policy_bp.route("/policy-rules/<int:rule_id>", methods=["PATCH"])
def update_policy_rule(rule_id: int) -> Any:
    err = _require_role(*_RULE_WRITE_ROLES)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    try:
        rule = pr_svc.update_rule(rule_id, body, by=actor)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("update_policy_rule failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(rule)


@policy_bp.route("/policy-rules/<int:rule_id>", methods=["DELETE"])
def delete_policy_rule(rule_id: int) -> Any:
    err = _require_role(*_RULE_WRITE_ROLES)
    if err:
        return err
    actor = _current_user_name() or "admin"
    ok = pr_svc.delete_rule(rule_id, by=actor)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deactivated"})


@policy_bp.route("/policy-rules/history", methods=["GET"])
def policy_rules_history() -> Any:
    rule_id = request.args.get("rule_id")
    rid = int(rule_id) if rule_id and rule_id.isdigit() else None
    limit = int(request.args.get("limit") or 100)
    return jsonify({"history": pr_svc.history_for(rid, limit=limit)})


# ─────────────────────────────────────────────────────────────
# Violation approvals (Accountant / CEO ack the warning)
# ─────────────────────────────────────────────────────────────

_VIOLATION_APPROVE_ROLES = (
    "admin",
    "bookkeeper",
    roles_svc.ROLE_HOLDING_CEO if hasattr(roles_svc, "ROLE_HOLDING_CEO") else "holding_ceo",
)


@policy_bp.route("/policy-violations/approve", methods=["POST"])
def approve_violation() -> Any:
    err = _require_role(*_VIOLATION_APPROVE_ROLES)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    key = (body.get("violation_key") or "").strip()
    if not key:
        return jsonify({"error": "violation_key required"}), 400
    actor = _current_user_name() or "unknown"
    role = roles_svc.get_role(actor)
    try:
        rec = pva_svc.approve(
            key=key,
            doc_id=body.get("doc_id"),
            policy_name=body.get("policy_name") or "",
            level=body.get("level") or "",
            message=body.get("message") or "",
            approved_by=actor,
            role=role,
            reason=(body.get("reason") or "").strip() or None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("approve_violation failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(rec), 201


@policy_bp.route("/policy-violations/approvals", methods=["GET"])
def list_violation_approvals() -> Any:
    doc_id = request.args.get("doc_id") or None
    rows = pva_svc.list_approvals(doc_id=doc_id)
    return jsonify({"approvals": rows})


@policy_bp.route("/policy-violations/approvals/<int:approval_id>", methods=["DELETE"])
def delete_violation_approval(approval_id: int) -> Any:
    err = _require_role("admin")
    if err:
        return err
    ok = pva_svc.delete_approval(approval_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "removed"})
