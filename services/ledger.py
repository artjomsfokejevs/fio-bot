"""Ledger posting service -- writes approved entries to actuals JSON and audit log."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import config
from services import db

logger = logging.getLogger(__name__)

__all__ = ["post_to_actuals", "AllocationValidationError"]


# Sum tolerance for allocation totals — accounts for rounding in % → € conversion.
_ALLOCATION_TOLERANCE_EUR = 0.05


class AllocationValidationError(ValueError):
    """Raised when an allocation set is malformed or doesn't reconcile.

    Caller is expected to surface .args[0] to the user (or call .reasons for
    the list of per-row issues).
    """

    def __init__(self, message: str, reasons: Optional[list] = None) -> None:
        super().__init__(message)
        self.reasons: list = reasons or []


def _validate_allocations(allocations: list, total_amount: float) -> None:
    """Sanity-check allocation rows before posting (FIO retro Top-10 fix #4).

    Rules:
    1. Every row MUST carry either `amount` (€) or `percentage` (% of total)
       AND a non-empty `ledger_code`. A row with neither numeric field is
       silent data loss waiting to happen.
    2. If ALL rows use `percentage`, sum must be 100 (±0.5%).
    3. If ALL rows use `amount`, sum must equal total_amount (±0.05€).
    4. Mixed mode (some %, some €) is rejected — too ambiguous to reconcile.
    """
    if not allocations:
        return
    reasons: list = []
    have_pct = 0
    have_amt = 0
    pct_sum = 0.0
    amt_sum = 0.0
    for idx, row in enumerate(allocations, start=1):
        if not row.get("ledger_code"):
            reasons.append(f"row {idx}: missing ledger_code")
            continue
        pct = row.get("percentage")
        amt = row.get("amount")
        if pct is None and amt is None:
            reasons.append(f"row {idx}: needs either `amount` or `percentage`")
            continue
        if pct is not None and amt is not None:
            # Both fields set — caller likely synced them, accept and prefer amount
            try:
                amt_sum += float(amt)
                have_amt += 1
            except (TypeError, ValueError):
                reasons.append(f"row {idx}: amount is not a number")
            continue
        if pct is not None:
            try:
                pct_sum += float(pct)
                have_pct += 1
            except (TypeError, ValueError):
                reasons.append(f"row {idx}: percentage is not a number")
        elif amt is not None:
            try:
                amt_sum += float(amt)
                have_amt += 1
            except (TypeError, ValueError):
                reasons.append(f"row {idx}: amount is not a number")

    if have_pct and have_amt and not all(
        r.get("amount") is not None and r.get("percentage") is not None
        for r in allocations
    ):
        reasons.append(
            "mixed percentage/amount allocations — use one mode for all rows"
        )

    if have_pct and not have_amt:
        if abs(pct_sum - 100.0) > 0.5:
            reasons.append(
                f"percentage allocations sum to {pct_sum:.2f}%, expected 100%"
            )
    elif have_amt and not have_pct and total_amount:
        if abs(amt_sum - float(total_amount)) > _ALLOCATION_TOLERANCE_EUR:
            reasons.append(
                f"amount allocations sum to €{amt_sum:.2f}, "
                f"document total is €{float(total_amount):.2f}"
            )

    if reasons:
        raise AllocationValidationError(
            f"Allocation validation failed ({len(reasons)} issue(s))",
            reasons=reasons,
        )


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

    # Validate BEFORE touching the actuals file — partial writes are corrupt
    _validate_allocations(allocations, float(total_amount or 0))

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
            if row_amount is None:
                # Validator should have caught this, but belt-and-braces:
                raise AllocationValidationError(
                    f"Allocation row for PC={row_pc} code={row_code} "
                    "has no amount and no percentage"
                )
            row_amount = float(row_amount)
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
