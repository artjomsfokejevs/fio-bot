"""SQLite database layer for FIO document queue and audit log."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

__all__ = [
    "init_db",
    "insert_document",
    "update_document",
    "get_documents",
    "get_document",
    "get_documents_by_profit_center",
    "get_audit_log",
    "get_document_stats_by_stream",
    "insert_audit_log",
    "insert_ml_feedback",
    "get_connection",
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    original_name TEXT,
    file_type TEXT,
    file_size INTEGER,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT DEFAULT 'User',
    status TEXT DEFAULT 'pending',
    parsed_json TEXT,
    classification_json TEXT,
    confidence INTEGER DEFAULT 0,
    ledger_code TEXT,
    profit_center TEXT,
    period TEXT,
    amount REAL,
    currency TEXT DEFAULT 'EUR',
    vendor TEXT,
    approved_by TEXT,
    approved_at TEXT,
    reject_reason TEXT,
    posted_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT,
    action TEXT,
    details TEXT,
    performed_by TEXT,
    performed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ml_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    is_correct INTEGER NOT NULL,
    wrong_fields TEXT,
    comment TEXT,
    created_at TEXT NOT NULL
);

-- Phase 6 — Card transaction audit (period-close workflow)
-- Bookkeeper imports CSV from Mercury/Revolut/etc, then reconciler finds
-- which transactions don't have a matching invoice in `documents`.
CREATE TABLE IF NOT EXISTS card_transactions (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,        -- mercury / revolut / stripe / generic
    batch_id        TEXT NOT NULL,        -- group rows by import batch
    imported_at     TEXT NOT NULL,
    imported_by     TEXT,
    posted_at       TEXT NOT NULL,
    period          TEXT NOT NULL,        -- YYYY-MM
    amount          REAL NOT NULL,        -- negative for outflows
    currency        TEXT NOT NULL,
    amount_eur      REAL,                 -- ECB-converted
    fx_rate         REAL,
    fx_date         TEXT,
    description     TEXT,
    counterparty    TEXT,
    reference       TEXT,
    card_holder     TEXT,                 -- assigned by user — drives department
    department      TEXT,                 -- inferred from card_holder via BT4YOU map
    profit_center   TEXT,
    matched_invoice_id TEXT,              -- FK to documents.id (or NULL)
    match_status    TEXT DEFAULT 'unmatched',  -- matched / suggested / unmatched / manual / excluded
    match_confidence INTEGER DEFAULT 0,
    match_reason    TEXT,
    notes           TEXT,
    raw_row         TEXT,                 -- original CSV row (json string)
    UNIQUE(source, posted_at, amount, description)
);
CREATE INDEX IF NOT EXISTS ix_ct_period      ON card_transactions(period);
CREATE INDEX IF NOT EXISTS ix_ct_department  ON card_transactions(department);
CREATE INDEX IF NOT EXISTS ix_ct_match       ON card_transactions(match_status);
CREATE INDEX IF NOT EXISTS ix_ct_holder      ON card_transactions(card_holder);

-- 2026-06-07 P1 — FIO users roster, managed in Admin tab by HR/Bookkeeper.
-- Replaces the hardcoded "Who is uploading?" dropdown in Upload tab and
-- becomes the single source of truth for valid uploader/approver names.
-- We intentionally do NOT use this for auth (Basic Auth stays on the
-- server middleware); this is a domain-roster table only.
CREATE TABLE IF NOT EXISTS fio_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT,
    role          TEXT NOT NULL DEFAULT 'uploader',
    profit_center TEXT,
    department    TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    updated_at    TEXT,
    updated_by    TEXT,
    UNIQUE(full_name)
);
CREATE INDEX IF NOT EXISTS ix_fio_users_active ON fio_users(active);
CREATE INDEX IF NOT EXISTS ix_fio_users_role   ON fio_users(role);

-- 2026-06-08 Top-7 P2.3 — Paying accounts roster. Admin/Bookkeeper
-- maintains the list of bank accounts the holding uses to wire payments.
-- The Mark Paid modal pulls this list as a dropdown instead of a free-text
-- prompt, so account labels stay consistent across docs and audit log.
CREATE TABLE IF NOT EXISTS paying_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,         -- short display: "Revolut · Amitours London"
    bank            TEXT,                  -- "Revolut", "Swedbank", "Wise"
    iban            TEXT,                  -- optional, full IBAN
    account_number  TEXT,                  -- for non-IBAN systems (US, etc.)
    currency        TEXT NOT NULL DEFAULT 'EUR',
    legal_entity    TEXT,                  -- FK-style ref to legal_entities.json code
    notes           TEXT,                  -- e.g. "main operating account"
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    updated_at      TEXT,
    updated_by      TEXT,
    UNIQUE(label)
);
CREATE INDEX IF NOT EXISTS ix_paying_active ON paying_accounts(active);
CREATE INDEX IF NOT EXISTS ix_paying_entity ON paying_accounts(legal_entity);

-- 2026-06-11 Top-2 P1 — App-level settings (key/value).
-- First consumer: chase_task_title + chase_task_body for the Bank
-- Statement Audit month-close "🚀 Generate chase tasks" flow.
-- Bookkeeper edits via Admin tab → values override the built-in defaults.
CREATE TABLE IF NOT EXISTS fio_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

-- 2026-06-16 Phase 1 P1.2 — Editable policy rules.
-- Replaces hardcoded EXPENSE_POLICIES in classifier.py. Each row is one
-- threshold ("office_supplies > 500 EUR per item → RED"). Admin/CEO edit
-- via the new Policies & Limits tab. classifier.check_expense_policy()
-- loads via services.policy_rules (with mtime/version cache + DEFAULTS
-- fallback if table empty, so a fresh DB still produces violations
-- using the canonical built-in thresholds).
CREATE TABLE IF NOT EXISTS policy_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,        -- e.g. "office_supplies_max_per_item"
    policy_name   TEXT NOT NULL,               -- "office_supplies" / "business_dinner" / "business_travel"
    field         TEXT NOT NULL,               -- "max_per_item" / "max_total" / "max_per_person" / "max_per_day"
    description   TEXT,                        -- human copy shown in the tab
    level         TEXT NOT NULL DEFAULT 'red', -- 'red' | 'yellow' | 'green'
    threshold_eur REAL NOT NULL,
    unit          TEXT NOT NULL DEFAULT 'per_invoice',  -- 'per_invoice' | 'per_person' | 'per_day'
    requires      TEXT,                        -- e.g. "travel_order", "attendee_list", or NULL
    scope         TEXT,                        -- optional: legal_entity / department scope
    owner         TEXT,                        -- who set / approved this limit (CEO/CFO name)
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    updated_at    TEXT,
    updated_by    TEXT
);
CREATE INDEX IF NOT EXISTS ix_policy_rules_active ON policy_rules(active);
CREATE INDEX IF NOT EXISTS ix_policy_rules_policy ON policy_rules(policy_name);

CREATE TABLE IF NOT EXISTS policy_rules_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     INTEGER,
    rule_code   TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    changed_by  TEXT,
    change_type TEXT NOT NULL,                 -- 'create' | 'update' | 'delete' | 'reactivate'
    old_json    TEXT,
    new_json    TEXT
);
CREATE INDEX IF NOT EXISTS ix_policy_rules_hist_rule ON policy_rules_history(rule_id);

-- 2026-06-16 Phase 1 P1.3 — Accounting / CEO approval of policy violations.
-- A violation_key is a stable hash of (doc_id, policy_name, level, message)
-- so approvals survive re-classifying the same doc. When approved, the
-- violation hides from the default Policy Violations panel (toggle shows
-- resolved ones with full who/when/why). Audit-trail by design.
CREATE TABLE IF NOT EXISTS policy_violation_approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    violation_key   TEXT NOT NULL UNIQUE,
    doc_id          TEXT,
    policy_name     TEXT,
    level           TEXT,
    message         TEXT,
    approved_by     TEXT NOT NULL,
    approved_at     TEXT NOT NULL,
    role            TEXT,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS ix_pva_doc ON policy_violation_approvals(doc_id);
"""


