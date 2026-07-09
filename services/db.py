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

# ── Canonical document-status groupings (2026-07-08, H10) ────────────────
# A document's lifecycle is:
#   pending -> parsed -> classified -> approved -> posted
#            -> budget_validated -> confirmed_to_pay -> paid
# (rejected / cancelled / needs_review are off the happy path)
#
# Three modules previously each hardcoded their OWN status set — including
# phantom values ("archived", "payment_executed") that are not real
# document statuses — so X-alarm, consolidated P&L, and weekly actuals
# reported three different "actuals" for the same month. These two
# constants are the single source of truth; import them, never re-list.
#
# COMMITTED_STATUSES — money committed against a stream's budget (the
#   forward-looking view that drives X-alarm / budget actuals): once the
#   bookkeeper budget-validates, the money is spoken for.
COMMITTED_STATUSES = ("budget_validated", "confirmed_to_pay", "paid")
#
# POSTED_STATUSES — money actually booked to the ledger / P&L (the
#   backward-looking view for consolidated P&L and weekly cash actuals).
POSTED_STATUSES = ("posted", "paid")

__all__ = [
    "COMMITTED_STATUSES",
    "POSTED_STATUSES",
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
    -- 2026-07-08 (H1) — the old UNIQUE(source, posted_at, amount,
    -- description) silently dropped GENUINE same-day duplicate charges
    -- (e.g. 2x EUR 4.50 Starbucks), under-reporting month totals. The
    -- primary-key `id` now folds in the row's position within its import
    -- file, so re-importing the same file stays idempotent while two
    -- identical rows in one file both survive. No 4-col UNIQUE needed.
    row_seq         INTEGER DEFAULT 0
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

-- 2026-07-09 (SOP Procedure 4, G3) — vendor bank-detail change control.
-- First IBAN seen per vendor is remembered; a later invoice from the same
-- vendor with a different IBAN is flagged and payment is blocked until a
-- human re-verifies (guards against vendor-impersonation / BEC fraud).
CREATE TABLE IF NOT EXISTS vendor_bank_details (
    vendor_key   TEXT NOT NULL,      -- 'vat:LV…' or 'name:<lowercased>'
    iban         TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    first_doc_id TEXT,
    recorded_by  TEXT,
    PRIMARY KEY (vendor_key, iban)
);
CREATE INDEX IF NOT EXISTS ix_vbd_vendor ON vendor_bank_details(vendor_key);

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
-- 2026-06-16 Phase 1 P1.5 — Partial payments on internal invoices.
-- An internal invoice (vendor = our own legal entity) is often paid in
-- instalments rather than one wire. Each row here = one instalment.
-- When SUM(amount_eur) >= documents.amount, the doc auto-transitions
-- to status='paid'. Visible only on docs where documents.is_internal=1.
CREATE TABLE IF NOT EXISTS partial_payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        TEXT NOT NULL,
    amount_eur    REAL NOT NULL,
    paid_at       TEXT NOT NULL,                    -- YYYY-MM-DD
    method        TEXT,                             -- 'bank_transfer' | 'card' | 'cash' | 'netting'
    reference     TEXT,                             -- bank-statement ref or note
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_pp_doc ON partial_payments(doc_id);

-- 2026-06-16 Phase 2 — In-app notification bell.
-- Used (so far) when CEO confirms a payment to surface to Rita without
-- forcing her to refresh the queue. Also a sink for future Phase 3
-- budget-alarm pings, P85-stub messages, etc.
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_role  TEXT,                          -- 'bookkeeper' / 'admin' / 'holding_ceo' / NULL = broadcast
    recipient_user  TEXT,                          -- specific full_name (overrides role); NULL = all users with role
    kind            TEXT NOT NULL,                 -- 'ceo_approved_invoice' / 'urgent_payment' / 'system' / 'budget_alarm'
    title           TEXT NOT NULL,
    body            TEXT,
    doc_id          TEXT,
    href            TEXT,                          -- optional deep-link
    severity        TEXT NOT NULL DEFAULT 'info',  -- 'info' / 'warning' / 'urgent'
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    read_at         TEXT,
    read_by         TEXT
);
CREATE INDEX IF NOT EXISTS ix_notif_role ON notifications(recipient_role);
CREATE INDEX IF NOT EXISTS ix_notif_user ON notifications(recipient_user);
CREATE INDEX IF NOT EXISTS ix_notif_unread ON notifications(read_at);

