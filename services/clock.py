"""Business-local clock helpers (2026-07-08, Phase 2).

Operators work in Riga (Europe/Riga, UTC+2/+3). Anchoring "this week" or
"today" on UTC meant that Monday 00:00–02:59 local time was still Sunday
in UTC, so week windows and the plan-vs-fact overlay shifted by a week
during those early-morning hours. Anchor on the business timezone.

Falls back to UTC if the tz database is unavailable (unlikely on Linux/Fly
but keeps the import safe).
"""
from __future__ import annotations

from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo
    _BUSINESS_TZ = ZoneInfo("Europe/Riga")
except Exception:  # noqa: BLE001 — no tzdata: degrade to UTC
    _BUSINESS_TZ = None

__all__ = ["business_today", "business_now"]


def business_now() -> datetime:
    """Timezone-aware 'now' in the business timezone (naive-UTC fallback)."""
    if _BUSINESS_TZ is not None:
        return datetime.now(_BUSINESS_TZ)
    return datetime.utcnow()


def business_today() -> date:
    """Calendar date in the business timezone — use for week/period anchoring."""
    return business_now().date()