def get_connection() -> sqlite3.Connection:
    """Return a new SQLite connection with row factory enabled."""
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they do not exist."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        # Migration: add uploaded_by column if missing (for existing databases)
        try:
            conn.execute("SELECT uploaded_by FROM documents LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE documents ADD COLUMN uploaded_by TEXT DEFAULT 'User'")
            conn.commit()
            logger.info("Migrated: added uploaded_by column")
        # Migration: department + cost_reason for the new ledger UX
        for col, ddl in (
            ("department", "ALTER TABLE documents ADD COLUMN department TEXT"),
            ("cost_reason", "ALTER TABLE documents ADD COLUMN cost_reason TEXT"),
            # Phase 2.1 — FX (ECB EUR equivalent + original amount)
            ("amount_orig", "ALTER TABLE documents ADD COLUMN amount_orig REAL"),
            ("currency_orig", "ALTER TABLE documents ADD COLUMN currency_orig TEXT"),
            ("fx_rate", "ALTER TABLE documents ADD COLUMN fx_rate REAL"),
            ("fx_date", "ALTER TABLE documents ADD COLUMN fx_date TEXT"),
            ("fx_source", "ALTER TABLE documents ADD COLUMN fx_source TEXT"),
            # Phase 2.4 — payment method (card/bank/cash/unknown)
            ("payment_method", "ALTER TABLE documents ADD COLUMN payment_method TEXT"),
            # Phase 2.5 — extended money breakdown
            ("subtotal", "ALTER TABLE documents ADD COLUMN subtotal REAL"),
            ("discount", "ALTER TABLE documents ADD COLUMN discount REAL"),
            ("credits", "ALTER TABLE documents ADD COLUMN credits REAL"),
            # Phase 3.5 — multi-stream cost allocation (Katia case)
            # JSON list: [{"profit_center":"AA","percentage":60,"amount":1380,"ledger_code":"BT00","note":"..."}]
            ("allocations_json", "ALTER TABLE documents ADD COLUMN allocations_json TEXT"),
            # Phase 5c — manual vendor verification (alternative to OpenCorporates for non-EU)
            ("vendor_verified_by", "ALTER TABLE documents ADD COLUMN vendor_verified_by TEXT"),
            ("vendor_verified_at", "ALTER TABLE documents ADD COLUMN vendor_verified_at TEXT"),
            ("vendor_verified_note", "ALTER TABLE documents ADD COLUMN vendor_verified_note TEXT"),
            # Phase 7 — Confirm-for-Payment workflow (CEO Holding approves week,
            # bookkeeper executes). New statuses: confirmed_to_pay, paid.
            ("confirmed_to_pay_at", "ALTER TABLE documents ADD COLUMN confirmed_to_pay_at TEXT"),
            ("confirmed_to_pay_by", "ALTER TABLE documents ADD COLUMN confirmed_to_pay_by TEXT"),
            ("confirmed_to_pay_note", "ALTER TABLE documents ADD COLUMN confirmed_to_pay_note TEXT"),
            # Bookkeeper budget pre-check (Phase 7.2)
            ("budget_validated_at", "ALTER TABLE documents ADD COLUMN budget_validated_at TEXT"),
            ("budget_validated_by", "ALTER TABLE documents ADD COLUMN budget_validated_by TEXT"),
            ("budget_validated_note", "ALTER TABLE documents ADD COLUMN budget_validated_note TEXT"),
            ("payment_executed_at", "ALTER TABLE documents ADD COLUMN payment_executed_at TEXT"),
            ("payment_executed_by", "ALTER TABLE documents ADD COLUMN payment_executed_by TEXT"),
            ("payment_account", "ALTER TABLE documents ADD COLUMN payment_account TEXT"),
            ("payment_paying_entity", "ALTER TABLE documents ADD COLUMN payment_paying_entity TEXT"),
            ("payment_reference", "ALTER TABLE documents ADD COLUMN payment_reference TEXT"),
            # 2026-06-07 P1 — Legal Entity column (Stream ≠ Legal Entity per
            # Rita's feedback). Values from data/legal_entities.json.
            ("legal_entity", "ALTER TABLE documents ADD COLUMN legal_entity TEXT"),
            # 2026-06-07 P1 — Comments at the Approved-Awaiting-Payment stage
            # (e.g. "wrong bank details", "not enough funds", "on hold").
            ("payment_comment", "ALTER TABLE documents ADD COLUMN payment_comment TEXT"),
            # ─────────────────────────────────────────────────────────────
            # 2026-06-07 P2 — Payment workflow extensions (Rita's tester feedback)
            # ─────────────────────────────────────────────────────────────
            # P2.1: Already-paid-by-card flag. When true, doc skips the
            # Awaiting-CEO + Awaiting-Payment stages — bookkeeper has
            # nothing to wire because someone already paid via corporate
            # card. We still capture the card holder for reconciliation.
            ("already_paid_by_card",
                "ALTER TABLE documents ADD COLUMN already_paid_by_card INTEGER DEFAULT 0"),
            ("paid_card_holder",
                "ALTER TABLE documents ADD COLUMN paid_card_holder TEXT"),
            # P2.2: Desired payment date + payment state machine.
            # States: needs_to_pay (default) → in_progress → paid.
            # 'on_hold' is also valid (use payment_comment for reason).
            ("desired_payment_date",
                "ALTER TABLE documents ADD COLUMN desired_payment_date TEXT"),
            ("payment_state",
                "ALTER TABLE documents ADD COLUMN payment_state TEXT DEFAULT 'needs_to_pay'"),
            # P2.5: Pay-in-original-currency tracking. Rita pays a HUF
            # invoice in HUF from a HUF account; we still record EUR for
            # accounting truth, but also keep the original-currency
            # amount + FX rate for the audit trail.
            # payment_currency: 'EUR' (default) | <orig currency code>
            # If payment_currency == orig currency, the wire was native.
            ("payment_currency",
                "ALTER TABLE documents ADD COLUMN payment_currency TEXT"),
            ("payment_amount_orig",
                "ALTER TABLE documents ADD COLUMN payment_amount_orig REAL"),
            ("payment_fx_rate",
                "ALTER TABLE documents ADD COLUMN payment_fx_rate REAL"),
        ):
            try:
                conn.execute("SELECT %s FROM documents LIMIT 1" % col)
            except sqlite3.OperationalError:
                conn.execute(ddl)
                conn.commit()
                logger.info("Migrated: added %s column", col)
        logger.info("Database initialised at %s", config.DB_PATH)
    finally:
        conn.close()


