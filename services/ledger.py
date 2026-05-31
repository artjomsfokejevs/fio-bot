"""Ledger posting service -- writes approved entries to actuals JSON and audit log."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import config
from services import db

logger = logging.getLogger(__name__)

__all__ = ["post_to_actuals"]


def post_to_actuals(doc: Dict[str, Any]) -> None:
    """Post an approved document to accounting_actuals.json.

    If the document has an `allocations_json` value (split across multiple
    profit centers), each split is posted to its own PC/code separately.
    Otherwise falls back to single-PC posting under doc.profit_center.
    """
    period = doc.get("period") or datetime.utcnow().strftime("%Y-%m")
    total_amount = doc.get("amount") or 0.0

    # Try to parse allocations
    allocations = _parse_allocations(doc.get("allocations_json"))

    actuals = _load_actuals()
    streams = actuals.setdefault("streams", {})

    posted_entries = []  # for the audit log

    if allocations:
        # Multi-stream split posting
        for row in allocations:
            row_pc = row.get("profit_center") or "AG"
            row_code = row.get("ledger_code") or doc.get("ledger_code")
            row_amount = row.get("amount")
            if row_amount is None and row.get("percentage") is not None and total_amount:
                row_amount = round(float(total_amount) * float(row["percentage"]) / 100.0, 2)
            if not row_code:
                raise ValueError("Allocation row missing ledger_code")
            row_amount = float(row_amount or 0)
            stream_name = _resolve_stream_name(row_pc)
            period_data = streams.setdefault(stream_name, {}).setdefault(period, {})
            current = period_data.get(row_code, 0.0)
            period_data[row_code] = round(current + row_amount, 2)
            posted_entries.append({
                "profit_center": row_pc,
                "ledger_code": row_code,
                "amount": row_amount,
                "percentage": row.get("percentage"),
                "note": row.get("note", ""),
            })
    else:
        # Single-stream posting (legacy / default)
        ledger_code = doc.get("ledger_code")
        if not ledger_code:
            raise ValueError("Cannot post without a ledger_code")
        profit_center = doc.get("profit_center") or "AG"
        stream_name = _resolve_stream_name(profit_center)
        period_data = streams.setdefault(stream_name, {}).setdefault(period, {})
        current = period_data.get(ledger_code, 0.0)
        period_data[ledger_code] = round(current + float(total_amount), 2)
        posted_entries.append({
            "profit_center": profit_center,
            "ledger_code": ledger_code,
            "amount": float(total_amount),
        })

    _save_actuals(actuals)

    # Update document status
    now = datetime.utcnow().isoformat()
    db.update_document(doc["id"], {"status": "posted", "posted_at": now})
    db.insert_audit_log(
        document_id=doc["id"],
        action="posted",
        details={
            "period": period,
            "total_amount": total_amount,
            "split": len(posted_entries) > 1,
            "entries": posted_entries,
        },
        performed_by="system",
    )

    logger.info(
        "Posted doc %s: %d entries totalling %.2f across %s",
        doc["id"],
        len(posted_entries),
        total_amount,
        ", ".join(sorted({e["profit_center"] for e in posted_entries})),
    )


def _parse_allocations(allocations_json: Any) -> list:
    """Safely decode the allocations_json column into a list of dicts."""
    if not allocations_json:
        return []
    if isinstance(allocations_json, list):
        return [r for r in allocations_json if isinstance(r, dict)]
    if isinstance(allocations_json, str):
        try:
            data = json.loads(allocations_json)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        except json.JSONDecodeError:
            return []
    return []


def _load_actuals() -> Dict[str, Any]:
    """Load the actuals JSON file."""
    try:
        with open(config.ACTUALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"streams": {}}


def _save_actuals(actuals: Dict[str, Any]) -> None:
    """Save the actuals JSON file."""
    with open(config.ACTUALS_FILE, "w", encoding="utf-8") as f:
        json.dump(actuals, f, indent=2, ensure_ascii=False)


def _resolve_stream_name(profit_center_code: str) -> str:
    """Convert a profit center code to its stream name.

    Args:
        profit_center_code: Short code like 'AA', 'AG', etc.

    Returns:
        Lowercase stream name for the actuals file.
    """
    mapping: Dict[str, str] = {
        "AG": "amitours_group",
        "AA": "alps2alps",
        "RR": "rock2rock",
        "BK": "skibookers",
        "SR": "skipasser",
        "MT": "mountly",
        "AH": "mountly",
        "PK": "mypeak_finance",
        "CF": "mypeak_finance",
        "AL": "alveda",
    }
    return mapping.get(profit_center_code, profit_center_code.lower())
