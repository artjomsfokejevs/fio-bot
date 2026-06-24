"""Payments Blueprint — partial payments on internal invoices (P1.5)
plus the small is_internal toggle endpoint.

Added 2026-06-16.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from services import db
from services import partial_payments as pp_svc
from services import roles as roles_svc

logger = logging.getLogger(__name__)

payments_bp = Blueprint("payments", __name__, url_prefix="/api")


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


_WRITE_ROLES = ("admin", "bookkeeper", "holding_ceo")


# ─────────────────────────────────────────────────────────────
# Partial payments
# ─────────────────────────────────────────────────────────────

@payments_bp.route("/documents/<doc_id>/partial-payments", methods=["GET"])
def list_partial_payments(doc_id: str) -> Any:
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "doc not found"}), 404
    items = pp_svc.list_for_doc(doc_id)
    total_paid = pp_svc.total_paid(doc_id)
    invoice_total = float(doc.get("amount") or 0.0)
    return jsonify({
        "doc_id": doc_id,
        "is_internal": bool(doc.get("is_internal")),
        "invoice_total": invoice_total,
        "total_paid": total_paid,
        "remaining": max(0.0, invoice_total - total_paid),
        "currency": doc.get("currency") or "EUR",
        "items": items,
    })


@payments_bp.route("/documents/<doc_id>/partial-payments", methods=["POST"])
def add_partial_payment(doc_id: str) -> Any:
    err = _require_role(*_WRITE_ROLES)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    actor = _current_user_name() or "admin"
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "doc not found"}), 404
    # 2026-06-24 (FB-1) — dropped the is_internal restriction. Real bug:
    # MyPeak Finance (CNSe) invoice was partial-paid 19/06 + rest 22/06,
    # bookkeeper could only write a comment because the invoice wasn't
    # flagged as internal. Partial payments now work for ANY invoice.
    try:
        rec = pp_svc.add(
            doc_id=doc_id,
            amount_eur=body.get("amount_eur"),
            paid_at=(body.get("paid_at") or "").strip(),
            method=(body.get("method") or "").strip() or None,
            reference=(body.get("reference") or "").strip() or None,
            by=actor,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("add_partial_payment failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(rec), 201


@payments_bp.route("/partial-payments/<int:partial_id>", methods=["DELETE"])
def delete_partial_payment(partial_id: int) -> Any:
    err = _require_role(*_WRITE_ROLES)
    if err:
        return err
    ok = pp_svc.delete(partial_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted"})


# ─────────────────────────────────────────────────────────────
# is_internal flag toggle (P1.5 prerequisite)
# ─────────────────────────────────────────────────────────────

@payments_bp.route("/documents/<doc_id>/is-internal", methods=["PATCH"])
def toggle_is_internal(doc_id: str) -> Any:
    err = _require_role(*_WRITE_ROLES)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    val = 1 if bool(body.get("is_internal")) else 0
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "doc not found"}), 404
    conn = db.get_connection()
    try:
        conn.execute("UPDATE documents SET is_internal = ? WHERE id = ?",
                     (val, doc_id))
        conn.commit()
    finally:
        conn.close()
    actor = _current_user_name() or "admin"
    try:
        db.insert_audit_log(doc_id, "is_internal_toggled",
                            {"is_internal": val}, performed_by=actor)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"doc_id": doc_id, "is_internal": bool(val)})


@payments_bp.route("/documents/<doc_id>/is-salary", methods=["PATCH"])
def toggle_is_salary(doc_id: str) -> Any:
    """Phase 2 — Salaries section. Bookkeeper marks salary invoices so
    they show in a dedicated Salaries filter in Budget Check (avoids
    mixing salary docs with vendor invoices)."""
    err = _require_role(*_WRITE_ROLES)
    if err:
        return err
    body = request.get_json(silent=True) or {}
    val = 1 if bool(body.get("is_salary")) else 0
    doc = db.get_document(doc_id)
    if not doc:
        return jsonify({"error": "doc not found"}), 404
    conn = db.get_connection()
    try:
        conn.execute("UPDATE documents SET is_salary = ? WHERE id = ?", (val, doc_id))
        conn.commit()
    finally:
        conn.close()
    actor = _current_user_name() or "admin"
    try:
        db.insert_audit_log(doc_id, "is_salary_toggled",
                            {"is_salary": val}, performed_by=actor)
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"doc_id": doc_id, "is_salary": bool(val)})
