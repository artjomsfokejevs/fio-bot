# Revenue / Accounts Receivable module — Architecture sketch

> Phase 3 design doc. Written 2026-06-22 so the next session can execute
> implementation as **pure typing** (per pattern P101 / P103). Bilingual
> EN with short RU summary at the bottom.

## Problem

Today FIO is **expense-only**: invoices come in, they get parsed, classified,
budget-checked, paid. We have zero visibility into the **revenue side** of
each stream:

- Sales teams send out proforma invoices that may or may not become real
  invoices when the customer pays
- No central register of Accounts Payable (vendors we owe) vs Accounts
  Receivable (customers who owe us)
- Cashflow analytics by stream + period is impossible without revenue
  data — we can only report on outflows
- When a proforma becomes a real invoice, there's no link between the two
  → manual reconciliation between deal pipeline + accounting

We want to mirror the existing Accounts Payable (expense) flow with an
Accounts Receivable (revenue) flow, plus cashflow analytics tying them
together by stream + period.

## Out of scope (explicitly)

- CRM functionality (deal pipeline, opportunities, stages) — that's
  Freshworks / Asana
- Tax computation (VAT MOSS / OSS / IOSS) — out for now
- Multi-currency hedging — we record native currency + EUR-equivalent
  the same way the expense flow does today

## Vocabulary

| Term | Meaning |
|---|---|
| **AP** Accounts Payable | What we owe vendors (= existing expense `documents`) |
| **AR** Accounts Receivable | What customers owe us (new — this module) |
| **Proforma invoice** | Pre-sale doc sent to customer; not yet a real receivable |
| **Real invoice** | Issued after customer commits / pays — produces a true AR row |
| **Cashflow** | Sum of (paid expenses) and (received receivables) per period |

## Schema (3 new tables)

```sql
-- 2026-?? Phase 3 Revenue module
CREATE TABLE IF NOT EXISTS revenue_documents (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK(kind IN ('proforma','invoice','credit_note')),
    profit_center   TEXT NOT NULL,                 -- canonical PC code
    customer        TEXT,                          -- customer / counterparty name
    customer_vat    TEXT,                          -- VAT number (for B2B)
    legal_entity    TEXT,                          -- our entity that issued the doc
    invoice_number  TEXT,                          -- our internal numbering
    issue_date      TEXT,
    due_date        TEXT,
    amount          REAL,                          -- in original currency
    amount_eur      REAL,
    currency        TEXT DEFAULT 'EUR',
    description     TEXT,
    ledger_code     TEXT,                          -- e.g. BB00 / BC00 / INT0
    status          TEXT NOT NULL DEFAULT 'draft', -- draft|sent|partially_paid|paid|cancelled
    proforma_id     TEXT,                          -- backref when kind='invoice' and replaces a proforma
    uploaded_at     TEXT NOT NULL,
    uploaded_by     TEXT,
    file_path       TEXT,                          -- PDF / image / docx
    file_type       TEXT,
    parsed_json     TEXT,                          -- LLM-extracted data
    classification_json TEXT,
    notes           TEXT,
    UNIQUE(invoice_number, legal_entity)
);
CREATE INDEX IF NOT EXISTS ix_rd_pc_period ON revenue_documents(profit_center, issue_date);
CREATE INDEX IF NOT EXISTS ix_rd_status ON revenue_documents(status);
CREATE INDEX IF NOT EXISTS ix_rd_proforma ON revenue_documents(proforma_id);

-- Receipts: when customer pays the invoice (partially or fully)
CREATE TABLE IF NOT EXISTS revenue_receipts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    revenue_doc_id  TEXT NOT NULL,
    received_at     TEXT NOT NULL,                 -- date money landed
    amount_eur      REAL NOT NULL,
    method          TEXT,                          -- bank_transfer / card / stripe / netting
    reference       TEXT,                          -- bank ref / Stripe charge id
    bank_statement_tx_id TEXT,                     -- optional FK to card_transactions if matched
    created_at      TEXT NOT NULL,
    created_by      TEXT,
    FOREIGN KEY (revenue_doc_id) REFERENCES revenue_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_rr_doc ON revenue_receipts(revenue_doc_id);

-- Audit trail for proforma → invoice transition + status changes
CREATE TABLE IF NOT EXISTS revenue_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    revenue_doc_id TEXT NOT NULL,
    action       TEXT NOT NULL,                    -- created / converted / sent / received_payment / cancelled
    details_json TEXT,
    actor        TEXT,
    occurred_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ra_doc ON revenue_audit(revenue_doc_id);
```

## Services (3 modules)

### `services/revenue.py`
- `list_docs(period=None, pc=None, status=None, kind=None)` → list
- `get_doc(id)` → single
- `create_doc(payload, *, by)` → insert + audit row
- `convert_proforma_to_invoice(proforma_id, real_invoice_payload, *, by)`
  — preserves backlink + audit "converted"
- `update_status(id, new_status, *, by)` → audit
- `delete_doc(id, *, by)` → cascade receipts + audit

### `services/revenue_receipts.py`
- `add_receipt(revenue_doc_id, amount_eur, received_at, method, reference, *, by)`
- `total_received(revenue_doc_id)` → float
- `remaining(revenue_doc_id)` → float (issue_amount − total_received)
- `auto_transition_if_complete(doc_id, *, by)` → if total ≥ amount, set status=paid
- Same pattern as `partial_payments` for expense invoices (P105 reuse)

### `services/cashflow.py` (analytics)
- `cashflow_for(pc, period_from, period_to)` → `{revenue, expense, net}`
  per month
- Reads both `revenue_receipts` (money in) + `documents.payment_executed_at`
  (money out, status='paid')
