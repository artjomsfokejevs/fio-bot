"""Revenue / Accounts Receivable Blueprint — Phase 1 of #94.

Endpoints:
    GET    /api/revenue                          — list (filters)
    GET    /api/revenue/<id>                     — single doc + receipts + audit
    POST   /api/revenue                          — create proforma/invoice/credit_note
    POST   /api/revenue/<id>                     — patch fields
    POST   /api/revenue/<id>/convert             — proforma → invoice
    POST   /api/revenue/<id>/status              — explicit status change
    DELETE /api/revenue/<id>

    GET    /api/revenue/<id>/receipts            — list receipts
    POST   /api/revenue/<id>/receipts            — add receipt
    DELETE /api/revenue-receipts/<int:receipt_id>

Role gating mirrors expense routes:
    read:        admin / holding_ceo / bookkeeper / stream_owner
    write:       admin / bookkeeper / stream_owner (own stream)
    delete:      admin only
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from services import revenue as rev_svc
from services import revenue_receipts as rr_svc
from services import cashflow as cf_svc
from services import revenue_bank_match as bm_svc
from services import roles as roles_svc

logger = logging.getLogger(__name__)

revenue_bp = Blueprint("revenue", __name__, url_prefix="/api")

_READ_ROLES = ("admin", "holding_ceo", "bookkeeper", "stream_owner")
_WRITE_ROLES = ("admin", "bookkeeper", "stream_owner")
_DELETE_ROLES = ("admin",)


def _user():
    return (request.headers.get("X-FIO-User") or "").strip() or None


def _require(*allowed):
    role = roles_svc.get_role(_user())
    if role not in allowed:
        return jsonify({
            "error": "forbidden",
            "your_role": role,
            "required": list(allowed),
        }), 403
    return None


# ── Documents ────────────────────────────────────────────────────────────────

@revenue_bp.route("/revenue", methods=["GET"])
def list_revenue() -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    return jsonify({
        "docs": rev_svc.list_docs(
            period=request.args.get("period") or None,
            pc=request.args.get("pc") or None,
            status=request.args.get("status") or None,
            kind=request.args.get("kind") or None,
            q=request.args.get("q") or None,
            limit=int(request.args.get("limit") or 500),
        ),
    })


@revenue_bp.route("/revenue/<doc_id>", methods=["GET"])
def get_revenue(doc_id: str) -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    doc = rev_svc.get_doc(doc_id)
    if not doc:
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        "doc": doc,
        "receipts": rr_svc.list_receipts(doc_id),
        "total_received": rr_svc.total_received(doc_id),
        "remaining": rr_svc.remaining(doc_id),
        "audit": rev_svc.audit_for(doc_id),
    })


@revenue_bp.route("/revenue", methods=["POST"])
def create_revenue() -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        doc = rev_svc.create_doc(payload, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    return jsonify({"doc": doc}), 201


@revenue_bp.route("/revenue/<doc_id>", methods=["POST"])
def update_revenue(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        doc = rev_svc.update_doc(doc_id, payload, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    if not doc:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"doc": doc})


@revenue_bp.route("/revenue/<doc_id>/convert", methods=["POST"])
def convert_revenue(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        doc = rev_svc.convert_proforma_to_invoice(doc_id, payload, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    return jsonify({"doc": doc}), 201


@revenue_bp.route("/revenue/<doc_id>/status", methods=["POST"])
def status_revenue(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    new_status = (payload.get("status") or "").strip()
    try:
        doc = rev_svc.update_status(doc_id, new_status, by=_user())
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    if not doc:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"doc": doc})


@revenue_bp.route("/revenue/<doc_id>", methods=["DELETE"])
def delete_revenue(doc_id: str) -> Any:
    err = _require(*_DELETE_ROLES)
    if err:
        return err
    ok = rev_svc.delete_doc(doc_id, by=_user())
    if not ok:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


# ── Receipts ─────────────────────────────────────────────────────────────────

@revenue_bp.route("/revenue/<doc_id>/receipts", methods=["GET"])
def list_receipts(doc_id: str) -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    return jsonify({
        "receipts": rr_svc.list_receipts(doc_id),
        "total_received": rr_svc.total_received(doc_id),
        "remaining": rr_svc.remaining(doc_id),
    })


@revenue_bp.route("/revenue/<doc_id>/receipts", methods=["POST"])
def add_receipt(doc_id: str) -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        receipt = rr_svc.add_receipt(
            doc_id,
            amount_eur=payload.get("amount_eur"),
            received_at=payload.get("received_at"),
            method=payload.get("method"),
            reference=payload.get("reference"),
            bank_statement_tx_id=payload.get("bank_statement_tx_id"),
            by=_user(),
        )
    except ValueError as exc:
        return jsonify({"error": "invalid_payload", "message": str(exc)}), 400
    return jsonify({
        "receipt": receipt,
        "doc": rev_svc.get_doc(doc_id),  # so UI sees status transition immediately
        "total_received": rr_svc.total_received(doc_id),
        "remaining": rr_svc.remaining(doc_id),
    }), 201


@revenue_bp.route("/revenue-receipts/<int:receipt_id>", methods=["DELETE"])
def delete_receipt(receipt_id: int) -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    ok = rr_svc.delete_receipt(receipt_id, by=_user())
    if not ok:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


# ── Cashflow analytics (Phase 2 of #94) ─────────────────────────────────────

@revenue_bp.route("/cashflow", methods=["GET"])
def cashflow_monthly() -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    pf = request.args.get("from") or None
    pt = request.args.get("to") or None
    pc = request.args.get("pc") or None
    return jsonify({
        "series": cf_svc.monthly_series(pf, pt, pc),
        "totals": cf_svc.totals_for_period(pf, pt, pc),
    })


@revenue_bp.route("/cashflow/by-stream", methods=["GET"])
def cashflow_by_stream() -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    return jsonify({
        "rows": cf_svc.breakdown_by_stream(
            request.args.get("from") or None,
            request.args.get("to") or None,
        ),
    })


@revenue_bp.route("/revenue/bank-match/suggestions/<batch_id>", methods=["GET"])
def bank_match_suggestions(batch_id: str) -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    return jsonify({"suggestions": bm_svc.suggestions_for_batch(batch_id)})


@revenue_bp.route("/revenue/bank-match/apply", methods=["POST"])
def bank_match_apply() -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    tx_id = (payload.get("tx_id") or "").strip()
    doc_id = (payload.get("revenue_doc_id") or "").strip()
    if not tx_id or not doc_id:
        return jsonify({"error": "tx_id and revenue_doc_id required"}), 400
    try:
        receipt = bm_svc.apply_match(tx_id, doc_id, by=_user())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"receipt": receipt,
                    "doc": rev_svc.get_doc(doc_id)}), 201


@revenue_bp.route("/revenue/bank-match/auto/<batch_id>", methods=["POST"])
def bank_match_auto(batch_id: str) -> Any:
    err = _require(*_WRITE_ROLES)
    if err:
        return err
    min_score = int(request.args.get("min_score") or 80)
    applied = bm_svc.auto_match_batch(batch_id, min_score=min_score, by=_user())
    return jsonify({"applied": applied, "count": len(applied)})


@revenue_bp.route("/cashflow/by-ledger", methods=["GET"])
def cashflow_by_ledger() -> Any:
    err = _require(*_READ_ROLES)
    if err:
        return err
    return jsonify({
        "rows": cf_svc.breakdown_by_ledger(
            request.args.get("from") or None,
            request.args.get("to") or None,
            request.args.get("side") or "both",
        ),
    })
