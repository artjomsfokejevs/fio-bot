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

    Updates the actuals file with the amount under the appropriate
    stream (profit center) and period, then writes an audit log entry.

    Args:
        doc: Document dictionary from the database (must have ledger_code,
             profit_center, period, amount).
    """
    ledger_code = doc.get("ledger_code")
    profit_center = doc.get("profit_center") or "AG"
    period = doc.get("period") or datetime.utcnow().strftime("%Y-%m")
    amount = doc.get("amount") or 0.0

    if not ledger_code:
        raise ValueError("Cannot post without a ledger_code")

    actuals = _load_actuals()
    streams = actuals.setdefault("streams", {})

    # Use profit center name as stream key
    stream_name = _resolve_stream_name(profit_center)
    stream = streams.setdefault(stream_name, {})
    period_data = stream.setdefault(period, {})

    current = period_data.get(ledger_code, 0.0)
    period_data[ledger_code] = round(current + amount, 2)

    _save_actuals(actuals)

    # Update document status
    now = datetime.utcnow().isoformat()
    db.update_document(doc["id"], {"status": "posted", "posted_at": now})
    db.insert_audit_log(
        document_id=doc["id"],
        action="posted",
        details={
            "ledger_code": ledger_code,
            "profit_center": profit_center,
            "period": period,
            "amount": amount,
        },
        performed_by="system",
    )

    logger.info(
        "Posted doc %s: %s %.2f to %s/%s",
        doc["id"],
        ledger_code,
        amount,
        stream_name,
        period,
    )


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