- `breakdown_by_stream(period_from, period_to)` → per-PC totals
- `breakdown_by_ledger(period_from, period_to)` → per-code totals
- Foundation for the Revenue tab dashboard

## Routes (1 new blueprint)

`routes/revenue.py` — mirrors `routes/payments.py` shape:

```
GET    /api/revenue                        — list (filters: period, pc, status, kind)
GET    /api/revenue/<id>                   — single
POST   /api/revenue                        — create proforma or invoice
POST   /api/revenue/<id>                   — update fields
POST   /api/revenue/<id>/convert           — proforma → invoice
DELETE /api/revenue/<id>

GET    /api/revenue/<id>/receipts          — list receipts for a doc
POST   /api/revenue/<id>/receipts          — add a receipt
DELETE /api/revenue-receipts/<id>          — remove

GET    /api/cashflow                       — period + pc filters, returns monthly series
GET    /api/cashflow/by-stream             — totals per stream
GET    /api/cashflow/by-ledger             — totals per ledger code
```

Same role-gating as existing expense routes:
- Admin / Holding CEO / Bookkeeper / Stream owner can read
- Admin / Bookkeeper / Stream owner (own stream) can create/update
- Admin only can delete

## UI (1 new tab + Analytics extension)

### New tab: 💵 Revenue (after Confirm for Payment, before FIO Legend)

**Sub-tabs** (mirror Confirm for Payment pattern):
- **Drafts** — proformas not yet sent
- **Sent** — issued invoices awaiting payment
- **Partially paid** — received < amount
- **Paid** — fully received
- **Cancelled / written off**

**Filter bar** (reuse `.bt-filter-bar`):
- Search · sort · date range · stream filter · kind (proforma / invoice) ·
  ledger code multi-select (reuse #93 pattern)

**Add new** form:
- Kind (proforma / invoice) · PC · customer · amount · currency · 
  legal entity (issuer) · due date · ledger code · file upload (PDF)
- "Convert proforma → invoice" action on draft rows

**Detail modal**:
- Same shape as expense modal
- Receipts section (add / list / running total / remaining)
- Audit trail tail

### Analytics tab extension

New "💸 Cashflow by stream" chart card:
- Monthly bars per stream — green (revenue) above zero, red (expense) below
- Hover shows breakdown
- Date-range picker (reuses existing #22 component)
- Drill-down to source docs (reuses #25 pattern)

### FIO Legend update

- Add "Revenue" + "AR" / "AP" / "Cashflow" entries to glossary
- Document the proforma → invoice transition flow
- List the new ledger codes if any (none initially — reuses REVENUE
  group from existing schema: BB00, BC00, INT0, OTH0)

## Phased delivery (3 phases ≈ 12-16h total)

### Phase 1 — Read/write API + minimal admin grid (4-5h)
- Schema migration (3 tables)
- `services/revenue.py` + `services/revenue_receipts.py` (no analytics yet)
- `routes/revenue.py` with CRUD + receipts
- Minimal "💵 Revenue" tab: flat list with read/write (no sub-tabs, no
  drag-drop, no fancy filters) — proves the data path works
- 8-10 unit tests
- One rolling deploy

### Phase 2 — Workflow + analytics (5-7h)
- Sub-tab UI (Drafts / Sent / Partially paid / Paid / Cancelled)
- Filter bar reusing #93 multi-select pattern
- Detail modal with receipts CRUD + audit trail
- `services/cashflow.py` + `/api/cashflow/*` endpoints
- Cashflow chart card in Analytics tab
- 10-15 unit tests
- One rolling deploy

### Phase 3 — Bank-statement integration + automation (3-4h)
- When bank statement is reconciled, match incoming credits to
  `revenue_documents` (similar to expense matching)
- Auto-mark invoice as paid when matched bank credit ≥ amount
- "Suggest match" UI on reconciliation
- Email notifications when a customer pays (mirror X-alarm pattern)
- 5-8 unit tests
- One rolling deploy

## Open questions for kickoff

1. **Numbering scheme** — do we use sequential per legal entity
   (`ALG-2026-0001`, `A2A-2026-0042`), or import existing numbering?
2. **VAT lines** — store as line items inside `revenue_documents.parsed_json`
   or first-class table?
3. **Multi-currency receipts** — customer pays in different currency than
   invoiced. Store FX-at-receipt or normalize at issue time?
4. **Stripe / payment-processor integration** — webhook in Phase 3, or
   manual reconciliation only?
5. **Reporting standard** — accrual vs cash basis? (Affects which date
   we use for the cashflow chart: invoice issue date vs receipt date.)

## Schema check ✓

Cross-referenced every column against existing `services/db.py` SCHEMA_SQL
to avoid the R102 pitfall (assumed `amount_eur` column that didn't exist).
All new tables are 100% new — no overlap with existing rows.

---

# RU краткое резюме

Сейчас FIO видит только расходы. Этот модуль добавляет **доходную часть**:

- **Proforma → Invoice → Paid** workflow для исходящих счетов
- **Accounts Receivable** регистр (что нам должны клиенты)
- **Cashflow analytics** по streams и периодам (доход − расход)
- Связь с Bank Statement Audit — автоматический match входящих платежей

**3 фазы за 12-16 часов**:
- Phase 1: CRUD API + минимальная таблица (4-5ч)
- Phase 2: workflow stages + аналитика cashflow (5-7ч)
- Phase 3: интеграция с bank reconciliation + Stripe webhook (3-4ч)

5 открытых вопросов на kickoff (номера счетов, НДС, мульти-валюта,
Stripe integration, accrual vs cash). Готово к implementation в
отдельной сессии — design decisions все приняты, осталось только
печатать код.
