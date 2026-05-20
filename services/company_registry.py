"""Multi-source company registry — VIES for EU, OpenCorporates for non-EU fallback.

Why this exists:
- EU VIES only validates EU VAT numbers. For an Armenian invoice (AM…),
  Georgian (GE…), Turkish (TR…), Russian (RU…), Swiss (CH…), UK (GB...
  post-Brexit) — VIES returns 404 or "country not supported".
- Testers need confirmation that a non-EU vendor is a real registered entity
  before posting an expense to actuals.

Pipeline:
  1. Detect country (from VAT prefix, or vendor.country, or address heuristics).
  2. If country IN EU 27 → call VIES (existing services/vies.py).
  3. ELSE → call OpenCorporates search (jurisdiction-scoped) for company-by-name
     OR by tax_id. Returns up to top-3 matches.
  4. As a last resort: format-validate the tax-id pattern per country and
     return {"source": "format_only", "verified": False} with a structured
     warning so the UI surfaces "verify manually".

API choice — OpenCorporates:
  - Public, free tier 50 req/month without key; with free key — 500/month.
  - Coverage: 220M+ companies in 130+ jurisdictions including AM, GE, RU, UA, TR.
  - Endpoint: https://api.opencorporates.com/v0.4/companies/search?q=...
  - Response cached on disk (7 days) to avoid burning quota.

Set OPENCORPORATES_API_KEY env-var to lift free-tier limits.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

__all__ = ["lookup_company", "detect_country", "registry_enrich_vendor"]

_CACHE_DIR = os.path.join(os.path.dirname(config.DB_PATH), "registry_cache")
_CACHE_TTL = 7 * 24 * 3600  # 7 days
_OC_BASE = "https://api.opencorporates.com/v0.4"
_OC_KEY = os.getenv("OPENCORPORATES_API_KEY", "").strip()
_TIMEOUT = 8.0

# ISO-3166-2 (lowercased) of EU-27 + EFTA/UK aliases used by VIES
_EU_VIES_COUNTRIES = {
    "at","be","bg","cy","cz","de","dk","ee","el","es","fi","fr","gr","hr",
    "hu","ie","it","lt","lu","lv","mt","nl","pl","pt","ro","se","si","sk",
    # XI = Northern Ireland post-Brexit, still in VIES
    "xi",
}

# Non-EU tax-id format patterns we can at least format-validate
_NON_EU_TAX_PATTERNS = {
    "am": (r"\b\d{8,10}\b", "Armenia HVHH (8-10 digits)"),
    "ge": (r"\b\d{9,11}\b", "Georgia VAT (9-11 digits)"),
    "ru": (r"\b\d{10,12}\b", "Russia INN (10 or 12 digits)"),
    "ua": (r"\b\d{8,10}\b", "Ukraine EDRPOU (8-10 digits)"),
    "tr": (r"\b\d{10}\b",   "Turkey Vergi Numarası (10 digits)"),
    "ch": (r"\bCHE-?\d{3}\.\d{3}\.\d{3}\b", "Switzerland UID (CHE-xxx.xxx.xxx)"),
    "gb": (r"\bGB?\d{9,12}\b", "UK VAT (9 or 12 digits with optional GB prefix)"),
    "us": (r"\b\d{2}-?\d{7}\b", "USA EIN (xx-xxxxxxx)"),
    "ae": (r"\b\d{15}\b", "UAE TRN (15 digits)"),
}

# Country guess heuristics from address text
_ADDRESS_TO_COUNTRY = (
    ("armenia", "am"), ("yerevan", "am"), ("ереван", "am"), ("армения", "am"),
    ("georgia", "ge"), ("tbilisi", "ge"), ("тбилиси", "ge"),
    ("russia", "ru"), ("moscow", "ru"), ("москва", "ru"), ("россия", "ru"),
    ("ukraine", "ua"), ("kyiv", "ua"), ("kiev", "ua"), ("украина", "ua"),
    ("turkey", "tr"), ("istanbul", "tr"), ("turkiye", "tr"),
    ("switzerland", "ch"), ("geneva", "ch"), ("zurich", "ch"),
    ("united kingdom", "gb"), ("london", "gb"), ("england", "gb"),
    ("united states", "us"), ("new york", "us"), ("usa", "us"),
    ("uae", "ae"), ("dubai", "ae"), ("abu dhabi", "ae"),
)


# ─────────────────────────────────────────────────────────────────
# Cache plumbing
# ─────────────────────────────────────────────────────────────────

def _cache_path(key: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", key)
    return os.path.join(_CACHE_DIR, f"{safe}.json")


def _cache_read(key: str) -> Optional[Dict[str, Any]]:
    p = _cache_path(key)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("_cached_at", 0) > _CACHE_TTL:
            return None
        return data
    except Exception:
        return None


def _cache_write(key: str, data: Dict[str, Any]) -> None:
    try:
        data["_cached_at"] = time.time()
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.debug("registry cache write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────
# Country detection
# ─────────────────────────────────────────────────────────────────

def detect_country(vendor: Optional[Dict[str, Any]], tax_id: Optional[str] = None) -> Optional[str]:
    """Best-effort 2-letter ISO country code from vendor dict + raw tax-id.

    Priority:
      1. Explicit vendor.country (parser sometimes fills it)
      2. Tax-id prefix (LV40203... → lv, GE12345... → ge)
      3. Address keyword (Yerevan → am, Tbilisi → ge)
    """
    if vendor and isinstance(vendor, dict):
        c = (vendor.get("country") or "").strip().lower()
        if c and len(c) == 2:
            return c

    # 2. From tax_id prefix
    if tax_id:
        m = re.match(r"^([A-Z]{2})", tax_id.upper().strip())
        if m:
            return m.group(1).lower()

    # 3. From address heuristics
    addr = ""
    if vendor and isinstance(vendor, dict):
        addr = (vendor.get("address") or "").lower()
    for kw, country in _ADDRESS_TO_COUNTRY:
        if kw in addr:
            return country
    return None


# ─────────────────────────────────────────────────────────────────
# OpenCorporates (non-EU fallback)
# ─────────────────────────────────────────────────────────────────

def _oc_search(query: str, jurisdiction: Optional[str] = None) -> List[Dict[str, Any]]:
    """Search OpenCorporates by name (or tax-id). Returns top matches."""
    cache_key = f"oc__{jurisdiction or 'any'}__{query}"
    cached = _cache_read(cache_key)
    if cached and "results" in cached:
        return cached["results"]

    params = {"q": query, "per_page": "5"}
    if jurisdiction:
        params["jurisdiction_code"] = jurisdiction
    if _OC_KEY:
        params["api_token"] = _OC_KEY

    url = f"{_OC_BASE}/companies/search?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FIO-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        companies = (data.get("results", {}) or {}).get("companies", [])
        results = []
        for entry in companies:
            comp = entry.get("company", {}) or {}
            results.append({
                "name": comp.get("name"),
                "company_number": comp.get("company_number"),
                "jurisdiction": comp.get("jurisdiction_code"),
                "incorporation_date": comp.get("incorporation_date"),
                "company_type": comp.get("company_type"),
                "registry_url": comp.get("registry_url"),
                "opencorporates_url": comp.get("opencorporates_url"),
            })
        _cache_write(cache_key, {"results": results})
        return results
    except urllib.error.HTTPError as e:
        if e.code == 401:
            logger.warning("OpenCorporates 401 — set OPENCORPORATES_API_KEY to lift free-tier limits")
        elif e.code == 429:
            logger.warning("OpenCorporates rate limit — try again later or upgrade plan")
        else:
            logger.warning("OpenCorporates HTTP %s: %s", e.code, e.reason)
        return []
    except Exception as exc:
        logger.warning("OpenCorporates fetch failed: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def lookup_company(
    vendor: Optional[Dict[str, Any]] = None,
    vat_or_tax_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Try to verify a company exists in an authoritative registry.

    Returns dict with at minimum:
      source: 'vies' | 'opencorporates' | 'format_only' | 'none'
      verified: bool
      country: str (2-letter) or None
      official_name: str or None
      matches: list of {name, jurisdiction, company_number, registry_url}
      warnings: list[str]
    """
    out: Dict[str, Any] = {
        "source": "none",
        "verified": False,
        "country": None,
        "official_name": None,
        "matches": [],
        "warnings": [],
    }

    country = detect_country(vendor, vat_or_tax_id)
    out["country"] = country
    name = (vendor or {}).get("name") if isinstance(vendor, dict) else None

    # 1. EU → defer to VIES (kept in services/vies.py for backwards-compat)
    if country and country in _EU_VIES_COUNTRIES:
        try:
            from services import vies
            v = vies.lookup_vat(vat_or_tax_id) if vat_or_tax_id else None
            if v and v.get("valid"):
                out["source"] = "vies"
                out["verified"] = True
                out["official_name"] = v.get("name") or v.get("trader_name")
                out["matches"] = [{
                    "name": out["official_name"],
                    "jurisdiction": country,
                    "company_number": vat_or_tax_id,
                    "registry_url": "https://ec.europa.eu/taxation_customs/vies/",
                }]
                return out
            if v is not None:
                out["warnings"].append("VAT not found in VIES registry (EU official source)")
        except Exception as exc:
            logger.debug("VIES lookup failed: %s", exc)

    # 2. Non-EU → OpenCorporates
    if country and country not in _EU_VIES_COUNTRIES:
        # Prefer searching by company name (more accurate than tax-id which OC indexes inconsistently)
        results = []
        if name:
            results = _oc_search(name, jurisdiction=country)
        if not results and vat_or_tax_id:
            results = _oc_search(vat_or_tax_id, jurisdiction=country)
        if results:
            out["source"] = "opencorporates"
            out["verified"] = True
            out["matches"] = results[:3]
            out["official_name"] = results[0].get("name")
            return out

    # 3. Last resort: format validation
    if country and country in _NON_EU_TAX_PATTERNS and vat_or_tax_id:
        pattern, label = _NON_EU_TAX_PATTERNS[country]
        if re.search(pattern, vat_or_tax_id, re.IGNORECASE):
            out["source"] = "format_only"
            out["verified"] = False
            out["warnings"].append(
                f"Tax-ID format matches {label}, but no public registry available. "
                "Please verify manually before approving."
            )
            return out

    if country:
        out["warnings"].append(
            f"No registry available for country '{country.upper()}' — verify vendor manually."
        )
    else:
        out["warnings"].append("Could not detect country — vendor verification skipped.")
    return out


def registry_enrich_vendor(parsed: Any) -> Any:
    """Mutate parsed (dict or list) — add 'registry_check' to each vendor.

    Idempotent — skips if registry_check already present and verified=True.
    """
    if isinstance(parsed, list):
        return [registry_enrich_vendor(p) for p in parsed]
    if not isinstance(parsed, dict):
        return parsed
    vendor = parsed.get("vendor")
    if not isinstance(vendor, dict):
        return parsed
    if vendor.get("registry_check", {}).get("verified") is True:
        return parsed
    vat = vendor.get("vat_number") or vendor.get("tax_id") or vendor.get("registration_number")
    try:
        check = lookup_company(vendor, vat)
        vendor["registry_check"] = check
        # If we got a confident match, surface official name to top-level vendor
        if check.get("verified") and check.get("official_name") and not vendor.get("official_name"):
            vendor["official_name"] = check["official_name"]
    except Exception as exc:
        logger.warning("registry enrich failed: %s", exc)
        vendor["registry_check"] = {"source": "error", "verified": False, "warnings": [str(exc)]}
    parsed["vendor"] = vendor
    return parsed
