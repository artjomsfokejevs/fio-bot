"""ERP export adapters — MCP-5 (2026-06-26).

Generates ledger files in formats accepted by the most common LV/EE
bookkeeping systems we may end up integrating with:

  - standard_books — Estonian SmartAccounts / Standard Books "Sales/Purchase
    Invoices" CSV import format (semicolon-separated, EE column names).
  - jumis_pro     — Latvian JumisPro "Pirkumi" CSV import format
    (UTF-8 with BOM, Latvian column names).
  - generic_csv   — vendor-neutral fallback (English column names) usable
    for ad-hoc import into anything else.

Each adapter consumes the same list[dict] of posted/approved documents and
returns (filename, bytes) tuples that the route handler can wrap into an
HTTP response (single file) or zip (multi-format batch).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

from services import db, pc_codes

logger = logging.getLogger(__name__)

__all__ = ["build_export", "list_supported_formats", "select_documents"]


SUPPORTED = ("standard_books", "jumis_pro", "generic_csv")


def list_supported_formats() -> List[Dict[str, str]]:
    return [
        {"id": "standard_books", "label": "Standard Books / SmartAccounts (EE)",
         "extension": "csv", "encoding": "utf-8"},
        {"id": "jumis_pro", "label": "JumisPro (LV)",
         "extension": "csv", "encoding": "utf-8-sig"},
        {"id": "generic_csv", "label": "Generic CSV (English headers)",
         "extension": "csv", "encoding": "utf-8"},
    ]


def select_documents(period: str,
                      pc: str | None = None,
                      statuses: Iterable[str] = ("posted", "approved",
                                                  "confirmed_to_pay",
                                                  "budget_validated"),
                      ) -> List[Dict[str, Any]]:
    """Pull documents matching a period (YYYY-MM) and optional PC."""
    conn = db.get_connection()
    try:
        placeholders = ",".join("?" for _ in statuses)
        params: List[Any] = [period]
        sql = (f"SELECT * FROM documents WHERE period = ? "
               f"AND status IN ({placeholders})")
        params.extend(statuses)
        if pc:
            canonical = pc_codes.to_canonical(pc) or pc
            aliases = pc_codes.legacy_aliases_of(canonical)
            ph = ",".join("?" for _ in aliases)
            sql += f" AND profit_center IN ({ph})"
            params.extend(aliases)
        sql += " ORDER BY uploaded_at"
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _amount(row: Dict[str, Any]) -> float:
    try:
        return round(float(row.get("amount") or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _doc_date(row: Dict[str, Any]) -> str:
    """Best-effort DD.MM.YYYY for LV/EE imports."""
    for k in ("posted_at", "approved_at", "uploaded_at"):
        v = (row.get(k) or "").strip()
        if v:
            try:
                d = datetime.fromisoformat(v.replace("Z", ""))
                return d.strftime("%d.%m.%Y")
            except ValueError:
                continue
    return ""


# ── Standard Books / SmartAccounts EE ────────────────────────────────────
# CSV with semicolon separator, header row, EE column names.
# Spec reference: https://www.smartaccounts.eu/en/help/csv-import
_SB_HEADERS = [
    "Kuupäev",           # Date DD.MM.YYYY
    "Number",            # Doc number / our id
    "Hankija",           # Vendor name
    "Kogusumma",         # Total amount
    "Valuuta",           # Currency
    "Kontonumber",       # GL/ledger code
    "Kulukoht",          # Profit center / cost center
    "Selgitus",          # Description
]


def _standard_books(rows: List[Dict[str, Any]]) -> Tuple[str, bytes]:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(_SB_HEADERS)
    for r in rows:
        w.writerow([
            _doc_date(r),
            r.get("id") or "",
            (r.get("vendor") or "").strip(),
            "%.2f" % _amount(r),
            (r.get("currency") or "EUR").upper(),
            r.get("ledger_code") or "",
            r.get("profit_center") or "",
            ((r.get("vendor") or "") + " — " + (r.get("ledger_code") or "")).strip(" —"),
        ])
    return ("standard_books_export.csv", buf.getvalue().encode("utf-8"))


# ── JumisPro LV ──────────────────────────────────────────────────────────
# CSV UTF-8 with BOM (so Excel opens it cleanly), comma separator,
# Latvian column names matching JumisPro "Pirkumi" import wizard.
_JP_HEADERS = [
    "Datums",           # Date DD.MM.YYYY
    "DokumentaNumurs",  # Doc number
    "Piegādātājs",      # Vendor
    "Summa",            # Amount
    "Valūta",           # Currency
    "Konts",            # GL code
    "Struktūrvienība",  # PC
    "Apraksts",         # Description
    "PVN",              # VAT amount (0 if unknown)
]


def _jumis_pro(rows: List[Dict[str, Any]]) -> Tuple[str, bytes]:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=",", lineterminator="\r\n",
                    quoting=csv.QUOTE_MINIMAL)
    w.writerow(_JP_HEADERS)
    for r in rows:
        total = _amount(r)
        vat = 0.0  # JumisPro requires the field; leave 0 unless we know
        w.writerow([
            _doc_date(r),
            r.get("id") or "",
            (r.get("vendor") or "").strip(),
            "%.2f" % total,
            (r.get("currency") or "EUR").upper(),
            r.get("ledger_code") or "",
            r.get("profit_center") or "",
            ((r.get("vendor") or "") + " · " + (r.get("ledger_code") or "")).strip(" ·"),
            "%.2f" % vat,
        ])
    # UTF-8 BOM so Excel reads it correctly when Rita opens for review.
    return ("jumis_pro_export.csv",
            "﻿".encode("utf-8") + buf.getvalue().encode("utf-8"))


# ── Generic CSV ──────────────────────────────────────────────────────────
_GEN_HEADERS = [
    "date", "doc_id", "vendor", "amount", "currency",
    "ledger_code", "profit_center", "legal_entity", "status", "description",
]


def _generic_csv(rows: List[Dict[str, Any]]) -> Tuple[str, bytes]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(_GEN_HEADERS)
    for r in rows:
        w.writerow([
            _doc_date(r),
            r.get("id") or "",
            (r.get("vendor") or "").strip(),
            "%.2f" % _amount(r),
            (r.get("currency") or "EUR").upper(),
            r.get("ledger_code") or "",
            r.get("profit_center") or "",
            r.get("legal_entity") or "",
            r.get("status") or "",
            ((r.get("vendor") or "") + " — " + (r.get("ledger_code") or "")).strip(" —"),
        ])
    return ("generic_export.csv", buf.getvalue().encode("utf-8"))


_BUILDERS = {
    "standard_books": _standard_books,
    "jumis_pro": _jumis_pro,
    "generic_csv": _generic_csv,
}


def build_export(format_id: str,
                  period: str,
                  pc: str | None = None) -> Tuple[str, bytes, int]:
    """Returns (filename, file_bytes, row_count).
    Raises ValueError on unknown format / no rows."""
    if format_id not in _BUILDERS:
        raise ValueError("unsupported format: " + format_id)
    rows = select_documents(period=period, pc=pc)
    if not rows:
        raise ValueError("no documents to export for period %s%s"
                          % (period, " / pc=" + pc if pc else ""))
    name, data = _BUILDERS[format_id](rows)
    return name, data, len(rows)
