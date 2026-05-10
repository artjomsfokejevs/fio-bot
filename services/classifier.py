"""Hybrid classification engine: rule-based + LLM fallback."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

__all__ = ["classify_document", "add_rule", "check_expense_policy"]


def classify_document(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a parsed document into ledger codes with confidence scores.

    Uses a two-step approach:
    1. Check the rule engine (accounting_rules.json) for vendor/description matches.
    2. If no high-confidence match, fall back to Claude LLM classification.

    Args:
        parsed: The parsed document data from parser.parse_document().

    Returns:
        Dictionary with 'codes' (top-3 matches) and 'auto_post' flag.
    """
    vendor_raw = parsed.get("vendor", "") or ""
    vendor = vendor_raw.get("name", "") if isinstance(vendor_raw, dict) else str(vendor_raw)
    description = _build_description(parsed)

    # Step 1: Rule engine
    rule_matches = _match_rules(vendor, description)
    if rule_matches and rule_matches[0]["confidence"] >= config.CONFIDENCE_REVIEW:
        logger.info("Rule engine matched: %s", rule_matches[0])
        auto_post = rule_matches[0]["confidence"] >= config.CONFIDENCE_AUTO_POST
        return {"codes": rule_matches[:3], "auto_post": auto_post}

    # Step 2: LLM classification
    llm_result = _classify_with_llm(parsed)
    if llm_result:
        auto_post = (
            len(llm_result) > 0
            and llm_result[0]["confidence"] >= config.CONFIDENCE_AUTO_POST
        )
        return {"codes": llm_result[:3], "auto_post": auto_post}

    # Fallback: unknown
    return {
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
    logger.info("Added rule: vendor=%s -> code=%s", vendor, code)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_rules() -> Dict[str, Any]:
    """Load the rules JSON file."""
    try:
        with open(config.RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"rules": []}


def _load_ledger_schema() -> Dict[str, Any]:
    """Load the ledger schema JSON file."""
    try:
        with open(config.LEDGER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"codes": [], "profit_centers": []}


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


def check_expense_policy(parsed: Dict[str, Any], classification: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check if expense complies with corporate policy.

    Args:
        parsed: Parsed document data.
        classification: Classification result with codes.

    Returns:
        List of policy warning dictionaries with 'level' (green/yellow/red),
        'message', and 'policy' keys.
    """
    warnings: List[Dict[str, Any]] = []
    description = _build_description(parsed).lower()
    money = parsed.get("money", {})
    total = money.get("total_amount", 0.0) or 0.0

    matched_policy: Optional[str] = None

    for policy_name, policy in EXPENSE_POLICIES.items():
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

    policy = EXPENSE_POLICIES[matched_policy]

    if matched_policy == "business_dinner":
        max_total = policy.get("max_total", 200.0)
        max_pp = policy.get("max_per_person", 50.0)
        if total > max_total:
            warnings.append({
                "level": "red",
                "message": "Exceeds corporate limit (max %.0f EUR for business dinner)" % max_total,
                "policy": matched_policy,
            })
        elif total > max_pp:
            warnings.append({
                "level": "yellow",
                "message": "Requires attendee list (dinner > %.0f EUR/person)" % max_pp,
                "policy": matched_policy,
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
            })
        else:
            warnings.append({
                "level": "green",
                "message": "Within corporate policy",
                "policy": matched_policy,
            })

    return warnings