-- 2026-06-16 Phase 3 — Stream budgets (CEO-agreed monthly caps per stream).
-- Schema per docs/stream-budgets-architecture.md.
CREATE TABLE IF NOT EXISTS stream_budgets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profit_center    TEXT NOT NULL,                 -- 'AA' / 'BK' / 'SR' / 'AH' / 'CF' / 'AL'
    period           TEXT NOT NULL,                 -- 'YYYY-MM'
    budget_eur       REAL NOT NULL,
    currency         TEXT NOT NULL DEFAULT 'EUR',
    agreed_by_ceo_at TEXT,
    agreed_by_ceo    TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL,
    created_by       TEXT,
    updated_at       TEXT,
    updated_by       TEXT,
    UNIQUE(profit_center, period)
);
CREATE INDEX IF NOT EXISTS ix_sb_pc_period ON stream_budgets(profit_center, period);

CREATE TABLE IF NOT EXISTS stream_budget_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id  INTEGER,
    pc         TEXT NOT NULL,
    period     TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT,
    old_eur    REAL,
    new_eur    REAL,
    reason     TEXT
);

CREATE TABLE IF NOT EXISTS xalarm_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_at    TEXT NOT NULL,
    profit_center   TEXT NOT NULL,
    period          TEXT NOT NULL,
    budget_eur      REAL NOT NULL,
    actual_eur      REAL NOT NULL,
    overrun_eur     REAL NOT NULL,
    overrun_pct     REAL NOT NULL,
    trigger_doc_id  TEXT,
    recipients_json TEXT NOT NULL,
    email_status    TEXT,
    asana_task_url  TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_xalarm_pc_period ON xalarm_log(profit_center, period);