def insert_document(doc: Dict[str, Any]) -> None:
    """Insert a new document row.

    Args:
        doc: Dictionary with keys matching the documents table columns.
    """
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO documents
               (id, filename, original_name, file_type, file_size, uploaded_at, uploaded_by, status)
               VALUES (:id, :filename, :original_name, :file_type, :file_size, :uploaded_at, :uploaded_by, :status)""",
            doc,
        )
        conn.commit()
        logger.info("Inserted document %s", doc["id"])
    finally:
        conn.close()


def update_document(doc_id: str, fields: Dict[str, Any]) -> None:
    """Update specific fields on a document row.

    Args:
        doc_id: The document primary key.
        fields: Column-value pairs to update.
    """
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [doc_id]
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE documents SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()
        logger.info("Updated document %s: %s", doc_id, list(fields.keys()))
    finally:
        conn.close()


def get_documents(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return documents, optionally filtered by status.

    Args:
        status: If provided, only return documents with this status.

    Returns:
        List of document dictionaries.
    """
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM documents WHERE status = ? ORDER BY uploaded_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY uploaded_at DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_document(doc_id: str) -> Optional[Dict[str, Any]]:
    """Return a single document by ID.

    Args:
        doc_id: The document primary key.

    Returns:
        Document dictionary or None if not found.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def insert_audit_log(
    document_id: str,
    action: str,
    details: Optional[Dict[str, Any]] = None,
    performed_by: str = "system",
) -> None:
    """Write an entry to the audit log.

    Args:
        document_id: Related document ID.
        action: Action name (uploaded, parsed, classified, approved, etc.).
        details: Optional JSON-serialisable details.
        performed_by: Who performed the action.
    """
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO audit_log (document_id, action, details, performed_by, performed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                document_id,
                action,
                json.dumps(details) if details else None,
                performed_by,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_stats() -> Dict[str, int]:
    """Return counts per document status.

    Returns:
        Dictionary with status names as keys and counts as values.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM documents GROUP BY status"
        ).fetchall()
        result: Dict[str, int] = {
            "pending": 0,
            "parsed": 0,
            "classified": 0,
            "approved": 0,
            "rejected": 0,
            "posted": 0,
        }
        for row in rows:
            result[row["status"]] = row["cnt"]
        return result
    finally:
        conn.close()


