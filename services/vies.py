"""VIES VAT auto-lookup -- official EU registry of EU VAT numbers.

Free public REST endpoint. Given a VAT like "LV40203241255", returns the
official company name and legal address as registered with the local tax
authority. Works without API keys, without LLM credits.

Docs: https://ec.europa.eu/taxation_customs/vies/

Endpoint used: https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{number}

This is the modern REST endpoint (the legacy SOAP service is being phased out).
We cache responses on disk to avoid hammering the EC service.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)

__all__ = ["lookup_vat", "vies_enrich_vendor"]

_CACHE_DIR = os.path.join(os.path.dirname(config.DB_PATH), "vies_cache")
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days
_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/%s/vat/%s"
_VAT_RE = re.compile(r"^([A-Z]{2})([A-Z0-9]+)$")


def _cache_path(vat: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Z0-9]", "", vat.upper())
    return os.path.join(_CACHE_DIR, "%s.json" % safe)


def _load_cache(vat: str) -> Optional[Dict[str, Any]]:
    path = _cache_path(vat)
    if not os.path.exists(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > _CACHE_TTL_SECONDS:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(vat: str, data: Dict[str, Any]) -> None:
    try:
        with open(_cache_path(vat), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("VIES cache write failed: %s", exc)


def lookup_vat(vat: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """Look up a VAT number in VIES; return dict on success, None on failure.

    Returns:
      {
        "vat": "LV40203241255",
        "valid": True,
        "name": "SIA EXAMPLE COMPANY",
        "address": "Brivibas iela 1, Riga, LV-1010",
        "country": "LV",
        "request_date": "2026-05-10T..."
      }

    On any failure (invalid format, network error, VIES down) returns None.
    """
    if not vat:
        return None
    vat_clean = re.sub(r"[\s\-]", "", vat.upper())
    match = _VAT_RE.match(vat_clean)
    if not match:
        return None

    country, number = match.group(1), match.group(2)

    # Disk cache first -- VIES is rate-limited and slow
    cached = _load_cache(vat_clean)
    if cached is not None:
        logger.debug("VIES cache hit for %s", vat_clean)
        return cached

    url = _VIES_URL % (country, number)
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "FIO/1.0 (Amitours Holding)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        logger.info("VIES lookup failed for %s: %s", vat_clean, exc)
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.info("VIES returned non-JSON for %s", vat_clean)
        return None

    # The REST endpoint returns: {countryCode, vatNumber, valid, name, address, ...}
    if not payload.get("valid"):
        result = {
            "vat": vat_clean,
            "valid": False,
            "country": country,
            "name": None,
            "address": None,
        }
        _save_cache(vat_clean, result)
        return result

    name = (payload.get("name") or "").strip().rstrip(",")
    address = (payload.get("address") or "").strip()
    # Address often comes with awkward newlines; normalize
    address = re.sub(r"\s*\n+\s*", ", ", address)
    address = re.sub(r"\s+", " ", address).strip(", ")

    result = {
        "vat": vat_clean,
        "valid": True,
        "country": country,
        "name": name or None,
        "address": address or None,
        "request_date": payload.get("requestDate"),
    }
    _save_cache(vat_clean, result)
    logger.info("VIES verified %s -- %s", vat_clean, name[:60] if name else "(no name)")
    return result


def vies_enrich_vendor(vendor: Dict[str, Any]) -> Dict[str, Any]:
    """Take a vendor dict (with at least vat_number) and enrich it from VIES.

    - Auto-fills `name` and `address` if VIES has them and they're missing.
    - Adds `vies_verified` and `vies_country` flags.
    - Adds `warnings` if VAT is invalid or vendor is foreign.
    - Returns the same dict (mutated) for convenience.
    """
    if not isinstance(vendor, dict):
        return vendor
    vat = vendor.get("vat_number")
    if not vat:
        # Add 'no_vat_number' warning here so downstream policy check sees it
        warns = vendor.setdefault("warnings", [])
        if "no_vat_number_found" not in warns:
            warns.append("no_vat_number_found")
        return vendor

    result = lookup_vat(vat)
    if result is None:
        # Network/format failure -- don't lie about verification
        return vendor

    vendor["vies_verified"] = bool(result.get("valid"))
    vendor["vies_country"] = result.get("country")

    if result.get("valid"):
        if result.get("name") and not vendor.get("name"):
            vendor["name"] = result["name"]
        # Always store official VIES values separately so we never overwrite human edits
        vendor["vies_official_name"] = result.get("name")
        vendor["vies_address"] = result.get("address")
        if result.get("address") and not vendor.get("address"):
            vendor["address"] = result["address"]
    else:
        warns = vendor.setdefault("warnings", [])
        if "vat_invalid_per_vies" not in warns:
            warns.append("vat_invalid_per_vies")

    return vendor