-- 2026-06-22 Revenue module Phase 1 (#94 / #96) — Accounts Receivable.
-- Mirrors documents (Accounts Payable) for outgoing invoices: proforma →
-- invoice → paid. Receipts table tracks partial payments from customers.
-- Audit table preserves status-change history. Per docs/revenue-module-architecture.md.
CREATE TABLE IF NOT EXISTS revenue_documents (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK(kind IN ('proforma','invoice','credit_note')),
    profit_center   TEXT NOT NULL,                 -- canonical PC code (services/pc_codes.py)
    customer        TEXT,                          -- customer / counterparty name
    customer_vat    TEXT,                          -- VAT number (for B2B)
    legal_entity    TEXT,                          -- our entity that issued the doc
    invoice_number  TEXT,                          -- our internal numbering
    issue_date      TEXT,                          -- YYYY-MM-DD
    due_date        TEXT,                          -- YYYY-MM-DD
    amount          REAL,                          -- in original currency
    amount_eur      REAL,                          -- EUR-equivalent at issue date
    currency        TEXT NOT NULL DEFAULT 'EUR',
    description     TEXT,
    ledger_code     TEXT,                          -- e.g. BB00 / BC00 / INT0 / OTH0
    status          TEXT NOT NULL DEFAULT 'draft', -- draft|sent|partially_paid|paid|cancelled
    proforma_id     TEXT,                          -- back-ref when kind='invoice' replaces a proforma
    uploaded_at     TEXT NOT NULL,
    uploaded_by     TEXT,
    file_path       TEXT,                          -- PDF / image / docx
    file_type       TEXT,
    parsed_json     TEXT,                          -- LLM-extracted data
    classification_json TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS ix_rd_pc_period ON revenue_documents(profit_center, issue_date);
CREATE INDEX IF NOT EXISTS ix_rd_status   ON revenue_documents(status);
CREATE INDEX IF NOT EXISTS ix_rd_proforma ON revenue_documents(proforma_id);
CREATE INDEX IF NOT EXISTS ix_rd_customer ON revenue_documents(customer);

CREATE TABLE IF NOT EXISTS revenue_receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revenue_doc_id  TEXT NOT NULL,
    received_at     TEXT NOT NULL,                 -- YYYY-MM-DD
    amount_eur      REAL NOT NULL,
    method          TEXT,                          -- bank_transfer / card / stripe / netting / other
    reference       TEXT,                          -- bank ref / Stripe charge id
    bank_statement_tx_id TEXT,                     -- optional FK → card_transactions.id (Phase 3)
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    FOREIGN KEY (revenue_doc_id) REFERENCES revenue_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_rr_doc ON revenue_receipts(revenue_doc_id);
CREATE INDEX IF NOT EXISTS ix_rr_received_at ON revenue_receipts(received_at);

CREATE TABLE IF NOT EXISTS revenue_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revenue_doc_id  TEXT NOT NULL,
    action          TEXT NOT NULL,                 -- created / converted / sent / received_payment / cancelled / updated
    details_json    TEXT,
    actor           TEXT,
    occurred_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ra_doc ON revenue_audit(revenue_doc_id);

-- 2026-06-24 — Chase-task supervision log (mirrors BT4YOU Supervision Manager).
-- One row per Asana task created via the chase flow. Status is refreshed on
-- demand via /api/card-audit/chase-tasks/refresh which calls Asana GET /tasks/<gid>.
CREATE TABLE IF NOT EXISTS chase_tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_gid          TEXT NOT NULL UNIQUE,            -- Asana task gid
    permalink_url     TEXT,
    title             TEXT,
    profit_center     TEXT,                            -- canonical PC for the chased stream
    tx_count          INTEGER NOT NULL DEFAULT 1,
    total_eur         REAL,
    project_gid       TEXT,
    project_name      TEXT,
    assignee_gid      TEXT,
    assignee_name     TEXT,
    due_on            TEXT,                            -- YYYY-MM-DD
    created_by        TEXT,                            -- FIO user who pressed Create
    created_at        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending', -- pending | done | cancelled
    completed_at      TEXT,
    last_synced_at    TEXT,
    tx_ids_json       TEXT,                            -- JSON array of card_transactions.id
    attachments_json  TEXT                             -- JSON array of {doc_id, filename, attachment_gid}
);
CREATE INDEX IF NOT EXISTS ix_chase_status   ON chase_tasks(status);
CREATE INDEX IF NOT EXISTS ix_chase_pc       ON chase_tasks(profit_center);
CREATE INDEX IF NOT EXISTS ix_chase_assignee ON chase_tasks(assignee_gid);
CREATE INDEX IF NOT EXISTS ix_chase_created  ON chase_tasks(created_at);

-- 2026-06-26 (G1) — bank account balances for cashflow projection.
-- Phase 3 of Financial Governance SOP. Manual seed today; once bank APIs
-- ship (H) the daily import job writes here directly.
CREATE TABLE IF NOT EXISTS bank_account_balances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paying_account_id INTEGER,                       -- FK to paying_accounts.id
    pc              TEXT NOT NULL,                   -- canonical PC code
    legal_entity    TEXT,
    balance_eur     REAL NOT NULL,
    balance_orig    REAL,
    currency        TEXT,
    as_of_date      TEXT NOT NULL,                   -- ISO date this snapshot represents
    source          TEXT,                            -- manual|mercury_api|revolut_api|...
    recorded_at     TEXT NOT NULL,
    recorded_by     TEXT
);
CREATE INDEX IF NOT EXISTS ix_bb_pc_date     ON bank_account_balances(pc, as_of_date);
CREATE INDEX IF NOT EXISTS ix_bb_account     ON bank_account_balances(paying_account_id);

