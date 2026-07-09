"""Shared money-string parser — handles both US and EU number formats.

2026-07-08 (C8) — the old card_audit._parse_amount did
`s.replace(",", ".")`, which turned "1,500" (US thousands, no decimals)
into "1.500" → 1.5. A €1,500 card charge imported as €1.50 and then
matched nothing (or a wrong small invoice), silently under-reporting
month totals. This helper distinguishes the thousands separator from
the decimal point using the standard "last separator wins; 3 trailing
digits ⇒ thousands" heuristic, so it copes with:

    "1,234.56"  → 1234.56   (US)
    "1.234,56"  → 1234.56   (EU)
    "1,500"     → 1500.0    (US thousands, no decimals)
    "1.500"     → 1500.0    (EU thousands, no decimals)
    "1,50"      → 1.50      (EU decimal)
    "1500.00"   → 1500.0
    "(€1,234)"  → -1234.0   (parenthesised negative)
"""
from __future__ import annotations

import re
from typing import Any, Optional

_JUNK = ("-", "—", "–", "#REF!", "#N/A", "N/A", "")


def parse_money(raw: Any) -> Optional[float]:
    """Parse a money string into a float, or None if not a number.

    Never raises. Handles US and EU thousands/decimal conventions,
    currency symbols, spaces, and parenthesised negatives.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if s in _JUNK:
        return None

    # Parenthesised negative: (€1,234) → -1234
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    # Leading minus
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    # Strip everything except digits and the two separators.
    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        return None

    has_dot = "." in s
    has_comma = "," in s

    if has_dot and has_comma:
        # Both present: whichever appears LAST is the decimal separator.
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")            # comma = thousands
        else:
            s = s.replace(".", "").replace(",", ".")  # dot = thousands, comma = decimal
    elif has_comma:
        # Only commas. If the last group has exactly 3 digits treat comma
        # as a thousands separator (1,500 → 1500); otherwise decimal
        # (1,50 → 1.50, money is never 3 decimal places).
        last = s.split(",")[-1]
        if len(last) == 3 and s.count(",") == 1 and len(s.split(",")[0]) <= 3:
            s = s.replace(",", "")
        elif s.count(",") > 1:
            s = s.replace(",", "")            # 1,234,567 → thousands
        else:
            s = s.replace(",", ".")           # decimal comma
    elif has_dot:
        # Only dots. Same 3-trailing-digit heuristic for the thousands case.
        last = s.split(".")[-1]
        if s.count(".") > 1:
            parts = s.split(".")
            s = "".join(parts[:-1]) + "." + parts[-1]   # 1.234.567,?? → keep last as decimal
        elif len(last) == 3 and len(s.split(".")[0]) <= 3 and len(s.replace(".", "")) >= 4:
            # ambiguous single dot with 3 trailing digits e.g. "1.500":
            # treat as thousands only when it forms a >=4-digit integer.
            s = s.replace(".", "")
        # else: plain decimal, leave as-is

    try:
        v = float(s)
    except ValueError:
        return None
    return -v if negative else v
