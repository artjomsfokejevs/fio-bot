"""Accounts Receivable (revenue documents) — Phase 1 of #94.

Mirrors the expense flow but for OUTGOING invoices we issue to customers.
A doc starts as a `proforma` (draft sale), gets converted to a real
`invoice` when the deal closes, then becomes `paid` once revenue_receipts
sums to amount_eur. `credit_note` is a third kind for refunds.

Status machine: draft → sent → partially_paid → paid (+ cancelled at any point).

Per docs/revenue-module-architecture.md.
"""
from __future__ import annotations

import json
import logging
import secrets
import string
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db
from services import pc_codes

logger = logging.getLogger(__name__)

__all__ = [
    "list_docs",
    "get_doc",
    "create_doc",
    "update_doc",
    "convert_proforma_to_invoice",
    "update_status",
    "delete_doc",
    "audit_for",
    "VALID_KINDS",
    "VALID_STATUSES",
]

VALID_KINDS = ("proforma", "invoice", "credit_note")
VALID_STATUSES = ("draft", "sent", "partially_paid", "paid", "cancelled")


def _gen_id(kind: str) -> str:
    """Short, prefix-stamped ID — easy to scan in logs / URLs."""
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    prefix = {"proforma": "pf", "invoice": "in", "credit_note": "cn"}.get(kind, "rd")
    return f"{prefix}_{suffix}"


def _now() -> str:
    return datetime.utcnow().isoformat()


def _audit(conn, doc_id: str, action: str, details: Optional[Dict[str, Any]] = None,
           actor: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO revenue_audit (revenue_doc_id, action, details_json, actor, occurred_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (doc_id, action, json.dumps(details) if details else None, actor, _now()),
    )


def list_docs(period: Optional[str] = None, pc: Optional[str] = None,
              status: Optional[str] = None, kind: Optional[str] = None,
              q: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
    """List revenue docs with optional filters. period = 'YYYY-MM' matches issue_date."""
    where: List[str] = []
    params: List[Any] = []
    if period:
        where.append("substr(issue_date, 1, 7) = ?")
        params.append(period)
    if pc:
        # Translate to canonical + include legacy aliases (read-time mapping)
        canonical = pc_codes.to_canonical(pc) or pc
        aliases = pc_codes.legacy_aliases_of(canonical)
        placeholders = ",".join("?" for _ in aliases)
        where.append(f"profit_center IN ({placeholders})")
        params.extend(aliases)
    if status:
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if q:
        like = "%" + q.strip().lower() + "%"
        where.append(
            "(LOWER(COALESCE(customer,'')) LIKE ? OR "
            " LOWER(COALESCE(invoice_number,'')) LIKE ? OR "
            " LOWER(COALESCE(description,'')) LIKE ? OR "
            " CAST(COALESCE(amount,0) AS TEXT) LIKE ?)"
        )
        params.extend([like, like, like, like])
    sql = "SELECT * FROM revenue_documents"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(issue_date, uploaded_at) DESC, id ASC LIMIT ?"
    params.append(limit)
    conn = db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def get_doc(doc_id: str) -> Optional[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM revenue_documents WHERE id = ?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _validate_payload(payload: Dict[str, Any], *, partial: bool = False) -> None:
    if not partial:
        for k in ("kind", "profit_center"):
            if not payload.get(k):
                raise ValueError(f"{k} required")
    if "kind" in payload and payload["kind"] not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}")
    if "status" in payload and payload["status"] not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    if "amount" in payload and payload["amount"] is not None:
        try:
            payload["amount"] = float(payload["amount"])
        except (TypeError, ValueError):
            raise ValueError("amount must be a number")
    if "amount_eur" in payload and payload["amount_eur"] is not None:
        try:
            payload["amount_eur"] = float(payload["amount_eur"])
        except (TypeError, ValueError):
            raise ValueError("amount_eur must be a number")


def create_doc(payload: Dict[str, Any], *, by: Optional[str] = None) -> Dict[str, Any]:
    _validate_payload(payload)
    doc_id = _gen_id(payload["kind"])
    payload["profit_center"] = pc_codes.to_canonical(payload["profit_center"]) or payload["profit_center"]
    # Default amount_eur to amount when currency is EUR
    if payload.get("amount") is not None and payload.get("amount_eur") is None:
        if (payload.get("currency") or "EUR").upper() == "EUR":
            payload["amount_eur"] = float(payload["amount"])
    now = _now()
    fields = ["id", "kind", "profit_center", "customer", "customer_vat",
              "legal_entity", "invoice_number", "issue_date", "due_date",
              "amount", "amount_eur", "currency", "description",
              "ledger_code", "status", "proforma_id", "uploaded_at",
              "uploaded_by", "file_path", "file_type", "parsed_json",
              "classification_json", "notes"]
    values = [doc_id, payload["kind"], payload["profit_center"],
              payload.get("customer"), payload.get("customer_vat"),
              payload.get("legal_entity"), payload.get("invoice_number"),
              payload.get("issue_date"), payload.get("due_date"),
              payload.get("amount"), payload.get("amount_eur"),
              payload.get("currency", "EUR"),
              payload.get("description"), payload.get("ledger_code"),
              payload.get("status", "draft"), payload.get("proforma_id"),
              now, by or payload.get("uploaded_by"),
              payload.get("file_path"), payload.get("file_type"),
              json.dumps(payload["parsed_json"]) if isinstance(payload.get("parsed_json"), dict) else payload.get("parsed_json"),
              json.dumps(payload["classification_json"]) if isinstance(payload.get("classification_json"), dict) else payload.get("classification_json"),
              payload.get("notes")]
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO revenue_documents ({}) VALUES ({})".format(
                ", ".join(fields), ", ".join("?" for _ in fields)
            ),
            values,
        )
        _audit(conn, doc_id, "created", {"kind": payload["kind"], "pc": payload["profit_center"]}, by)
        conn.commit()
    finally:
        conn.close()
    return get_doc(doc_id)  # type: ignore[return-value]