-- 2026-06-29 — Weekly cashflow (Amitours UNIFIED CASH TIMELINE).
-- Mirrors the operator's planning Google Sheet so we can keep both
-- "Actual" rows (derived from documents + revenue_receipts + bank balances)
-- and "Forecast"/"Estimate" rows (operator-entered) in one table. Read API
-- exposes a richer projection than the simple 13-week one; UI editor for
-- forecast rows lands in a later phase.
--
-- Conventions:
--   * week_start  — ISO Monday (YYYY-MM-DD).
--   * row_type    — actual | forecast | estimate | plug.
--                   'actual' rows are recomputed from source data on every
--                   read so manual edits to them are NOT preserved (the
--                   computer is the source of truth for actuals).
--                   'forecast'/'estimate'/'plug' rows are operator-entered
--                   and survive recompute.
--   * All amounts in EUR. NULL = no value (different from 0).
CREATE TABLE IF NOT EXISTS cashflow_weekly (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start                  TEXT NOT NULL,            -- ISO Monday YYYY-MM-DD
    week_label                  TEXT,                     -- 'W26', 'W-15', 'PLUG: …'
    row_type                    TEXT NOT NULL CHECK(row_type IN ('actual','forecast','estimate','plug')),

    -- Revenue (cash-in)
    b2c_revenue_fact            REAL,
    b2c_revenue_plan            REAL,
    b2b_revenue_fact            REAL,
    b2b_revenue_plan            REAL,
    holdback                    REAL,                     -- Stripe / processor reserve change

    -- Financing
    financing_inflow            REAL,
    financing_outflow           REAL,

    -- Alps2Alps cost engine (operator splits "burn" plan vs fact)
    a2a_burn_fact               REAL,
    a2a_burn_plan               REAL,
    marketing_plan              REAL,
    a2a_cogs_fact               REAL,
    a2a_cogs_plan               REAL,
    a2a_cogs_ap                 REAL,                     -- DEP/BON memo line

    -- Holding & portfolio
    holding_royalty             REAL,
    portfolio_burn_fact         REAL,
    portfolio_burn_plan         REAL,
    portfolio_inflows           REAL,

    -- Other / catch-all
    outstanding_ap_intercompany REAL,                     -- bucket per the Google Sheet
    net_eur                     REAL,                     -- computed: sum of in - sum of out
    balance_eop_eur             REAL,                     -- running balance, end-of-period

    note                        TEXT,                     -- free-form (e.g. plug reason)
    source                      TEXT,                     -- 'derived' | 'manual' | 'import:sheet'
    updated_at                  TEXT NOT NULL,
    updated_by                  TEXT,
    UNIQUE(week_start, row_type)
);
CREATE INDEX IF NOT EXISTS ix_cw_week  ON cashflow_weekly(week_start);
CREATE INDEX IF NOT EXISTS ix_cw_type  ON cashflow_weekly(row_type);

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
    """Return a new SQLite connection with row factory enabled.

    2026-07-08 (C9) — under 2 gunicorn workers, concurrent writes (a CSV
    import overlapping a bookkeeper approval) raised
    ``sqlite3.OperationalError: database is locked`` and 500'd the
    request mid-transaction. Three PRAGMAs fix it:

      * WAL journal mode — readers no longer block a writer and vice
        versa; a single writer + many readers run concurrently.
      * busy_timeout=5000 — a second writer waits up to 5 s for the
        lock instead of failing instantly.
      * foreign_keys=ON — SQLite ships FKs OFF per-connection; several
        tables declare ``ON DELETE CASCADE`` (covenant_checks, …) that
        silently did nothing until now.

    WAL is a database-level setting persisted on first use, but the
    busy_timeout / foreign_keys pragmas are per-connection, so they must
    be re-applied on every connection.
    """
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they do not exist."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        # 2026-07-08 (H1) — rebuild card_transactions to drop the legacy
        # 4-column UNIQUE(source, posted_at, amount, description) that
        # silently dropped genuine same-day duplicate charges. Detect it
        # in the stored table SQL; rebuild once, preserving all rows.
        try:
            tbl_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='card_transactions'"
            ).fetchone()
            tbl_sql = (tbl_sql_row["sql"] if tbl_sql_row else "") or ""
            if "UNIQUE(source, posted_at, amount, description)" in tbl_sql:
                logger.info("Migrating card_transactions: dropping legacy 4-col UNIQUE")
                cols = [r["name"] for r in conn.execute(
                    "PRAGMA table_info(card_transactions)").fetchall()]
                has_row_seq = "row_seq" in cols
                copy_cols = ", ".join(cols)
                conn.execute("ALTER TABLE card_transactions RENAME TO card_transactions_old")
                conn.executescript(SCHEMA_SQL)  # recreates without the UNIQUE
                seq_default = "" if has_row_seq else ", 0"
                seq_col = "row_seq" if has_row_seq else "row_seq"
                if has_row_seq:
                    conn.execute(
                        f"INSERT INTO card_transactions ({copy_cols}) "
                        f"SELECT {copy_cols} FROM card_transactions_old")
                else:
                    conn.execute(
                        f"INSERT INTO card_transactions ({copy_cols}, row_seq) "
                        f"SELECT {copy_cols}, 0 FROM card_transactions_old")
                conn.execute("DROP TABLE card_transactions_old")
                conn.commit()
                logger.info("card_transactions migration complete")
        except sqlite3.OperationalError as exc:
            logger.warning("card_transactions H1 migration skipped: %s", exc)
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
            # 2026-06-16 Phase 1 P1.5 — Internal-invoice flag (gates the
            # partial-payments UI). Default 0; bookkeeper toggles via Admin
            # or it's set at parse time when vendor matches our own legal
            # entities list.
            ("is_internal",
                "ALTER TABLE documents ADD COLUMN is_internal INTEGER DEFAULT 0"),
            # 2026-06-16 Phase 2 — Salary doc category (bookkeeper request).
            # When True the doc shows up in the new Salaries section of
            # Budget Check, filterable by desired_payment_date so Rita can
            # batch-approve invoices coming due on a specific payday.
            ("is_salary",
                "ALTER TABLE documents ADD COLUMN is_salary INTEGER DEFAULT 0"),
            # 2026-06-26 (G2) — Inter-company elimination. When an internal
            # invoice from AG (shared services) lands on AA's books, set
            # counterparty_pc='AG' so the consolidated P&L can subtract the
            # AG→AA leg (otherwise AG cost AND AA cost both inflate the total).
            # NULL means external vendor (no elimination needed).
            ("counterparty_pc",
                "ALTER TABLE documents ADD COLUMN counterparty_pc TEXT"),
            # 2026-06-16 Phase 2 — bank-statement archive metadata.
            # batch_id already lives on card_transactions; this remembers
            # the user-facing archive label + last re-check timestamp
            # without polluting card_transactions rows.
        ):
            try:
                conn.execute("SELECT %s FROM documents LIMIT 1" % col)
            except sqlite3.OperationalError:
                conn.execute(ddl)
                conn.commit()
                logger.info("Migrated: added %s column", col)
        # 2026-06-24 FB-L — fio_users sub-permissions. Comma-separated
        # capability codes that further constrain a role. Example:
        #   bookkeeper + permissions="approve_budget,mark_paid"  → full
        #   bookkeeper + permissions="mark_paid"                 → no budget
        #   bookkeeper + pc_scope="CF,SP"  → only sees CF / SP streams
        for col, ddl in (
            ("permissions",
                "ALTER TABLE fio_users ADD COLUMN permissions TEXT"),
            ("pc_scope",
                "ALTER TABLE fio_users ADD COLUMN pc_scope TEXT"),
        ):
            try:
                conn.execute("SELECT %s FROM fio_users LIMIT 1" % col)
            except sqlite3.OperationalError:
                conn.execute(ddl)
                conn.commit()
                logger.info("Migrated: added fio_users.%s column", col)
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


