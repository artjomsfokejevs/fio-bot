"""Key-value app settings stored in SQLite.

Used for editable copy that's neither code nor user-content:
  * chase_task_title  — title template for missing-invoice chase
  * chase_task_body   — body template (multiline)

The admin tab exposes editing; the chase-missing endpoint reads via
`get(key, default)`. If a key is missing, the default (compiled into
the codebase) is returned — so the product still works in a fresh DB.

Added 2026-06-11 for the Top-2 "editable chase template" feature.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from services import db

logger = logging.getLogger(__name__)

__all__ = ["get", "set_", "list_all", "DEFAULTS"]


# Built-in defaults — used when no DB row exists for a key.
DEFAULTS = {
    "chase_task_title": (
        "📄 Missing invoice — €{amount} · {vendor} · {date}"
    ),
    "chase_task_body": (
        "Hi {stakeholder},\n\n"
        "FIO Bank Statement Audit flagged a transaction in your stream "
        "({pc} — {pc_label}) that has no matching invoice.\n\n"
        "Transaction details:\n"
        "  Date: {date}\n"
        "  Amount: €{amount}\n"
        "  Description: {description}\n"
        "  Counterparty: {counterparty}\n"
        "  Reference: {reference}\n"
        "  Source: {source}\n\n"
        "FIO attribution: stream {pc} ({reason})\n"
        "Routed to: {stakeholder} — {stakeholder_title}\n\n"
        "Action: please find the invoice for this charge and upload it "
        "to FIO (https://fio-amitours.fly.dev/) under Upload → mark as "
        "'already paid'. If this charge belongs to a different stream, "
        "reply with the correct profit-center code so Rita can re-route.\n\n"
        "— FIO Accounting Bot (auto-generated month-close chase, routed by stream)"
    ),
}


def get(key: str, default: Optional[str] = None) -> str:
    """Fetch a setting. Falls back to DEFAULTS[key], then `default` arg."""
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM fio_settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row[0] is not None:
            return row[0]
    finally:
        conn.close()
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default or ""


def set_(key: str, value: str, by: str = "admin") -> None:
    """Upsert. Empty value resets to default (DELETEs the row)."""
    conn = db.get_connection()
    try:
        if value == "" or value is None:
            conn.execute("DELETE FROM fio_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO fio_settings (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  value = excluded.value, updated_at = excluded.updated_at, "
                "  updated_by = excluded.updated_by",
                (key, value, datetime.utcnow().isoformat(), by),
            )
        conn.commit()
        logger.info("setting saved: %s by=%s", key, by)
    finally:
        conn.close()


def list_all() -> dict:
    """Return every setting (overlay over DEFAULTS so caller sees both
    set + unset keys with their effective values)."""
    out = dict(DEFAULTS)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT key, value, updated_at, updated_by FROM fio_settings"
        ).fetchall()
        for r in rows:
            out[r[0]] = r[1]
        meta = {r[0]: {"updated_at": r[2], "updated_by": r[3]} for r in rows}
    finally:
        conn.close()
    return {"effective": out, "defaults": DEFAULTS, "meta": meta}
