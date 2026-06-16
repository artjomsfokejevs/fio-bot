"""Hybrid classification engine: rule-based + LLM fallback."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

__all__ = ["classify_document", "classify_line_items", "add_rule", "check_expense_policy"]


# category_hint (from parser prompt) → default ledger code mapping
# Used as a fast hint, LLM may override with more specific code.
_HINT_TO_CODE = {
    "marketing": ("MGG0", "Marketing — Google Ads / paid acquisition"),
    "travel":    ("BT00", "Business trips"),
    "food":      ("REO0", "Representative expenses (meals)"),
    "office":    ("RNT0", "Rent & utilities / office supplies"),
    "subscription": ("SWS1", "Software subscriptions"),
    "consulting": ("CNO0", "Consulting — Other"),
    "other":     ("OTH0", "Grants, subsidies, misc"),
}


def classify_document(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a parsed document into ledger codes with confidence scores.

    Uses a two-step approach:
    1. Check the rule engine (accounting_rules.json) for vendor/description matches.
    2. If no high-confidence match, fall back to Claude LLM classification.

    Also computes per_line classification when the invoice has > 1 line items
    with different category_hints (Phase 2.5 / multi-ledger split).

    Args:
        parsed: The parsed document data from parser.parse_document().

    Returns:
        Dictionary with 'codes' (top-3 matches), 'auto_post' flag, and
        'per_line' (list of {line_index, code, label, confidence, profit_center}).
    """
    vendor_raw = parsed.get("vendor", "") or ""
    vendor = vendor_raw.get("name", "") if isinstance(vendor_raw, dict) else str(vendor_raw)
    description = _build_description(parsed)

    # Step 1: Rule engine for the overall document
    rule_matches = _match_rules(vendor, description)
    if rule_matches and rule_matches[0]["confidence"] >= config.CONFIDENCE_REVIEW:
        logger.info("Rule engine matched: %s", rule_matches[0])
        auto_post = rule_matches[0]["confidence"] >= config.CONFIDENCE_AUTO_POST
        result = {"codes": rule_matches[:3], "auto_post": auto_post}
    else:
        # Step 2: LLM classification
        llm_result = _classify_with_llm(parsed)
        if llm_result:
            auto_post = (
                len(llm_result) > 0
                and llm_result[0]["confidence"] >= config.CONFIDENCE_AUTO_POST
            )
            result = {"codes": llm_result[:3], "auto_post": auto_post}
        else:
            # Fallback: unknown
            result = {
                "codes": [
                    {
                        "code": "OTH0",
                        "label": "Grants, subsidies, misc",
                        "confidence": 30,
                        "reasoning": "No rule or LLM match found",
                        "profit_center": None,
                    }
                ],
                "auto_post": False,
            }

    # Phase 2.5 — per-line classification when there are mixed categories
    result["per_line"] = classify_line_items(parsed, result["codes"][0] if result["codes"] else None)

    # Phase 2.2 — Dmitrijs bridge: surface profit_center to top-level result
    # (top-level write to DB is done by app.py — here we just ensure codes[0] has it)
    top = result["codes"][0] if result["codes"] else None
    if top and not top.get("profit_center"):
        # Try to harvest PC from per_line if any item has one
        for line in result["per_line"]:
            if line.get("profit_center"):
                top["profit_center"] = line["profit_center"]
                break

    return result