_EDITABLE_FIELDS = {
    "kind", "profit_center", "customer", "customer_vat", "legal_entity",
    "invoice_number", "issue_date", "due_date", "amount", "amount_eur",
    "currency", "description", "ledger_code", "status", "notes",
    # 2026-07-08 (C5) — file_path/file_type were PATCH-editable, so any
    # write-role user could set file_path="/app/data/fio.db" then GET
    # /revenue/<id>/file to exfiltrate the database (or .env). These are
    # set ONLY by the upload handler now, never via the generic PATCH.
}


def update_doc(doc_id: str, patch: Dict[str, Any], *, by: Optional[str] = None,
               allow_file_fields: bool = False) -> Optional[Dict[str, Any]]:
    """Patch a revenue doc.

    2026-07-08 (C5) — file_path/file_type are NOT in _EDITABLE_FIELDS (a
    PATCH-set path was an arbitrary-file-read vector). The upload handler
    passes allow_file_fields=True and we accept file_path ONLY when it
    resolves under UPLOAD_FOLDER/revenue, so a caller can never point it
    at the database or .env.
    """
    existing = get_doc(doc_id)
    if not existing:
        return None
    _validate_payload(patch, partial=True)
    if "profit_center" in patch and patch["profit_center"]:
        patch["profit_center"] = pc_codes.to_canonical(patch["profit_center"]) or patch["profit_center"]
    _allowed = set(_EDITABLE_FIELDS)
    if allow_file_fields:
        import os as _os
        import config as _cfg
        safe_root = _os.path.realpath(_os.path.join(_cfg.UPLOAD_FOLDER, "revenue"))
        fp = patch.get("file_path")
        if fp and _os.path.realpath(str(fp)).startswith(safe_root + _os.sep):
            _allowed = _allowed | {"file_path", "file_type"}
        else:
            # Reject an out-of-tree path outright instead of silently dropping it.
            patch.pop("file_path", None)
            patch.pop("file_type", None)
    cols, vals = [], []
    for k, v in patch.items():
        if k in _allowed:
            cols.append(f"{k} = ?")
            vals.append(v)
    if not cols:
        return existing
    conn = db.get_connection()
    try:
        conn.execute(
            f"UPDATE revenue_documents SET {', '.join(cols)} WHERE id = ?",
            (*vals, doc_id),
        )
        _audit(conn, doc_id, "updated", {"fields": list(patch.keys())}, by)
        conn.commit()
    finally:
        conn.close()
    return get_doc(doc_id)


