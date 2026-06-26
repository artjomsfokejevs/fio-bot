"""Travel / per-diem report module — MCP-3 (2026-06-26).

Models a business trip as a single `documents` row with kind='travel'.

Per-diem rates default per LV Cabinet Regulation No. 969 (2026 amendments)
and EE per-diem schedule. The operator can override per-trip.

Travel report fields stored as JSON in documents.parsed_json:
  {
    "kind": "travel",
    "destination_country": "EE",   # ISO 3166-1 alpha-2
    "destination_city":    "Tallinn",
    "purpose":             "Q3 review with TransferHub",
    "departure_date":      "2026-07-01",
    "return_date":         "2026-07-04",
    "per_diem_eur":        50.0,    # operator-set or default-by-country
    "days":                4,
    "per_diem_total_eur":  200.0,
    "expense_lines": [...]          # optional extra costs (taxi, hotel) the
                                    # operator listed manually
  }

The document.amount = per_diem_total_eur + sum(expense_lines.amount_eur),
status='pending' so it lands in Approve like any other doc.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = [
    "PER_DIEM_DEFAULTS_EUR",
    "default_per_diem_for",
    "create_travel_report",
    "update_travel_report",
    "list_travel_reports",
]


# LV/EE 2026 baseline rates per Cabinet Reg. No.969 + EE schedule.
# Operator can override per trip; this is the "Suggest" default.
PER_DIEM_DEFAULTS_EUR: Dict[str, float] = {
    "LV": 6.0,    # domestic LV
    "EE": 50.0,
    "LT": 45.0,
    "DE": 60.0, "FR": 60.0, "ES": 60.0, "IT": 60.0, "NL": 60.0,
    "AT": 60.0, "BE": 60.0, "CH": 70.0, "GB": 70.0, "FI": 60.0,
    "SE": 60.0, "DK": 60.0, "NO": 65.0, "IE": 60.0, "PT": 50.0,
    "PL": 45.0, "CZ": 45.0, "SK": 45.0, "HU": 45.0, "RO": 45.0,
    "BG": 45.0, "GR": 50.0, "HR": 45.0, "SI": 50.0, "MT": 50.0,
    "CY": 50.0, "LU": 60.0,
    "US": 80.0, "CA": 70.0, "JP": 75.0, "AE": 80.0, "AM": 50.0,
    # everything else → 60.0 fallback
}


def default_per_diem_for(country_code: Optional[str]) -> float:
    if not country_code:
        return 60.0
    return PER_DIEM_DEFAULTS_EUR.get(country_code.upper().strip(), 60.0)


def _days_between(dep: str, ret: str) -> int:
    """Inclusive count of trip days. Departure + return are both counted."""
    try:
        d1 = date.fromisoformat(dep[:10])
        d2 = date.fromisoformat(ret[:10])
    except (ValueError, TypeError):
        return 0
    delta = (d2 - d1).days + 1
    return max(0, delta)


def _calc_totals(payload: Dict[str, Any]) -> Dict[str, Any]:
    days = _days_between(payload.get("departure_date") or "",
                          payload.get("return_date") or "")
    rate = float(payload.get("per_diem_eur") or 0)
    per_diem_total = round(days * rate, 2)
    extra_total = 0.0
    for ln in payload.get("expense_lines") or []:
        try:
            extra_total += float(ln.get("amount_eur") or 0)
        except (TypeError, ValueError):
            continue
    extra_total = round(extra_total, 2)
    payload["days"] = days
    payload["per_diem_total_eur"] = per_diem_total
    payload["expense_total_eur"] = extra_total
    payload["total_eur"] = round(per_diem_total + extra_total, 2)
    return payload


def _required(payload: Dict[str, Any]) -> Optional[str]:
    if not payload.get("departure_date") or not payload.get("return_date"):
        return "departure_date and return_date are required"
    if _days_between(payload["departure_date"], payload["return_date"]) <= 0:
        return "return_date must be on or after departure_date"
    if not payload.get("destination_country"):
        return "destination_country (ISO-2) is required"
    if not payload.get("profit_center"):
        return "profit_center is required"
    return None


def create_travel_report(payload: Dict[str, Any], *,
                          by: Optional[str] = None) -> Dict[str, Any]:
    err = _required(payload)
    if err:
        raise ValueError(err)
    payload = dict(payload)
    # Auto-fill per_diem_eur if absent
    if not payload.get("per_diem_eur"):
        payload["per_diem_eur"] = default_per_diem_for(
            payload.get("destination_country"))
    payload["kind"] = "travel"
    payload = _calc_totals(payload)

    doc_id = "tr_" + uuid.uuid4().hex[:10]
    now = datetime.utcnow().isoformat()
    vendor = "Travel: " + (payload.get("destination_country") or "?")
    if payload.get("destination_city"):
        vendor += " · " + payload["destination_city"]
    period = (payload.get("departure_date") or now)[:7]

    db.insert_document({
        "id": doc_id,
        "filename": doc_id + ".json",
        "original_name": "Travel report — " + vendor,
        "file_type": "json",
        "file_size": 0,  # virtual doc — no file on disk
        "uploaded_at": now,
        "uploaded_by": by or "operator",
        "status": "pending",
    })
    db.update_document(doc_id, {
        "vendor": vendor,
        "amount": payload["total_eur"],
        "currency": "EUR",
        "profit_center": payload.get("profit_center"),
        "ledger_code": payload.get("ledger_code") or "OP00",
        "period": period,
        "parsed_json": json.dumps(payload),
    })
    try:
        db.insert_audit_log(doc_id, "travel_report_created", {
            "country": payload.get("destination_country"),
            "days": payload.get("days"),
            "total_eur": payload.get("total_eur"),
            "by": by,
        })
    except Exception:  # noqa: BLE001
        logger.exception("travel report audit log failed for %s", doc_id)
    return {"id": doc_id, **payload}


def update_travel_report(doc_id: str, patch: Dict[str, Any], *,
                          by: Optional[str] = None) -> Dict[str, Any]:
    row = db.get_document(doc_id)
    if not row:
        raise ValueError("travel report not found: " + doc_id)
    # parsed_json is auto-decoded by services.db._row_to_dict; tolerate
    # either dict (normal path) or string (legacy / malformed) input.
    raw = row.get("parsed_json")
    if isinstance(raw, dict):
        payload = dict(raw)
    elif isinstance(raw, str) and raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
    else:
        payload = {}
    if payload.get("kind") != "travel":
        raise ValueError("doc " + doc_id + " is not a travel report")
    payload.update({k: v for k, v in patch.items() if v is not None})
    payload = _calc_totals(payload)
    db.update_document(doc_id, {
        "amount": payload["total_eur"],
        "parsed_json": json.dumps(payload),
        "profit_center": payload.get("profit_center") or row.get("profit_center"),
        "ledger_code": payload.get("ledger_code") or row.get("ledger_code"),
    })
    try:
        db.insert_audit_log(doc_id, "travel_report_updated", {
            "fields": list(patch.keys()), "by": by,
        })
    except Exception:  # noqa: BLE001
        pass
    return {"id": doc_id, **payload}


def list_travel_reports(period: Optional[str] = None,
                         pc: Optional[str] = None,
                         limit: int = 100) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        sql = ("SELECT * FROM documents WHERE file_type = 'json' "
               "AND parsed_json LIKE '%\"kind\": \"travel\"%'")
        params: List[Any] = []
        if period:
            sql += " AND period = ?"
            params.append(period)
        if pc:
            sql += " AND profit_center = ?"
            params.append(pc)
        sql += " ORDER BY uploaded_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        raw = d.get("parsed_json")
        if isinstance(raw, str) and raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
        elif isinstance(raw, dict):
            payload = raw
        else:
            payload = {}
        d["travel"] = payload
        out.append(d)
    return out