def classify_line_items(
    parsed: Dict[str, Any],
    document_default: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Suggest a ledger code per line item.

    If all line items share the same category → returns single-item list inheriting
    document_default. If items have DIFFERENT category_hints → returns per-line
    suggestions so the user can split the invoice across multiple ledger codes.

    Args:
        parsed: Parser output with line_items[].
        document_default: Top-level classification (from classify_document).

    Returns:
        List of {line_index, description, amount, code, label, confidence,
        profit_center, category_hint, source}.
    """
    line_items = parsed.get("line_items") or []
    if not line_items:
        return []

    suggestions: List[Dict[str, Any]] = []
    seen_hints: set = set()
    for i, line in enumerate(line_items):
        if not isinstance(line, dict):
            continue
        hint = (line.get("category_hint") or "").lower().strip()
        seen_hints.add(hint or "other")

        if hint and hint in _HINT_TO_CODE:
            code, label = _HINT_TO_CODE[hint]
            suggestions.append({
                "line_index": i,
                "description": line.get("description") or line.get("description_en") or "",
                "amount": line.get("amount", 0),
                "code": code,
                "label": label,
                "confidence": 70,
                "profit_center": document_default.get("profit_center") if document_default else None,
                "category_hint": hint,
                "source": "category_hint",
                "reasoning": f"Auto-mapped from category hint '{hint}'",
            })
        else:
            # Fall back to document-level code if available
            code = document_default.get("code") if document_default else "OTH0"
            label = document_default.get("label") if document_default else "Misc"
            suggestions.append({
                "line_index": i,
                "description": line.get("description") or line.get("description_en") or "",
                "amount": line.get("amount", 0),
                "code": code,
                "label": label,
                "confidence": document_default.get("confidence", 50) if document_default else 30,
                "profit_center": document_default.get("profit_center") if document_default else None,
                "category_hint": hint or None,
                "source": "document_default",
                "reasoning": "Inherited from document classification",
            })

    # If all line items share single category → return single-element list (no split needed)
    if len(seen_hints) <= 1:
        return suggestions[:1] if suggestions else []
    return suggestions


def add_rule(vendor: str, code: str, profit_center: Optional[str] = None) -> None:
    """Add a new classification rule to accounting_rules.json.

    Args:
        vendor: Vendor name substring to match.
        code: Ledger code to assign.
        profit_center: Optional profit center code.
    """
    rules_data = _load_rules()
    new_rule: Dict[str, Any] = {
        "vendor_contains": vendor,
        "ledger_code": code,
        "confidence": 95,
    }
    if profit_center:
        new_rule["profit_center"] = profit_center

    # Check for duplicate
    for existing in rules_data["rules"]:
        if (
            existing.get("vendor_contains", "").lower() == vendor.lower()
            and existing.get("ledger_code") == code
        ):
            logger.info("Rule already exists for vendor=%s code=%s", vendor, code)
            return

    rules_data["rules"].append(new_rule)
    with open(config.RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rules_data, f, indent=2, ensure_ascii=False)
    _invalidate_json_cache(config.RULES_FILE)
    logger.info("Added rule: vendor=%s -> code=%s", vendor, code)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# mtime-based cache for hot JSON loaders. Without this, a batch import of 1000
# invoices hits the filesystem ~2000× (rules + schema per document). Re-reads
# only when the source file changes on disk. (FIO retro Top-10 fix #3, 2026-05-21)
_JSON_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _cached_json(path: str, default: Dict[str, Any]) -> Dict[str, Any]:
    """Read a JSON file with mtime-keyed in-memory cache.

    If the file's mtime hasn't changed since last load, returns the cached dict
    (returned by reference — callers MUST NOT mutate the result). On read error
    or missing file, returns `default` (also by reference). The cache entry is
    refreshed automatically when the file is rewritten (e.g. by add_rule).
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return default
    cached = _JSON_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    _JSON_CACHE[path] = (mtime, data)
    return data


def _invalidate_json_cache(path: str) -> None:
    """Drop the cached entry for a path. Call after rewriting the file."""
    _JSON_CACHE.pop(path, None)


def _load_rules() -> Dict[str, Any]:
    """Load the rules JSON file (cached, mtime-invalidated)."""
    return _cached_json(config.RULES_FILE, {"rules": []})


def _load_ledger_schema() -> Dict[str, Any]:
    """Load the ledger schema JSON file (cached, mtime-invalidated)."""
    return _cached_json(config.LEDGER_FILE, {"codes": [], "profit_centers": []})


def _get_label_for_code(code: str) -> str:
    """Look up the human label for a ledger code."""
    schema = _load_ledger_schema()
    for entry in schema.get("codes", []):
        if entry["code"] == code:
            return entry["label"]
    return code


def _build_description(parsed: Dict[str, Any]) -> str:
    """Build a searchable description string from parsed data."""
    parts: List[str] = []
    vendor = parsed.get("vendor", "")
    if vendor:
        if isinstance(vendor, dict):
            parts.append(vendor.get("name", ""))
        else:
            parts.append(str(vendor))
    for item in parsed.get("line_items", []):
        if item.get("description"):
            parts.append(item["description"])
    return " ".join(parts)


def _match_rules(vendor: str, description: str) -> List[Dict[str, Any]]:
    """Match vendor/description against the rule engine.

    Returns:
        List of matching codes sorted by confidence descending.
    """
    rules_data = _load_rules()
    matches: List[Dict[str, Any]] = []

    vendor_lower = vendor.lower()
    desc_lower = description.lower()

    for rule in rules_data.get("rules", []):
        vendor_match = rule.get("vendor_contains", "").lower()
        desc_match = rule.get("description_contains", "").lower() if rule.get("description_contains") else None

        if vendor_match and vendor_match not in vendor_lower and vendor_match not in desc_lower:
            continue

        if desc_match and desc_match not in desc_lower:
            continue

        code = rule["ledger_code"]
        matches.append({
            "code": code,
            "label": _get_label_for_code(code),
            "confidence": rule.get("confidence", 80),
            "reasoning": "Rule match: vendor_contains='%s'" % rule.get("vendor_contains", ""),
            "profit_center": rule.get("profit_center"),
        })

    matches.sort(key=lambda m: m["confidence"], reverse=True)
    return matches


def _classify_with_llm(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Use Claude to classify a document that didn't match any rules."""
    if not config.ANTHROPIC_API_KEY:
        logger.warning("No API key -- skipping LLM classification")
        return []

    try:
        import anthropic

        schema = _load_ledger_schema()
        codes_text = "\n".join(
            "  %s: %s (%s / %s)" % (c["code"], c["label"], c["statement"], c["group"])
            for c in schema.get("codes", [])
        )
        centers_text = "\n".join(
            "  %s: %s" % (pc["code"], pc["name"])
            for pc in schema.get("profit_centers", [])
        )

        prompt = (
            "You are a financial classifier for Amitours Holding. "
            "Given the following parsed document data, classify it into the most "
            "appropriate ledger code(s).\n\n"
            "LEDGER CODES:\n%s\n\n"
            "PROFIT CENTERS:\n%s\n\n"
            "DOCUMENT DATA:\n%s\n\n"
            "Return ONLY valid JSON (no markdown fences) with this structure:\n"
            "[\n"
            '  {"code": "MGG0", "label": "Google Ads", "confidence": 85, '
            '"reasoning": "...", "profit_center": "AA"}\n'
            "]\n\n"
            "Return up to 3 options sorted by confidence. "
            "Confidence should be 50-90 (never higher for LLM classification). "
            "profit_center can be null if unclear."
        ) % (codes_text, centers_text, json.dumps(parsed, default=str)[:3000])

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        import re
        text = response.content[0].text
        cleaned = re.sub(r"```json\s*", "", text)
        cleaned = re.sub(r"```\s*$", "", cleaned.strip())
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
        return []

    except Exception as exc:
        logger.exception("LLM classification failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Expense policy checker (Feature 7)
# ---------------------------------------------------------------------------

# Legacy EXPENSE_POLICIES kept as a hard fallback / public reference
# (some tests + downstream tools may import it). The runtime source of truth
# is now services.policy_rules.get_effective_policies(), which overlays
# DB-edited thresholds on top of these defaults and falls back to them when
# the DB has no rows (M81 graceful pattern). Added 2026-06-16 Phase 1 P1.2.
EXPENSE_POLICIES: Dict[str, Dict[str, Any]] = {
    "business_dinner": {
        "max_per_person": 50.0,
        "max_total": 200.0,
        "requires": "attendee_list",
        "category_keywords": ["restaurant", "dinner", "lunch", "cafe", "bar",
                              "bistro", "grill", "pizza", "sushi", "food"],
    },
    "business_travel": {
        "max_per_day": 150.0,
        "requires": "travel_order",
        "category_keywords": ["hotel", "flight", "taxi", "parking", "fuel",
                              "gas", "petrol", "toll", "train", "bus"],
    },
    "office_supplies": {
        "max_per_item": 500.0,
        "category_keywords": ["office", "supplies", "stationery", "equipment",
                              "printer", "paper", "toner", "desk", "chair"],
    },
}


def _resolve_policies() -> Dict[str, Dict[str, Any]]:
    """Load DB-overlaid policies; fall back to EXPENSE_POLICIES if anything
    fails (classifier must never break the upload pipeline)."""
    try:
        from services import policy_rules as _pr  # local import avoids cycle at module load
        return _pr.get_effective_policies()
    except Exception:  # noqa: BLE001 — be graceful, never block classify
        return EXPENSE_POLICIES


def _rule_code(policies: Dict[str, Dict[str, Any]], policy_name: str, field: str) -> Optional[str]:
    rules = (policies.get(policy_name) or {}).get("_rules") or {}
    meta = rules.get(field)
    return meta.get("code") if meta else None


def check_expense_policy(parsed: Dict[str, Any], classification: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check if expense complies with corporate policy.

    Args:
        parsed: Parsed document data.
        classification: Classification result with codes.

    Returns:
        List of policy warning dictionaries with 'level' (green/yellow/red),
        'message', 'policy' and (when DB-backed) 'rule_code' keys.
    """
    warnings: List[Dict[str, Any]] = []
    description = _build_description(parsed).lower()
    money = parsed.get("money", {})
    total = money.get("total_amount", 0.0) or 0.0

    policies = _resolve_policies()
    matched_policy: Optional[str] = None

    for policy_name, policy in policies.items():
        keywords = policy.get("category_keywords", [])
        if any(kw in description for kw in keywords):
            matched_policy = policy_name
            break

    if not matched_policy:
        warnings.append({
            "level": "green",
            "message": "Within corporate policy",
            "policy": "general",
        })
        return warnings

    policy = policies[matched_policy]

    if matched_policy == "business_dinner":
        max_total = policy.get("max_total", 200.0)
        max_pp = policy.get("max_per_person", 50.0)
        if total > max_total:
            warnings.append({
                "level": "red",
                "message": "Exceeds corporate limit (max %.0f EUR for business dinner)" % max_total,
                "policy": matched_policy,
                "rule_code": _rule_code(policies, matched_policy, "max_total"),
            })
        elif total > max_pp:
            warnings.append({
                "level": "yellow",
                "message": "Requires attendee list (dinner > %.0f EUR/person)" % max_pp,
                "policy": matched_policy,
                "rule_code": _rule_code(policies, matched_policy, "max_per_person"),
            })
        else:
            warnings.append({
                "level": "green",
                "message": "Within corporate policy",
                "policy": matched_policy,
            })

    elif matched_policy == "business_travel":
        max_per_day = policy.get("max_per_day", 150.0)
        if total > max_per_day:
            warnings.append({
                "level": "yellow",
                "message": "Requires travel order (travel expense > %.0f EUR/day)" % max_per_day,
                "policy": matched_policy,
                "rule_code": _rule_code(policies, matched_policy, "max_per_day"),
            })
        else:
            warnings.append({
                "level": "green",
                "message": "Within corporate policy",
                "policy": matched_policy,
            })

    elif matched_policy == "office_supplies":
        max_per_item = policy.get("max_per_item", 500.0)
        if total > max_per_item:
            warnings.append({
                "level": "red",
                "message": "Exceeds corporate limit (max %.0f EUR for office supplies)" % max_per_item,
                "policy": matched_policy,
                "rule_code": _rule_code(policies, matched_policy, "max_per_item"),
            })
        else:
            warnings.append({
                "level": "green",
                "message": "Within corporate policy",
                "policy": matched_policy,
            })

    return warnings