def insert_ml_feedback(
    document_id: str,
    is_correct: bool,
    wrong_fields: Optional[List[str]] = None,
    comment: Optional[str] = None,
) -> None:
    """Insert ML feedback for a document.

    Args:
        document_id: The document ID this feedback relates to.
        is_correct: True if the scan was correct, False otherwise.
        wrong_fields: List of field names that were wrong.
        comment: Free-text comment from the user.
    """
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO ml_feedback
               (document_id, is_correct, wrong_fields, comment, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                document_id,
                1 if is_correct else 0,
                json.dumps(wrong_fields) if wrong_fields else None,
                comment,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        logger.info("ML feedback saved for document %s (correct=%s)", document_id, is_correct)
    finally:
        conn.close()


def get_documents_by_profit_center(
    profit_center: str,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return documents filtered by profit center.

    Includes docs where the requested PC appears either as the main
    `profit_center` field (single-stream posting) OR inside the
    `allocations_json` JSON list (split-allocated docs).

    Args:
        profit_center: Profit center code (e.g. 'AA', 'BK').
        status: If provided, also filter by this status.

    Returns:
        List of document dictionaries.
    """
    conn = get_connection()
    pc_like = '%"profit_center": "' + profit_center + '"%'
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM documents "
                "WHERE (profit_center = ? OR allocations_json LIKE ?) "
                "AND status = ? "
                "ORDER BY uploaded_at DESC",
                (profit_center, pc_like, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM documents "
                "WHERE profit_center = ? OR allocations_json LIKE ? "
                "ORDER BY uploaded_at DESC",
                (profit_center, pc_like),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_audit_log(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the most recent audit log entries.

    Args:
        limit: Maximum number of entries to return.

    Returns:
        List of audit log dictionaries.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY performed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_document_stats_by_stream() -> Dict[str, Dict[str, int]]:
    """Return document counts grouped by profit center and status.

    Returns:
        Dictionary keyed by profit_center code, each containing
        status counts and a total.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT profit_center, status, COUNT(*) as cnt "
            "FROM documents WHERE profit_center IS NOT NULL "
            "GROUP BY profit_center, status"
        ).fetchall()
        result: Dict[str, Dict[str, int]] = {}
        for row in rows:
            pc = row["profit_center"]
            if pc not in result:
                result[pc] = {"pending": 0, "classified": 0, "approved": 0, "rejected": 0, "posted": 0, "total": 0}
            result[pc][row["status"]] = row["cnt"]
            result[pc]["total"] = result[pc].get("total", 0) + row["cnt"]
        return result
    finally:
        conn.close()


# JSON-stored columns across all tables. Keep this list as the single source
# of truth — any new TEXT column holding JSON must be added here so
# _row_to_dict() decodes it everywhere. (FIO retro G54, 2026-06-02)
_JSON_COLUMNS = (
    "parsed_json",
    "classification_json",
    "allocations_json",
    "raw_row",          # card_transactions
    "details",          # audit_log
)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a sqlite3.Row to a plain dictionary, parsing known JSON fields.

    JSON-stored columns (see _JSON_COLUMNS) are decoded with json.loads();
    malformed payloads stay as raw strings (callers can still introspect).
    """
    d = dict(row)
    for key in _JSON_COLUMNS:
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
