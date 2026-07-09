"""FX conversion service — ECB reference rates via Frankfurter API.

For every invoice in a non-EUR currency we compute the EUR-equivalent on the
document_date using ECB's published daily reference rate. Results are cached
on disk (per date) to stay polite to the public API and to keep the parser
deterministic on retries.

Returns NEVER raise — fall back gracefully to original amount + warning so
that approval flow still works when the API is offline.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from datetime import datetime, date, timedelta
from typing import Dict, Optional, Tuple

import config

logger = logging.getLogger(__name__)

__all__ = ["convert_to_eur", "get_rate"]

_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "fx_rates_cache.json")
_FRANKFURTER = "https://api.frankfurter.dev/v1"   # ECB-sourced, no auth, free
_TIMEOUT = 6.0

# Stable fallback rates (last ECB publication 2026-05) — used only when API is unreachable.
_FALLBACK = {
    "USD": 1.08,   # 1 EUR = 1.08 USD → invert: 1 USD = 0.926 EUR
    "GBP": 0.85,
    "CHF": 0.94,
    "AMD": 420.0,  # 1 EUR = 420 AMD → invert: 1 AMD = 0.00238 EUR
    "RUB": 100.0,
    "PLN": 4.30,
    "CZK": 24.5,
    "DKK": 7.45,
    "SEK": 11.45,
    "NOK": 11.85,
    "RON": 4.98,
    "BGN": 1.96,
    "HUF": 395.0,
    "TRY": 38.0,
    "GEL": 2.95,
    "UAH": 45.0,
}


def _load_cache() -> Dict[str, Dict[str, float]]:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(c: Dict[str, Dict[str, float]]) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2)
    except Exception as exc:
        logger.warning("Could not persist fx cache: %s", exc)


def _previous_business_day(d: date) -> date:
    """ECB does not publish on weekends. Walk back to nearest weekday."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def get_rate(from_currency: str, on_date: Optional[str] = None) -> Tuple[Optional[float], str, str]:
    """Get 1 unit of `from_currency` in EUR on `on_date` (YYYY-MM-DD).

    Returns (rate_eur_per_unit, effective_date, source).
    source: 'ecb' | 'ecb-cache' | 'fallback' | 'identity' | 'unavailable'
    rate is None when source is 'unavailable' (no live rate, no static
    fallback) -- callers must treat this as needs-manual-FX, never as 1.0.
    """
    fc = (from_currency or "EUR").upper().strip()
    if fc in ("EUR", "€", ""):
        return (1.0, on_date or datetime.utcnow().strftime("%Y-%m-%d"), "identity")

    # Normalise date
    if on_date:
        try:
            target = datetime.strptime(on_date[:10], "%Y-%m-%d").date()
        except ValueError:
            target = datetime.utcnow().date()
    else:
        target = datetime.utcnow().date()

    # Don't query future dates
    today = datetime.utcnow().date()
    if target > today:
        target = today
    target = _previous_business_day(target)
    eff_date = target.strftime("%Y-%m-%d")

    cache = _load_cache()
    cached_for_date = cache.get(eff_date, {})
    if fc in cached_for_date:
        return (float(cached_for_date[fc]), eff_date, "ecb-cache")

    # Live fetch: GET /{date}?base=EUR&symbols={fc}
    # Frankfurter returns "rates": {fc: <fc-per-1-eur>} — we invert.
    url = f"{_FRANKFURTER}/{eff_date}?base=EUR&symbols={fc}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FIO-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
        per_eur = float(payload.get("rates", {}).get(fc, 0.0))
        if per_eur > 0:
            rate_eur_per_unit = 1.0 / per_eur
            cache.setdefault(eff_date, {})[fc] = rate_eur_per_unit
            _save_cache(cache)
            return (rate_eur_per_unit, eff_date, "ecb")
    except Exception as exc:
        # Some non-G20 currencies (AMD, GEL, UAH, etc.) are not on ECB list — expected
        if fc in _FALLBACK:
            logger.debug("Frankfurter FX not available for %s (using static fallback): %s", fc, exc)
        else:
            logger.warning("Frankfurter FX fetch failed for %s @ %s: %s", fc, eff_date, exc)

    # Fallback
    per_eur_fb = _FALLBACK.get(fc)
    if per_eur_fb:
        return (1.0 / per_eur_fb, eff_date, "fallback")
    # 2026-07-08 (C7) — no live rate AND no static fallback. The old code
    # returned 1.0, which booked a 10,000 AED invoice as €10,000 (~4x
    # real) with only a log line. Return rate=None so convert_to_eur can
    # flag the doc as needs-manual-FX instead of silently 1:1-ing it.
    logger.error("No FX rate available for %s — flagging for manual entry", fc)
    return (None, eff_date, "unavailable")


def convert_to_eur(
    amount: Optional[float],
    from_currency: str,
    on_date: Optional[str] = None,
) -> Dict[str, object]:
    """Convert any amount to EUR. Returns extended money dict.

    Output keys:
      amount_orig, currency_orig   — what was on the invoice
      amount_eur, currency_eur     — EUR equivalent (always EUR)
      fx_rate                      — EUR per 1 unit of currency_orig
      fx_date                      — ECB reference date used
      fx_source                    — 'ecb' / 'ecb-cache' / 'fallback' / 'identity'
      fx_warning                   — optional human-readable hint (only when fallback)
    """
    if amount is None:
        return {
            "amount_orig": None, "currency_orig": (from_currency or "EUR").upper(),
            "amount_eur": None, "currency_eur": "EUR",
            "fx_rate": None, "fx_date": None, "fx_source": None,
        }
    rate, eff_date, source = get_rate(from_currency, on_date)
    # 2026-07-08 (C7) — no rate available for this currency: do NOT invent
    # a 1:1 EUR amount. Leave amount_eur=None + needs_manual_fx so the
    # upload pipeline / UI blocks the doc until an operator enters a rate.
    if rate is None:
        return {
            "amount_orig": round(float(amount), 2),
            "currency_orig": (from_currency or "EUR").upper(),
            "amount_eur": None,
            "currency_eur": "EUR",
            "fx_rate": None,
            "fx_date": eff_date,
            "fx_source": "unavailable",
            "needs_manual_fx": True,
            "fx_warning": (
                "No FX rate available for %s — EUR amount must be entered "
                "manually before this document can be posted."
                % (from_currency or "").upper()
            ),
        }
    amount_eur = round(float(amount) * rate, 2)
    out: Dict[str, object] = {
        "amount_orig": round(float(amount), 2),
        "currency_orig": (from_currency or "EUR").upper(),
        "amount_eur": amount_eur,
        "currency_eur": "EUR",
        "fx_rate": round(rate, 6),
        "fx_date": eff_date,
        "fx_source": source,
    }
    if source == "fallback":
        out["fx_warning"] = (
            "Used static fallback FX rate (ECB live API unreachable) — "
            "please verify EUR amount manually."
        )
    return out
