"""Shared HTTP helpers used across app.py + blueprints.

2026-07-01 (retro AA153) — one place for the RFC 5987 Content-Disposition
builder so no route ever hits gunicorn/h11 with a raw non-ASCII filename.
"""
from __future__ import annotations

from urllib.parse import quote

__all__ = ["content_disposition"]


def content_disposition(filename: str) -> str:
    """Build a `Content-Disposition: attachment; filename=...` value that is
    safe for HTTP transport even when the filename contains non-ASCII bytes.

    Emits BOTH forms so old (ISO-8859-1 only) and modern (RFC 5987 aware)
    clients both do the right thing:

        attachment;
          filename="Expense_Report_J_nijs_2026.pdf";   ← ASCII fallback
          filename*=UTF-8''Expense_Report_J%C5%ABnijs_2026.pdf

    Rationale: gunicorn's h11 parser refuses any byte outside ISO-8859-1
    in an HTTP header, so a raw `"Jūnijs"` triggers "Invalid HTTP Header"
    → the WSGI worker returns 400 with a 0-byte body → the reverse proxy
    (fly.io / cloudflare / nginx) reports HTTP 502 to the operator, with
    no useful error surface. Route every download through this helper.
    """
    ascii_fallback = (
        filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    )
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'
