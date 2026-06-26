"""Stream Budgets + X-alarm log Blueprint — Phase 3 (2026-06-16).

Endpoints:
    GET    /api/stream-budgets?period=YYYY-MM
    GET    /api/stream-budgets/<pc>/<period>
    POST   /api/stream-budgets
    GET    /api/stream-budgets/<pc>/<period>/actuals
    GET    /api/stream-budgets/history?pc=&period=

    GET    /api/xalarm-log?pc=&period=&only_unack=true
    POST   /api/xalarm-log/<id>/acknowledge
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from services import stream_budgets as sb
from services import xalarm as xa
from services import roles as roles_svc

logger = logging.getLogger(__name__)

budgets_bp = Blueprint("budgets", __name__, url_prefix="/api")


def _current_user_name():
    name = (request.headers.get("X-FIO-User") or "").strip()
    return name or None


def _require_role(*allowed):
    user = _current_user_name()
    role = roles_svc.get_role(user)
    if role not in allowed:
        return jsonify({
            "error": "forbidden",
            "message": "Role '%s' is not allowed here. Required: %s" %
                       (role, list(allowed)),
            "you": user, "your_role": role,
        }), 403
    return None


_WRITE_ROLES = ("admin", "holding_ceo")


# 2026-06-26 (D2) — local capability gate
def _require_capability(cap: str):
    user = _current_user_name()
    if roles_svc.has_capability(user, cap):
        return None
    return jsonify({
        "error": "forbidden",
        "your_role": roles_svc.get_role(user),
        "missing_capability": cap,
        "message": "Capability '%s' required." % cap,
    }), 403


@budgets_bp.route("/stream-budgets", methods=["GET"])
def list_budgets() -> Any:
    period = (request.args.get("period") or "").strip() or None
    rows = sb.list_budgets(period=period)
    # Enrich with live actuals so the UI can show traffic-lights.
    out = []
    for r in rows:
        status = sb.is_over(r["profit_center"], r["period"])
        rec = dict(r)
        rec.update(status)
        out.append(rec)
    return jsonify({"budgets": out})


@budgets_bp.route("/stream-budgets/<pc>/<period>", methods=["GET"])
def get_budget(pc: str, period: str) -> Any:
    b = sb.get_budget(pc, period)
    status = sb.is_over(pc, period)
    return jsonify({"budget": b, "status": status})


@budgets_bp.route("/stream-budgets/<pc>/<period>/actuals", methods=["GET"])
def actuals(pc: str, period: str) -> Any:
    return jsonify(sb.is_over(pc, period))


@budgets_bp.route("/stream-budgets", methods=["POST"])
def set_budget() -> Any:
    err = _require_role(*_WRITE_ROLES) or _require_capability("manage_policies")
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    try:
        rec = sb.set_budget(
            pc=(body.get("profit_center") or "").strip(),
            period=(body.get("period") or "").strip(),
            eur=body.get("budget_eur"),
            agreed_by_ceo=body.get("agreed_by_ceo") or actor,
            agreed_by_ceo_at=body.get("agreed_by_ceo_at"),
            notes=body.get("notes"),
            reason=body.get("reason"),
            by=actor,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("set_budget failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(rec), 201


@budgets_bp.route("/stream-budgets/history", methods=["GET"])
def budget_history() -> Any:
    pc = request.args.get("pc") or None
    period = request.args.get("period") or None
    return jsonify({"history": sb.history_for(pc=pc, period=period)})


# ─────────────────────────────────────────────────────────────
# X-alarm log
# ─────────────────────────────────────────────────────────────

@budgets_bp.route("/xalarm-log", methods=["GET"])
def list_xalarm() -> Any:
    pc = request.args.get("pc") or None
    period = request.args.get("period") or None
    only_unack = (request.args.get("only_unack") or "").lower() in ("1", "true", "yes")
    return jsonify({"items": xa.list_log(pc=pc, period=period, only_unack=only_unack)})


@budgets_bp.route("/xalarm-log/<int:xid>/acknowledge", methods=["POST"])
def ack_xalarm(xid: int) -> Any:
    err = (_require_role("admin", "holding_ceo", "bookkeeper")
           or _require_capability("approve_budget"))
    if err:
        return err
    actor = _current_user_name() or "unknown"
    ok = xa.acknowledge(xid, by=actor)
    return jsonify({"status": "acknowledged" if ok else "already_or_missing"})
