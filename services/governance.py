"""Governance hard-controls for SOP-FIN-001 Procedure 4 (2026-07-09).

Three controls that make the approval procedure enforceable rather than
declarative:

  1. Invoice-splitting detection — a single obligation must not be broken
     into smaller invoices to stay under an approval tier. We aggregate
     same-vendor invoices in a rolling window and flag when the aggregate
     crosses a higher tier than the individual invoice.

  2. Two-person segregation of duties (SoD) — no single user may occupy
     two of {budget-validate, CEO-confirm, mark-paid} on the same invoice.
     Mode is configurable (off | warn | enforce) via the `sod_mode`
     setting; default 'warn' so an MVP team where one person wears several
     hats is surfaced + audit-logged without being blocked. Flip to
     'enforce' once staffing allows.

  3. Vendor bank-detail change control — the first time we see a vendor's
     IBAN we remember it; if a later invoice from the same vendor carries
     a different IBAN, the doc is flagged and payment is blocked until a
     human re-verifies (guards against vendor-impersonation / BEC fraud).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from services import db
from services import settings as _settings

__all__ = [
    "tier_for", "detect_split", "sod_conflict", "sod_mode",
    "vendor_key", "check_vendor_bank_change", "record_vendor_bank",
]

# ── Approval tiers (SOP-FIN-001 §4) ──────────────────────────────────────
# (upper_bound_exclusive_eur, tier_label). Last tier is open-ended.
_TIERS: List[Tuple[float, str]] = [
    (1000.0, "T1"),
    (10000.0, "T2"),
    (50000.0, "T3"),
    (float("inf"), "T4"),
]


def tier_for(amount_eur: float) -> str:
    a = abs(float(amount_eur or 0))
    for bound, label in _TIERS:
        if a < bound:
            return label
    return "T4"


# ── 1. Invoice-splitting detection ───────────────────────────────────────
def _doc_amount_eur(doc: Dict[str, Any]) -> float:
    # documents.amount is already EUR (parser FX-converts on ingest).
    try:
        return float(doc.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def detect_split(vendor: Optional[str], legal_entity: Optional[str],
                 amount_eur: float, *, window_days: int = 30,
                 exclude_doc_id: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate same-vendor invoices in the trailing `window_days` and
    decide whether the running total crosses a higher approval tier than
    this single invoice would.

    Returns:
      {
        split_suspected: bool,
        individual_tier, aggregate_tier,
        aggregate_eur, sibling_count, window_days, vendor
      }
    """
    out = {
        "split_suspected": False,
        "individual_tier": tier_for(amount_eur),
        "aggregate_tier": tier_for(amount_eur),
        "aggregate_eur": round(float(amount_eur or 0), 2),
        "sibling_count": 0,
        "window_days": window_days,
        "vendor": vendor,
    }
    v = (vendor or "").strip().lower()
    if not v:
        return out  # can't group without a vendor
    since = (datetime.utcnow() - timedelta(days=window_days)).isoformat()
    conn = db.get_connection()
    try:
        params: List[Any] = [v, since]
        le_clause = ""
        if legal_entity:
            le_clause = " AND COALESCE(legal_entity,'') = ?"
            params.append(legal_entity)
        excl = ""
        if exclude_doc_id:
            excl = " AND id != ?"
            params.append(exclude_doc_id)
        rows = conn.execute(
            "SELECT COALESCE(amount,0) AS amount FROM documents "
            "WHERE LOWER(TRIM(COALESCE(vendor,''))) = ? "
            "AND COALESCE(uploaded_at,'') >= ? "
            # only invoices still in-flight or committed count toward the
            # obligation (skip rejected)
            "AND status NOT IN ('rejected')" + le_clause + excl,
            params,
        ).fetchall()
    finally:
        conn.close()
    siblings_total = sum(float(r["amount"] or 0) for r in rows)
    aggregate = round(float(amount_eur or 0) + siblings_total, 2)
    out["aggregate_eur"] = aggregate
    out["sibling_count"] = len(rows)
    out["aggregate_tier"] = tier_for(aggregate)
    # Suspected when the aggregate lands in a strictly higher tier AND
    # there is at least one sibling (a lone big invoice is not a split).
    tier_rank = {"T1": 1, "T2": 2, "T3": 3, "T4": 4}
    if rows and tier_rank[out["aggregate_tier"]] > tier_rank[out["individual_tier"]]:
        out["split_suspected"] = True
    return out