_DOC_SORT_KEYS = {
    "date_desc":         "COALESCE(uploaded_at, '') DESC",
    "date_asc":          "COALESCE(uploaded_at, '') ASC",
    "amount_desc":       "COALESCE(amount, 0) DESC",
    "amount_asc":        "COALESCE(amount, 0) ASC",
    "legal_entity":      "COALESCE(legal_entity, '') ASC, COALESCE(uploaded_at, '') DESC",
    "vendor":            "COALESCE(vendor, '') ASC, COALESCE(uploaded_at, '') DESC",
}


def get_documents(
    status: Optional[str] = None,
    q: Optional[str] = None,
    sort: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return documents, optionally filtered.

    Args:
        status:    filter by exact status value
        q:         case-insensitive search across vendor / description / amount
        sort:      one of _DOC_SORT_KEYS (defaults to date_desc)
        date_from: YYYY-MM-DD inclusive lower bound on uploaded_at
        date_to:   YYYY-MM-DD inclusive upper bound on uploaded_at

    Phase 1 P1.4 (2026-06-16) added q/sort/date filters — back-compat for
    callers passing only status. Returns full document dict rows.
    """
    where: List[str] = []
    params: List[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if q:
        like = "%" + q.strip().lower() + "%"
        where.append(
            "(LOWER(COALESCE(vendor,'')) LIKE ? OR "
            " LOWER(COALESCE(original_name,'')) LIKE ? OR "
            " LOWER(COALESCE(filename,'')) LIKE ? OR "
            " CAST(COALESCE(amount,0) AS TEXT) LIKE ?)"
        )
        params.extend([like, like, like, like])
    if date_from:
        where.append("DATE(COALESCE(uploaded_at,'')) >= DATE(?)")
        params.append(date_from)
    if date_to:
        where.append("DATE(COALESCE(uploaded_at,'')) <= DATE(?)")
        params.append(date_to)

    sql = "SELECT * FROM documents"
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = _DOC_SORT_KEYS.get(sort or "date_desc", _DOC_SORT_KEYS["date_desc"])
    sql += " ORDER BY " + order

    conn = get_connection()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
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
