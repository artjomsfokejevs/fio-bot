"""Phase 3 of #94 — match incoming bank credits to revenue_documents.

For each card_transactions row with amount > 0 (inflow) in a batch, score
candidate `revenue_documents` (status in {sent, partially_paid}) by:

  +50  exact amount_eur match (within 0.5%)
  +30  amount partially covers remaining receivable
  +20  customer name token overlap with description/counterparty
  +20  invoice_number appears in description or reference
  +10  PC of revenue doc == PC inferred for transaction (card_holder map)
  +10  posted_at within ±14 days of issue_date

A candidate with score ≥ 60 becomes the suggested match; on operator
confirm we call `apply_match()` which inserts a revenue_receipt and
auto-transitions status.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from services import db
from services import revenue as _rev
from services import revenue_receipts as _rr
from services import pc_codes

logger = logging.getLogger(__name__)

__all__ = [
    "suggestions_for_tx",
    "suggestions_for_batch",
    "apply_match",
]


_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _tokens(s: Optional[str]) -> set:
    return set(_TOKEN_RE.findall((s or "").lower()))


def _open_docs(pc: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = db.get_connection()
    try:
        sql = ("SELECT * FROM revenue_documents "
               "WHERE status IN ('sent','partially_paid')")
        params: List[Any] = []
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            sql += " AND profit_center IN (" + ",".join("?" for _ in aliases) + ")"
            params.extend(aliases)
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def _score(tx: Dict[str, Any], doc: Dict[str, Any]) -> Dict[str, Any]:
    score = 0
    reasons: List[str] = []
    tx_amt = float(tx.get("amount_eur") or tx.get("amount") or 0)
    doc_amt = float(doc.get("amount_eur") or doc.get("amount") or 0)
    remaining = max(0.0, doc_amt - _rr.total_received(doc["id"]))

    if doc_amt > 0 and abs(tx_amt - doc_amt) <= max(0.5, 0.005 * doc_amt):
        score += 50
        reasons.append(f"amount=€{doc_amt:.2f}")
    elif remaining > 0 and 0 < tx_amt <= remaining + 0.5:
        score += 30
        reasons.append(f"partial €{tx_amt:.2f}/€{remaining:.2f}")

    tx_text = " ".join([tx.get("description") or "", tx.get("counterparty") or "",
                        tx.get("reference") or ""])
    if doc.get("invoice_number") and doc["invoice_number"].lower() in tx_text.lower():
        score += 20
        reasons.append(f"inv#{doc['invoice_number']}")

    if doc.get("customer"):
        overlap = _tokens(doc["customer"]) & _tokens(tx_text)
        if overlap:
            score += 20
            reasons.append("customer:" + ",".join(sorted(overlap))[:30])

    if (tx.get("profit_center") and doc.get("profit_center") and
            pc_codes.to_canonical(tx["profit_center"]) ==
            pc_codes.to_canonical(doc["profit_center"])):
        score += 10
        reasons.append("pc")

    try:
        if tx.get("posted_at") and doc.get("issue_date"):
            tx_d = datetime.fromisoformat(tx["posted_at"][:10])
            iss_d = datetime.fromisoformat(doc["issue_date"][:10])
            if abs((tx_d - iss_d).days) <= 14:
                score += 10
                reasons.append("date±14d")
    except (ValueError, TypeError):
        pass

    return {"doc_id": doc["id"], "doc": doc, "score": score, "reasons": reasons}


def suggestions_for_tx(tx: Dict[str, Any], top: int = 3) -> List[Dict[str, Any]]:
    """Return up to `top` candidate matches with score >= 30, sorted desc."""
    candidates = [_score(tx, d) for d in _open_docs()]
    candidates = [c for c in candidates if c["score"] >= 30]
    candidates.sort(key=lambda c: -c["score"])
    return candidates[:top]


def suggestions_for_batch(batch_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """For all inflow txs in a batch, return {tx_id: [suggestions...]}."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM card_transactions WHERE batch_id = ? "
            "AND amount > 0 AND match_status IN ('unmatched','suggested')",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        tx = dict(r)
        suggestions = suggestions_for_tx(tx)
        if suggestions:
            out[tx["id"]] = suggestions
    return out


def apply_match(tx_id: str, revenue_doc_id: str, *, by: Optional[str] = None,
                auto: bool = False) -> Dict[str, Any]:
    """Record a revenue_receipt for the matched tx and tag the tx itself.

    auto=True means this was triggered by the scheduler / scan, not by a
    human click — useful for audit reasons. Returns the new receipt.
    """
    conn = db.get_connection()
    try:
        tx_row = conn.execute(
            "SELECT * FROM card_transactions WHERE id = ?", (tx_id,)
        ).fetchone()
    finally:
        conn.close()
    if not tx_row:
        raise ValueError(f"tx {tx_id} not found")
    tx = dict(tx_row)

    # 2026-07-08 (H2) — double-apply guard. Without this, a double-click,
    # a retried request, or auto_match_batch racing a manual click would
    # insert TWO revenue_receipts for one bank credit — flipping the doc
    # to `paid` on phantom money. A tx that is already matched (auto or
    # manual) must not be matched again.
    if (tx.get("match_status") or "").lower() in ("matched", "auto", "manual"):
        raise ValueError(
            "transaction %s is already matched (status=%s)"
            % (tx_id, tx.get("match_status"))
        )
    amt = float(tx.get("amount_eur") or tx.get("amount") or 0)
    if amt <= 0:
        raise ValueError("transaction amount must be > 0 for revenue match")

    # Atomically claim the tx first (conditional UPDATE): if another
    # request matched it between our SELECT and here, rowcount is 0 and we
    # abort BEFORE writing a receipt.
    method = "auto" if auto else "manual"
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE card_transactions SET match_status = ?, "
            "match_confidence = 100, matched_invoice_id = ?, "
            "match_reason = ? WHERE id = ? "
            "AND (match_status IS NULL OR match_status NOT IN ('matched','auto','manual'))",
            (method, revenue_doc_id,
             "revenue match" + (" (auto)" if auto else ""),
             tx_id),
        )
        conn.commit()
        claimed = cur.rowcount > 0
    finally:
        conn.close()
    if not claimed:
        raise ValueError("transaction %s was matched concurrently" % tx_id)

    # tx is now claimed — safe to record the receipt exactly once.
    receipt = _rr.add_receipt(
        revenue_doc_id, amount_eur=amt,
        received_at=tx.get("posted_at"),
        method="bank_transfer",
        reference=tx.get("reference") or tx.get("description"),
        bank_statement_tx_id=tx_id,
        by=by or ("auto-match" if auto else None),
    )
    return receipt


def auto_match_batch(batch_id: str, *, min_score: int = 80,
                     by: Optional[str] = None) -> List[Dict[str, Any]]:
    """Scan a batch and auto-apply matches with score >= min_score."""
    applied: List[Dict[str, Any]] = []
    sugg = suggestions_for_batch(batch_id)
    for tx_id, candidates in sugg.items():
        if candidates and candidates[0]["score"] >= min_score:
            top = candidates[0]
            try:
                receipt = apply_match(tx_id, top["doc_id"], by=by, auto=True)
                applied.append({"tx_id": tx_id, "doc_id": top["doc_id"],
                                "score": top["score"], "receipt": receipt})
            except Exception as exc:
                logger.warning("auto-match failed for tx=%s doc=%s: %s",
                               tx_id, top["doc_id"], exc)
    return applied