# ── 2. Two-person segregation of duties ──────────────────────────────────
def sod_mode() -> str:
    """'off' | 'warn' | 'enforce' (default 'warn')."""
    m = (_settings.get("sod_mode", "warn") or "warn").strip().lower()
    return m if m in ("off", "warn", "enforce") else "warn"


# Which prior-stage actor fields conflict with each action.
_SOD_PRIOR = {
    "confirm_payment": ["budget_validated_by"],
    "mark_paid": ["budget_validated_by", "confirmed_to_pay_by"],
}


def sod_conflict(doc: Dict[str, Any], action: str, actor: Optional[str]) -> Optional[str]:
    """Return the name of the conflicting prior stage if `actor` already
    performed one of the incompatible earlier stages on this doc, else None.
    Case-insensitive name compare.
    """
    a = (actor or "").strip().lower()
    if not a:
        return None
    for field in _SOD_PRIOR.get(action, []):
        prior = (doc.get(field) or "").strip().lower()
        if prior and prior == a:
            return field
    return None


# ── 3. Vendor bank-detail change control ─────────────────────────────────
def vendor_key(doc: Dict[str, Any]) -> Optional[str]:
    """Stable key for a vendor: prefer VAT number, else normalised name."""
    parsed = doc.get("parsed_json")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            parsed = {}
    parsed = parsed or {}
    vend = parsed.get("vendor") or {}
    vat = (vend.get("vat_number") or "").strip().upper() if isinstance(vend, dict) else ""
    if vat:
        return "vat:" + re.sub(r"[^A-Z0-9]", "", vat)
    name = (doc.get("vendor") or "").strip().lower()
    return ("name:" + name) if name else None


def _doc_iban(doc: Dict[str, Any]) -> Optional[str]:
    parsed = doc.get("parsed_json")
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            parsed = {}
    parsed = parsed or {}
    vend = parsed.get("vendor") or {}
    iban = ""
    if isinstance(vend, dict):
        iban = (vend.get("iban") or "").strip()
    if not iban:
        iban = (parsed.get("iban") or "").strip()
    return re.sub(r"\s+", "", iban).upper() or None


def record_vendor_bank(doc: Dict[str, Any], *, by: Optional[str] = None) -> None:
    """Remember this vendor's IBAN (first-seen wins; updates last_seen).
    Idempotent — safe to call on every parse."""
    key = vendor_key(doc)
    iban = _doc_iban(doc)
    if not key or not iban:
        return
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT iban FROM vendor_bank_details WHERE vendor_key = ? AND iban = ?",
            (key, iban),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE vendor_bank_details SET last_seen = ? WHERE vendor_key = ? AND iban = ?",
                (now, key, iban),
            )
        else:
            conn.execute(
                "INSERT INTO vendor_bank_details "
                "(vendor_key, iban, first_seen, last_seen, first_doc_id, recorded_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, iban, now, now, doc.get("id"), by),
            )
        conn.commit()
    finally:
        conn.close()


def check_vendor_bank_change(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Compare this doc's IBAN against the vendor's known IBAN(s).

    Returns {changed, current_iban, known_ibans}. `changed` is True when
    the vendor has a prior IBAN on record and this doc's IBAN is not among
    them — i.e. the bank details changed since we first paid this vendor.
    """
    out = {"changed": False, "current_iban": None, "known_ibans": []}
    key = vendor_key(doc)
    iban = _doc_iban(doc)
    out["current_iban"] = iban
    if not key or not iban:
        return out
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT iban FROM vendor_bank_details WHERE vendor_key = ?", (key,)
        ).fetchall()
    finally:
        conn.close()
    known = [r["iban"] for r in rows]
    out["known_ibans"] = known
    if known and iban not in known:
        out["changed"] = True
    return out
