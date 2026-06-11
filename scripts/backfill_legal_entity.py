#!/usr/bin/env python3
"""FIO — backfill documents.legal_entity from parser output.

The 2026-06-09 Top-10 P2.1 fix taught the parser prompt to identify the
bill-to recipient and map to one of the 9 entity codes. But existing
documents in the queue were parsed BEFORE that change — their
parsed_json may already contain `our_entity` (if a later re-parse hit
them) or may not. This script:

  1. Finds all approved/posted/classified/budget_validated/paid/
     confirmed_to_pay docs with NULL legal_entity.
  2. For each, reads parsed_json. If `our_entity` is present + maps to a
     known code, writes it to documents.legal_entity (+ marks the source
     in classification_json so the UI can show an "🤖 AI" badge).
  3. For docs where parsed_json doesn't have `our_entity`, applies a
     heuristic on vendor.bill_to text if present, else leaves NULL.
  4. Idempotent — re-running does nothing on already-set rows.

Usage:
    python3 scripts/backfill_legal_entity.py            # dry-run, prints diff
    python3 scripts/backfill_legal_entity.py --apply    # actually writes

For prod: `flyctl ssh console -a fio-amitours -C "python3 scripts/backfill_legal_entity.py --apply"`

(Top-10 self-review fix, 2026-06-11.)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Allow running from project root OR from scripts/
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services import db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_legal_entity")

# Same 9 codes as data/legal_entities.json — keep in sync.
KNOWN_CODES = {
    "ALPS2ALPS_OU", "AMITOURS_LONDON", "LORA_TRANS", "DMS",
    "AMITOURS_GROUP_SA", "ALEXCURSION", "SINKOPA",
    "AMITOURS_HOLDING", "AMITOURS_CORP", "OTHER",
}

# Heuristic fallback: if parsed_json has no our_entity, scan vendor address
# / bill-to text for known entity name fragments. Last resort, can be wrong.
NAME_TO_CODE = [
    (("alps2alps", "a2a oü", "a2a ou"),        "ALPS2ALPS_OU"),
    (("amitours london", "amitours london ltd"),"AMITOURS_LONDON"),
    (("lora trans", "lora-trans"),             "LORA_TRANS"),
    (("amitours group sa", "amitours group s.a"), "AMITOURS_GROUP_SA"),
    (("alexcursion",),                         "ALEXCURSION"),
    (("sinkopa",),                             "SINKOPA"),
    (("amitours holding",),                    "AMITOURS_HOLDING"),
    (("amitours corp", "amitours inc"),        "AMITOURS_CORP"),
    # DMS is too short to use as a substring — require explicit token boundary
    # (handled inline below).
]


def _entity_from_parsed(parsed: Dict[str, Any]) -> Optional[str]:
    """Pull `our_entity` directly from parser output if it's a known code."""
    val = parsed.get("our_entity")
    if isinstance(val, str) and val.strip().upper() in KNOWN_CODES:
        return val.strip().upper()
    return None


def _entity_from_heuristic(parsed: Dict[str, Any]) -> Optional[str]:
    """Last-resort: search the bill-to / customer text for known names."""
    # Try several locations
    candidates: list = []
    for key in ("bill_to", "billed_to", "customer", "buyer", "recipient"):
        v = parsed.get(key)
        if isinstance(v, dict):
            for sub in v.values():
                if isinstance(sub, str):
                    candidates.append(sub)
        elif isinstance(v, str):
            candidates.append(v)
    # Concatenate + lower-case for a single needle
    haystack = " ".join(candidates).lower()
    if not haystack:
        return None
    for needles, code in NAME_TO_CODE:
        if any(n in haystack for n in needles):
            return code
    # DMS — require token boundary, otherwise it matches "dms" inside random words
    if " dms " in (" " + haystack + " ") or "dms sia" in haystack:
        return "DMS"
    return None


def backfill_one(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return {legal_entity, source} or None if nothing inferable.

    `source` is "parser" or "heuristic". Used by the UI to badge AI-vs-manual.
    """
    raw = doc.get("parsed_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return None

    code = _entity_from_parsed(parsed)
    if code:
        return {"legal_entity": code, "source": "parser"}
    code = _entity_from_heuristic(parsed)
    if code:
        return {"legal_entity": code, "source": "heuristic"}
    return None


def run(apply: bool = False) -> int:
    db.init_db()
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, vendor, status, parsed_json, classification_json, legal_entity "
            "FROM documents "
            "WHERE legal_entity IS NULL "
            "AND status IN ('approved','posted','classified','budget_validated',"
            "               'confirmed_to_pay','paid')"
        ).fetchall()
        docs = [db._row_to_dict(r) for r in rows]
        log.info("Found %d docs needing backfill", len(docs))

        updates: list = []
        for d in docs:
            result = backfill_one(d)
            if result:
                updates.append((d, result))

        log.info("Inferred legal_entity for %d / %d docs", len(updates), len(docs))
        for d, r in updates:
            log.info("  %s · %s → %s (%s)",
                     d["id"][:8], (d.get("vendor") or "?")[:40],
                     r["legal_entity"], r["source"])

        if not apply:
            log.info("Dry-run — no writes. Pass --apply to commit.")
            return 0

        for d, r in updates:
            # Stamp source on classification_json so UI can show 🤖 AI badge
            cls_raw = d.get("classification_json")
            if isinstance(cls_raw, str):
                try:
                    cls = json.loads(cls_raw or "{}")
                except json.JSONDecodeError:
                    cls = {}
            elif isinstance(cls_raw, dict):
                cls = cls_raw
            else:
                cls = {}
            cls["legal_entity_source"] = r["source"]
            db.update_document(d["id"], {
                "legal_entity": r["legal_entity"],
                "classification_json": json.dumps(cls, default=str),
            })
        log.info("Wrote %d updates.", len(updates))
        return len(updates)
    finally:
        conn.close()


def main(argv: list) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="Commit changes. Without this flag, dry-run only.")
    args = p.parse_args(argv[1:])
    rc = run(apply=args.apply)
    return 0 if rc >= 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