def update_status(doc_id: str, new_status: str, *, by: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if new_status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    existing = get_doc(doc_id)
    if not existing:
        return None
    old_status = existing.get("status")
    if old_status == new_status:
        return existing
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE revenue_documents SET status = ? WHERE id = ?",
            (new_status, doc_id),
        )
        _audit(conn, doc_id, "status_changed",
               {"from": old_status, "to": new_status}, by)
        conn.commit()
    finally:
        conn.close()
    return get_doc(doc_id)


def convert_proforma_to_invoice(proforma_id: str, invoice_payload: Dict[str, Any],
                                *, by: Optional[str] = None) -> Dict[str, Any]:
    """Create a new `invoice` doc that supersedes a `proforma`.

    The new invoice carries proforma_id back-ref; the proforma's status
    flips to 'cancelled' so it can't be paid twice.
    """
    proforma = get_doc(proforma_id)
    if not proforma:
        raise ValueError(f"proforma {proforma_id} not found")
    if proforma["kind"] != "proforma":
        raise ValueError(f"{proforma_id} is not a proforma (kind={proforma['kind']})")
    invoice_payload = dict(invoice_payload)
    invoice_payload["kind"] = "invoice"
    invoice_payload["proforma_id"] = proforma_id
    invoice_payload.setdefault("profit_center", proforma["profit_center"])
    invoice_payload.setdefault("customer", proforma.get("customer"))
    invoice_payload.setdefault("customer_vat", proforma.get("customer_vat"))
    invoice_payload.setdefault("legal_entity", proforma.get("legal_entity"))
    invoice_payload.setdefault("amount", proforma.get("amount"))
    invoice_payload.setdefault("amount_eur", proforma.get("amount_eur"))
    invoice_payload.setdefault("currency", proforma.get("currency", "EUR"))
    invoice_payload.setdefault("description", proforma.get("description"))
    invoice_payload.setdefault("ledger_code", proforma.get("ledger_code"))
    new_doc = create_doc(invoice_payload, by=by)
    # Cancel the proforma
    conn = db.get_connection()
    try:
        conn.execute("UPDATE revenue_documents SET status = 'cancelled' WHERE id = ?",
                     (proforma_id,))
        _audit(conn, proforma_id, "converted",
               {"to_invoice": new_doc["id"]}, by)
        _audit(conn, new_doc["id"], "converted_from",
               {"from_proforma": proforma_id}, by)
        conn.commit()
    finally:
        conn.close()
    return get_doc(new_doc["id"])  # type: ignore[return-value]


def delete_doc(doc_id: str, *, by: Optional[str] = None) -> bool:
    existing = get_doc(doc_id)
    if not existing:
        return False
    conn = db.get_connection()
    try:
        # SQLite default has PRAGMA foreign_keys=OFF, so we cascade manually.
        # Audit row is written FIRST so the snapshot survives even if delete fails.
        _audit(conn, doc_id, "deleted",
               {"snapshot": {"kind": existing["kind"], "amount": existing.get("amount"),
                             "status": existing.get("status")}}, by)
        conn.execute("DELETE FROM revenue_receipts WHERE revenue_doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM revenue_documents WHERE id = ?", (doc_id,))
        conn.commit()
    finally:
        conn.close()
    return True


def audit_for(doc_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM revenue_audit WHERE revenue_doc_id = ? "
            "ORDER BY occurred_at DESC LIMIT ?",
            (doc_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            rec = dict(r)
            if rec.get("details_json"):
                try:
                    rec["details"] = json.loads(rec["details_json"])
                except Exception:
                    rec["details"] = rec["details_json"]
            out.append(rec)
        return out
    finally:
        conn.close()
