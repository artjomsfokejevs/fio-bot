# SOP-FIN-001: Financial Governance Framework

## Document Control

| Field | Value |
|-------|-------|
| Document ID | SOP-FIN-001 |
| Title | Financial Governance Framework — Amitours Holding |
| Version | 1.0 |
| Effective Date | 2026-06-23 |
| Next Review | 2026-08-23 (bi-monthly) |
| Document Owner | Group CFO (interim: Holding Ops Lead — Artjoms Fokejevs) |
| Approver | Holding CEO — Raitis Bullits |
| Classification | Internal — Restricted to Holding CEO, CFO, Stream BOs, Bookkeeper |
| Supersedes | None (initial release) |
| Related Documents | FIO Accounting Bot user guide; BT4YOU Executive Bot org tree; Internal Policies & Limits SOP |

## Revision History

| Version | Date | Author | Summary of Changes |
|---------|------|--------|--------------------|
| 0.1 (draft) | 2026-06-23 | Artjoms Fokejevs | Initial governance framework |
| 0.2 (draft) | 2026-06-23 | Artjoms Fokejevs | CFO profile, bank-API table, WCF proposals, loan inventory, signing officers, bi-monthly review |
| 1.0 | 2026-06-23 | Artjoms Fokejevs | Reformatted as SOP for team distribution |

---

## 1. Purpose

This SOP establishes a formal, automated financial governance framework for
Amitours Holding and its subsidiaries. It moves financial oversight from
fragmented, manual reporting to a single source of truth so leadership can
make data-driven decisions on cash flow, profit-and-loss, and dividend
distribution.

The SOP defines who does what, how often, with which tools, and what to do
when something goes wrong.

## 2. Scope

This SOP applies to:

- The parent entity (Amitours Holding OÜ — PC code AH)
- All operating subsidiaries (AA, AG)
- All venture-portfolio entities (AL, CF, MN, MT, SP, RR)
- Future acquired or incubated entities, from the date of consolidation

It covers:

- Holding structure documentation
- Reporting cadences, approval hierarchies, escalation triggers
- Cash-flow monitoring and automation
- Consolidated profit-and-loss reporting
- Dividend distribution policy, including constraints from loans and guarantees
- Balance-sheet environment for the holding

It does **not** cover:

- Statutory tax filings (separate SOP — owned by external accountant)
- Payroll execution (separate SOP — owned by HR)
- Operational procurement below tier T1 (€1,000) — governed by Internal Policies & Limits

## 3. Definitions

| Term | Definition |
|------|------------|
| Holding | Amitours Holding OÜ — the parent legal entity (PC code AH) |
| Subsidiary | Any legal entity in which the Holding has a controlling stake |
| Venture | A subsidiary in the venture-portfolio (AL, CF, MN, MT, SP, RR) |
| PC code | Profit-Center code — 2-letter identifier per stream (see Appendix A) |
| Stream BO | Business Owner — the accountable executive for a PC |
| FIO | FIO Accounting Bot — the operational ledger and approval workflow system (https://fio-amitours.fly.dev/) |
| BT4YOU Executive Bot | The executive dashboard and org-tree source of truth |
| AR | Accounts Receivable — money customers owe us |
| AP | Accounts Payable — money we owe vendors |
| WCF | Working Capital Floor — minimum cash balance per PC for dividend eligibility |
| Consolidated P&L | Holding-level profit-and-loss with intercompany transactions eliminated |
| X-alarm | Automated alert raised when a stream breaches budget thresholds |
| T1 / T2 / T3 / T4 | Approval tiers by invoice amount: <€1k / <€10k / <€50k / ≥€50k |
| Signing Officer | The person authorised to commit a legal entity to a financial obligation |
| Covenant | A contractual financial ratio required by a lender (e.g. debt-service coverage ≥ 1.2) |

## 4. Roles and Responsibilities

### 4.1 Role definitions

| Role | Primary holder | Backup |
|------|----------------|--------|
| Holding CEO | Raitis Bullits | — |
| Group CFO | TBD (open vacancy) | Holding Ops Lead (Artjoms Fokejevs) until filled |
| Finance Director / Bookkeeper | Rita | — |
| Holding Ops Lead | Artjoms Fokejevs | Holding CEO |
| Stream Business Owners | See Appendix A | Holding CEO |
| IT / Development | Internal team | hold_tech (vacant) |
| Legal | hold_legal (vacant) | Holding CEO + external counsel |

### 4.2 RACI matrix

R = Responsible · A = Accountable · C = Consulted · I = Informed

| Activity | CFO | Bookkeeper | IT | FIO/Bot | Stream BO | Holding CEO |
|---|---|---|---|---|---|---|
| Maintain holding structure documentation | C | R | I | A | C | A |
| Define and maintain this SOP | A | R | C | I | C | A |
| Operate cash-flow automation | A | R | R | A | I | A |
| Maintain consolidated P&L dashboard | A | R | R | A | I | A |
| Maintain dividend policy and calculator | A | R | C | C | C | A |
| Weekly payment-batch approval | C | R | I | A | I | A |
| Stream over-budget escalation response | C | R | I | A | A | C |
| Loan and guarantee covenant tracking | A | R | I | A | I | C |
| Bi-monthly business-model and budget review | A | R | I | A | R | C |
| Quarterly dividend distribution decision | A | C | I | C | C | A |

## 5. Procedures

### 5.1 Procedure 1 — Maintain holding structure (Phase 1)

**Frequency**: continuous; full review at each bi-monthly cycle.
**Owner**: CFO (with IT for sync).
**Trigger**: any change in roles, ventures, or signing officers.

Steps:

1. CFO confirms that `bt4you_snapshot/data/holding_config.json` is the
   authoritative source for the org tree and signing officers.
2. When a venture is added, removed, or its Business Owner changes:
   1. Update BT4YOU Executive Bot first (sync source).
   2. Run the FIO sync job that pulls the updated `holding_config.json`.
   3. Verify the new PC code in `services/pc_codes.py` and FIO Admin → Users.
   4. Notify all Stream BOs of the change.
3. CFO validates the table in **Appendix A** monthly and signs off in the
   document control table.

Records produced: updated `holding_config.json` commit; updated PC code
catalog commit; meeting minutes if structural change was discussed.

### 5.2 Procedure 2 — Reporting cadences (Phase 2)

**Frequency**: as defined in the cadence table.
**Owner**: each report has a named owner (see table).

| Report | Cadence | Owner | Surface | Audience |
|--------|---------|-------|---------|----------|
| Daily cash position | Every weekday 08:00 UTC | Bookkeeper | `/api/cashflow` digest in Slack | Holding CEO, CFO, Holding Ops Lead |
| Weekly payment batch | Every Friday | Bookkeeper | Confirm-for-Payment tab | Holding CEO |
| Monthly stream P&L | 1st business day of next month | Each Stream BO | FIO Analytics tab | Stream BOs, CFO, Holding CEO |
| Monthly X-alarm digest | 1st business day of next month | System (X-alarm) | `/api/xalarm-log?period=YYYY-MM` | CFO, Stream BOs |
| Bi-monthly business-model & budget review | Every 2 months (Feb / Apr / Jun / Aug / Oct / Dec), 5th business day | CFO | Stream Budget editor + memo | Holding CEO, Stream BOs |
| Quarterly consolidated P&L | 10th business day after quarter close | CFO | Consolidated P&L dashboard | Holding CEO, board |
| Quarterly dividend-readiness review | 15th of month after quarter close | CFO | Dividend calculator | Holding CEO |

Steps for each report are defined in the surfacing tool's user guide. The
SOP requires that the report is **produced**, **distributed**, and
**acknowledged** within the cadence; any miss is itself a governance issue
escalated to the CFO and Holding CEO.

### 5.3 Procedure 3 — Bi-monthly business-model and budget review

**Frequency**: every 2 months (Feb / Apr / Jun / Aug / Oct / Dec).
**Owner**: CFO chairs; Stream BOs present; Holding CEO attends.

Steps:

1. **Day −5**: CFO opens a review draft per stream — pre-filled with
   trailing-60-day actuals, plan-vs-actual variance, and current
   `stream_budgets.budget_eur`.
2. **Day −3**: Each Stream BO submits a one-page memo: are model
   assumptions still valid? unit-economics drift? proposed budget reset
   for next 2 months?
3. **Day 0 (5th business day of the cycle month)**: 90-minute review
   meeting; CFO chairs; Holding CEO attends; each Stream BO presents.
   Decision per stream: confirm or revise.
4. **Day +1**: revised budgets posted to FIO Admin → Stream Budgets;
   audit-logged in `stream_budget_history`; memo archived in the
   designated shared drive folder.
5. **Day +2 onward**: X-alarm uses the revised budgets going forward.

Records produced: 6 memos per cycle (one per stream); meeting minutes;
revised `stream_budgets` rows with audit history; updated forecasts.

### 5.4 Procedure 4 — Approval hierarchy and escalation (Phase 2)

**Frequency**: per transaction.
**Owner**: per the tier.

Thresholds (enforced by `services/policy_rules.py`):

| Tier | Amount (EUR per invoice) | Approvers |
|------|--------------------------|-----------|
| T1 | < 1,000 | Stream BO (auto-approved if stream budget OK) |
| T2 | 1,000 – 10,000 | Stream BO + Bookkeeper (Budget OK + Approved by Accounting) |
| T3 | 10,000 – 50,000 | T2 + Holding CEO (Awaiting CEO stage); signing officer of the entity must be on the approval chain |
| T4 | ≥ 50,000 | T3 + written board memo |

Escalation triggers (auto-routed):

| Trigger | Routed to | Channel |
|---------|-----------|---------|
| Stream monthly spend ≥ 100% of budget | Stream BO, Holding Ops Lead, CFO | In-app bell + email |
| Stream monthly spend ≥ 80% of budget | Stream BO, CFO | In-app bell |
| Invoice flagged as policy violation | Bookkeeper, assigned approver | FIO notifications |
| Bank statement: unmatched outflow ≥ €500 at month-close | Stream BO | Asana task auto-created |
| Cashflow projection runway < 90 days | Holding CEO, CFO, Holding Ops Lead | Email + Slack |
| Loan covenant variance | Holding CEO, CFO, Legal | Email |
| Guarantee drawn > 80% of max call | Holding CEO, CFO, Legal | Email |

### 5.5 Procedure 5 — Cash-flow automation (Phase 3)

**Frequency**: implementation one-off; operation continuous.
**Owner**: CFO (sponsor), IT (build), Bookkeeper (operate).

5.5.1 Capabilities already live (do not rebuild):

- Expense capture, bank-statement import + reconciliation, AR receipts,
  revenue cashflow API, bank-credit ↔ revenue auto-match, stream
  budgets + X-alarm, Confirm-for-Payment 4-stage workflow.

5.5.2 Gaps to close (build steps):

1. **13-week cash-flow projection** — IT to build
   `services/cashflow_projection.py` combining AR pipeline (by `due_date`),
   AP pipeline (by `desired_payment_date`), recurring payroll and rent,
   opening bank balance. Endpoint `/api/cashflow/projection?weeks=13&pc=ALL`.
2. **Bank balance import** — IT to create `bank_account_balances` table +
   daily import job using bank APIs (see **Appendix B**).
3. **Inter-company elimination** — IT + CFO to design
   `services/intercompany.py`; integrate with cashflow and consolidated P&L.
4. **Payroll integration** — Bookkeeper + IT to wire salary documents into
   the cashflow stream so projections include payroll.
5. **Daily Slack digest** — IT to schedule `/api/cashflow/digest` cron at
   08:00 UTC posting to `#fio-cashflow`.
6. **FX roll-forward** — IT to extend FX module to roll ECB rates forward
   to projected dates; raise alert if FX exposure > 10% of revenue.

5.5.3 Acceptance criteria for Phase 3 completion:

- Projection vs. actual reconciles within ±5% for the trailing 4 weeks.
- Daily digest landing in Slack and acknowledged by Holding CEO.
- Auto-X-alarm fires correctly for at least one synthetic test per stream.

### 5.6 Procedure 6 — Consolidated P&L dashboard (Phase 4)

**Frequency**: implementation one-off; operation continuous.
**Owner**: CFO (sponsor), IT (build), Bookkeeper (validate).

Dependencies: Phase 3 elimination data; insurance, grocery, and transfer
margin models finalised; intercompany flagging in `documents`.

Steps:

1. IT adds `counterparty_pc` column to `documents` for elimination flagging.
2. IT builds endpoint `/api/pnl/consolidated?period=...` returning raw,
   eliminations, and consolidated revenue / expense / net per stream and
   per ledger group.
3. IT adds **Consolidated P&L** card to FIO Analytics tab: 4-tile KPI
   strip (revenue / expense / net / margin %), Raw ⇄ Eliminated toggle,
   per-stream table with drill-down, quarterly trend chart, export to
   XLSX and PDF.
4. CFO (with Bookkeeper) reconciles Q1 2026 against manual close;
   must agree within ±0.5%.
5. CFO sign-off → dashboard role-gated for `admin`, `holding_ceo`, `cfo`
   (full view); `bookkeeper` (full view, no eliminations toggle);
   `stream_owner` (own stream only); `viewer` (no access).

### 5.7 Procedure 7 — Dividend distribution (Phase 5)

**Frequency**: quarterly.
**Owner**: CFO (calculation), Holding CEO (decision), Legal (covenant interpretation).

5.7.1 Eligibility test (per venture, per quarter):

A venture is dividend-eligible only if **all six** conditions hold:

1. consolidated_net (PC, quarter) > 0
2. trailing_4q_consolidated_net (PC) > 0
3. cash_balance (PC, end-of-quarter) ≥ working_capital_floor (PC) — see **Appendix C**
4. No active loan-covenant breach (see **Appendix D**)
5. No active guarantee-secured obligation drawn > 80% of max call
6. Stream BO and Holding CEO have signed off statutory accounts for the period

If any condition fails → max dividend = 0 and reason is recorded.

5.7.2 Max-dividend formula (per eligible venture):

```
free_cash         = cash_balance(pc, eop) − working_capital_floor(pc)
distributable     = min(consolidated_net(pc, quarter), free_cash)
loan_buffer       = sum(loan_principal_due_next_4q(pc)) × 1.25
guarantee_buffer  = sum(guarantee_max_call(pc)) × 0.50
max_dividend(pc)  = max(0, distributable − loan_buffer − guarantee_buffer)
```

Holding's max-dividend (to shareholders) = sum across eligible PCs **minus**
holding-level commitments (parent-company loans, holding-level guarantees).

5.7.3 Workflow:

1. **Day +15 after quarter close**: Bot auto-computes the eligibility
   table and max-dividend per stream; emails to Holding CEO and CFO;
   creates Asana task in "Quarterly Close" project.
2. CFO reviews with Legal (covenant interpretation) and Bookkeeper
   (cash and accounts validation).
3. **Holding CEO** posts the decision in the Dividend calculator UI:
   actual dividend per stream (must be ≤ max). Decision is audit-logged
   with timestamp, actor, memo.
4. **Bookkeeper** executes transfer via Confirm-for-Payment T4 (board
   memo required).
5. Receipt at AH is logged as a `revenue_receipts` row against the
   AH-internal "Dividend in" document.

### 5.8 Procedure 8 — Loan and guarantee inventory (supports Phase 5)

**Frequency**: full sweep once on go-live; updated on every new agreement.
**Owner**: CFO; Stream BOs supply; Legal validates.

Steps:

1. CFO drafts a one-page request per Stream BO and the Holding CEO:
   *"List every active loan agreement (term loan, line of credit,
   intercompany loan, related-party loan), every guarantee issued by
   your entity (bank guarantee, parent-company guarantee, surety,
   letter of credit), and every guarantee received. For each provide:
   counterparty, agreement reference, principal or max-call, current
   outstanding, maturity, covenants, scanned PDF of the signed
   agreement."*
2. Each Stream BO replies within 5 business days with data and PDFs.
3. CFO uploads each scanned signed agreement PDF to the Balance Sheet
   environment (see **Procedure 9**) and records the path in
   `agreement_file`.
4. CFO populates `loans` and `guarantees` tables via the new
   Admin → Loans & Guarantees UI (or `scripts/seed_obligations.py`).
5. CFO records covenant ratios in `covenants_json`.
6. Bot recomputes `covenant_checks` quarterly from FIO data and the
   consolidated P&L; any breach triggers an X-alarm to Holding CEO,
   CFO, Legal.

Schemas:

```sql
CREATE TABLE loans (
  id TEXT PRIMARY KEY,
  pc TEXT NOT NULL,
  counterparty TEXT NOT NULL,
  agreement_ref TEXT,
  agreement_file TEXT,
  principal_eur REAL NOT NULL,
  outstanding_eur REAL NOT NULL,
  interest_rate REAL,
  start_date TEXT,
  maturity_date TEXT,
  next_payment_date TEXT,
  next_payment_amount_eur REAL,
  covenants_json TEXT,
  collateral_description TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT, created_by TEXT
);

CREATE TABLE guarantees (
  id TEXT PRIMARY KEY,
  pc TEXT NOT NULL,
  beneficiary TEXT NOT NULL,
  agreement_ref TEXT,
  agreement_file TEXT,
  max_call_amount_eur REAL NOT NULL,
  current_exposure_eur REAL,
  start_date TEXT,
  expiry_date TEXT,
  type TEXT NOT NULL,
  underlying_obligation TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT, created_by TEXT
);

CREATE TABLE covenant_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loan_id TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  ratio TEXT NOT NULL,
  actual_value REAL,
  threshold REAL,
  passed INTEGER NOT NULL,
  notes TEXT,
  FOREIGN KEY (loan_id) REFERENCES loans(id) ON DELETE CASCADE
);
```

### 5.9 Procedure 9 — Balance Sheet environment (Phase 5b)

**Frequency**: extension build one-off; data maintained continuously.
**Owner**: CFO (data), IT (build).

The Balance Sheet is a thin extension of FIO, not a separate system.

| Component | Where it lives | Owner |
|-----------|----------------|-------|
| Cash and bank balances | `bank_account_balances` (new — Procedure 5) | CFO + Bookkeeper |
| Accounts Receivable | `revenue_documents` + `revenue_receipts` (live) | Bookkeeper |
| Accounts Payable | `documents` + `partial_payments` (live) | Bookkeeper |
| Loans payable | `loans` table (Procedure 8) | CFO |
| Guarantees | `guarantees` table (Procedure 8) | CFO |
| Fixed assets | `fixed_assets` table (new — Phase 5b) | CFO + Bookkeeper |
| Equity movements | `equity_movements` table (new — Phase 5b) | CFO |
| Signed agreements (PDF archive) | FIO file volume or cloud bucket; path referenced in `loans.agreement_file` / `guarantees.agreement_file` | CFO |
| Consolidated Balance Sheet view | `/api/balance-sheet/consolidated?as_of=YYYY-MM-DD` (new endpoint) | CFO + IT |

## 6. Forms and Tools

| Form / Tool | Purpose | Where it lives |
|-------------|---------|----------------|
| FIO Accounting Bot | Operational ledger and approval workflow | https://fio-amitours.fly.dev/ |
| BT4YOU Executive Bot | Org-tree source of truth | Internal app |
| Stream Budget editor | Admin tab in FIO; bi-monthly review tool | FIO Admin → Stream Budgets |
| Internal Policies & Limits | Threshold-editing UI | FIO Admin → Internal Policies & Limits |
| Confirm-for-Payment | Approval workflow with sub-stages | FIO main tab |
| Bank Statement Audit | Reconciliation and chase tasks | FIO main tab |
| Revenue (AR) tab | Proforma / invoice / receipts workflow | FIO main tab |
| Cashflow dashboard | Monthly and projected cashflow | FIO Analytics tab |
| Dividend calculator | Quarterly max-dividend calc and decision log | FIO Admin (to be built — Phase 5) |
| Loans & Guarantees UI | Inventory and covenant tracker | FIO Admin (to be built — Procedure 8) |
| Asana "Quarterly Close" project | Workflow checklist for each quarterly close | Asana |
| Slack `#fio-cashflow` | Daily digest channel | Slack |
| Shared drive folder "Holding-Finance" | Memos, scanned agreements, board packs | Google Drive (or equivalent) |

## 7. Records and Retention

| Record type | Owner | Retention |
|-------------|-------|-----------|
| FIO database (all transactional records) | IT | 10 years minimum |
| Bi-monthly review memos | CFO | 10 years |
| Quarterly close packs (P&L, cashflow, dividend decision) | CFO | 10 years |
| Loan agreements (scanned PDF) | CFO + Legal | Lifetime of agreement + 10 years |
| Guarantee agreements (scanned PDF) | CFO + Legal | Lifetime of agreement + 10 years |
| Covenant check history | IT (in `covenant_checks` table) | 10 years |
| Bank API access credentials | CFO | Rotated every 90 days; previous never retained |
| Audit logs (FIO actions) | IT (in `audit_log` table) | 10 years |
| Board memos for T4 approvals | Holding CEO | Lifetime + 10 years |

## 8. Exceptions and Escalations

| Situation | Action | Escalation path |
|-----------|--------|------------------|
| FIO outage during weekly payment batch | Bookkeeper executes via fallback bank UI; logs in FIO once restored | IT (immediate); Holding CEO (if outage > 4h) |
| Stream BO unavailable for approval at month-close | Holding CEO can act as backup approver | Logged automatically |
| Loan covenant breach detected | Stop dividend distribution for that PC; CFO and Legal review remedies (waiver, refinance, prepayment) | Holding CEO same-day |
| Bank API credentials compromised | CFO immediately revokes; rotates; logs incident | Holding CEO + IT |
| Discrepancy ≥ 0.5% between FIO consolidated P&L and Bookkeeper manual close | Block dashboard sign-off until reconciled | CFO leads; involves IT if data issue |
| Dividend distribution proposed above max calculated | Holding CEO must justify in writing; Legal opines on covenant impact; board memo required | Board (formal) |
| Late report (any cadence in §5.2) | Owner explains in next bi-monthly review | CFO compiles; Holding CEO reviews trend |

## 9. References

- BT4YOU Executive Bot org tree: `bt4you_snapshot/data/holding_config.json`
- FIO PC code catalog: `services/pc_codes.py`
- FIO stream budgets: `services/stream_budgets.py`, `routes/budgets.py`
- FIO X-alarm: `services/xalarm.py`
- FIO cashflow API: `services/cashflow.py`, `/api/cashflow`
- FIO internal policies: `services/policy_rules.py`
- FIO users / signing officers: `fio_users` table, Admin → Users
- Pattern catalog (PRODUCT_BUILDER): P118 (canonical/legacy code translation), P121 (SQLite FK cascade pitfall), P122 (backend-first sweep for multi-phase modules)

---

# Appendix A — PC code catalog and Signing Officers

| PC | Stream | Business Owner | Holding Parent | Status | Primary Signing Officer | Co-signer (≥ €25k) |
|----|--------|-----------------|----------------|--------|--------------------------|---------------------|
| AA | Alps2Alps | Artjoms Fokejevs | hold_ops_lead | Operating | Artjoms Fokejevs | Raitis Bullits |
| AH | Amitours Holding OÜ | Raitis Bullits | — | Operating | Raitis Bullits | Artjoms Fokejevs |
| AG | Amitours Group | Artjoms Fokejevs | hold_ops_lead | Operating | Artjoms Fokejevs | Raitis Bullits |
| AL | ALVEDA | Evgeny Kuryshev | vs_root | Venture | Evgeny Kuryshev | Raitis Bullits |
| CF | MyPeak Finance | Rihards S. | vs_root | Venture | Rihards S. | Raitis Bullits |
| MN | Mountly | Katia Sogreeva-Peyron | vs_root | Venture | Katia Sogreeva-Peyron | Raitis Bullits |
| MT | Medical Travel | TBD | vs_root | Venture | TBD | Raitis Bullits |
| SP | Skipasser | Serge Cymbal | vs_root | Venture | Serge Cymbal | Raitis Bullits |
| RR | Rock2Rock | TBD | vs_root | New venture | TBD | Raitis Bullits |

Legacy PC codes still allowed in historical filters but not selectable in new
records: SR → SP; BK (decommissioned 2026-06-16); PK → CF.

---

# Appendix B — Bank API access inventory

CFO owns this inventory. IT implements once credentials are stored in
`flyctl secrets` for the FIO app.

| Bank / Provider | Entities | Credentials needed | Auth method | Data pulled | Rate limit | Credential owner |
|-----------------|----------|---------------------|-------------|-------------|------------|------------------|
| Mercury (US business banking) | AA, AH | API token + Workspace ID | Bearer token | Accounts, balances (daily), transactions (incremental), wire status | 100 req/min | Holding Ops Lead → CFO |
| Revolut Business | AA, AG, AL, SP | API certificate + private key + OAuth client_id + client_secret | OAuth2 + JWT | Balances, statements, counterparties, FX history | 120 req/min per app | Holding Ops Lead → CFO |
| LHV Pank (Estonia) | AH, MN | XML API key + AccountAdminAccess | Mutual TLS + API key | Daily statements (XML), balance, payment status | 60 req/min | Bookkeeper → CFO |
| Swedbank Latvia | AA, CF | Connect API token (PSD2 Open Banking) | OAuth2 + eIDAS cert | Account info, balance, transactions | 4 req/sec | Bookkeeper |
| Wise Business | Multiple (FX) | API token | Bearer token | Multi-currency balances, transfers, FX rates | 100 req/min | Holding Ops Lead |
| Stripe (revenue side) | AA, SP, MN, AL | Restricted API key (read-only) | Bearer token | Charges, payouts | 100 req/sec | Each Stream BO |
| Paddle (revenue side, MyPeak) | CF | API key | Bearer token | Transactions, subscriptions | 30 req/sec | Stream BO (Rihards) |

**Collection procedure** (per Procedure 5):

1. CFO emails the relevant Signing Officer with the row above.
2. Signing Officer generates credentials in the bank portal; delivers via
   1Password Secure Share, Signal, or in person — never plain email,
   never Slack DM.
3. CFO posts to Fly via
   `flyctl secrets set BANK_<PROVIDER>_<ENTITY>_TOKEN=… -a fio-amitours`.
4. IT runs `/api/admin/bank-secret-check` to verify FIO can read it.
5. IT enables the daily sync; Bookkeeper validates against a manual
   balance check.
6. Credentials rotated every 90 days; calendar reminder on Holding CEO.

---

# Appendix C — Working Capital Floor proposals

Default formula:

```
WCF(pc) = max(
    90_days_of_OPEX(pc),
    90_days_of_payroll(pc),
    next_4q_loan_repayments(pc),
    minimum_per_PC
)
```

Initial `minimum_per_PC` proposals — to be confirmed by CFO + each Stream
BO in writing during the CFO's first 90-day mandate:

| PC | Stream | Initial WCF (EUR) | Rationale |
|----|--------|-------------------:|-----------|
| AA | Alps2Alps | 400,000 | Largest payroll; seasonal Q1/Q4 swings; transfer-supplier prepayments |
| AH | Amitours Holding OÜ | 150,000 | Holding-level legal / tax / committee costs; no revenue buffer |
| AG | Amitours Group | 80,000 | Shared-services bucket; cost rebilled to streams |
| AL | ALVEDA | 120,000 | Doctor-network commitments; content-team payroll |
| CF | MyPeak Finance | 60,000 | Lean team; main risk is regulatory licence fees |
| MN | Mountly | 150,000 | BD team across 4 markets; SaaS infra |
| MT | Medical Travel | 40,000 | Early-stage; revisit when run-rate > €100k/mo |
| SP | Skipasser | 180,000 | Multi-region BD team + dev + UX; pre-revenue scaling |
| RR | Rock2Rock | 30,000 | Earliest-stage; will rise with revenue |
| Holding-wide buffer | — | 300,000 | Held at AH; covers cross-venture emergency injection |

Reviewed at each bi-monthly cycle; revised in writing only with CFO +
Holding CEO sign-off.

---

# Appendix D — Loan and Guarantee inventory template

Use this template when seeding the `loans` and `guarantees` tables for the
first time, and when adding new agreements.

**Loan record (one row per active agreement):**

| Field | Example | Required |
|-------|---------|----------|
| pc | AA | yes |
| counterparty | LHV Pank | yes |
| agreement_ref | LHV-2025-LN-014 | yes |
| agreement_file | /agreements/loans/lhv-2025-014.pdf | yes |
| principal_eur | 500000.00 | yes |
| outstanding_eur | 320000.00 | yes |
| interest_rate | 0.045 | yes |
| start_date | 2025-03-01 | yes |
| maturity_date | 2028-02-28 | yes |
| next_payment_date | 2026-07-15 | yes |
| next_payment_amount_eur | 14500.00 | yes |
| covenants_json | {"min_debt_service_coverage": 1.2, "max_leverage": 3.0, "reporting": "quarterly"} | yes if any |
| collateral_description | Receivables pledge over A2A booking accounts | optional |
| status | active | yes |

**Guarantee record (one row per active obligation):**

| Field | Example | Required |
|-------|---------|----------|
| pc | AH | yes |
| beneficiary | Estonian Tax Authority | yes |
| agreement_ref | EE-GUAR-2024-007 | yes |
| agreement_file | /agreements/guarantees/ee-2024-007.pdf | yes |
| max_call_amount_eur | 150000.00 | yes |
| current_exposure_eur | 0.00 | yes |
| start_date | 2024-09-01 | yes |
| expiry_date | 2027-08-31 | yes |
| type | parent_company_guarantee | yes |
| underlying_obligation | Operating-license bond for ALVEDA Estonian operations | yes |
| status | active | yes |

---

# Appendix E — CFO profile (target hire)

| Dimension | Target |
|-----------|--------|
| Title | Group CFO — Amitours Holding |
| Reports to | Holding CEO (Raitis Bullits) |
| Direct reports | Bookkeeper (Rita), future FP&A analyst, future Treasury controller |
| Engagement model | Fractional (3 days / week) acceptable for first 6 months; full-time when consolidated revenue exceeds €5M run-rate |
| Base location | Riga or Tallinn preferred; remote with monthly on-site acceptable |

**Required experience**

- 8+ years in finance leadership at a group / holding (not single-entity)
- Hands-on consolidation under IFRS or local GAAP across multiple legal entities (Estonia OÜ, Latvia SIA at minimum)
- Multi-currency treasury and cash-flow forecasting (EUR, USD, CHF, HUF)
- Venture-portfolio finance: shared-services charge-back, intercompany elimination, parent-company guarantees, dividend up-stream timing
- Prior CFO or Finance Director role in travel / mobility, fintech, SaaS, or marketplace
- Lender / bank-covenant negotiation experience

**Required capabilities**

- Builds 13-week rolling cash forecasts and 3-statement models without an analyst
- Reads SQL well enough to validate FIO outputs and write basic `SELECT … GROUP BY` queries
- Comfortable with low-code tooling (Retool, Looker, Metabase) — will own the consolidated P&L dashboard
- Working knowledge of at least one bank API (Mercury, Revolut Business, LHV)
- Negotiation: can run a term-sheet negotiation with EU banks for ventures (€1M–€5M facility tickets)

**Soft signals**

- Bias toward automation over headcount
- Comfortable challenging the CEO on capital-allocation decisions
- Has shipped at least one finance-tooling rollout that the operating team actually adopted

**First-90-day mandate**

| Week | Deliverable |
|------|-------------|
| 1–2 | Review FIO and this SOP; meet each Stream BO; produce gap list |
| 3–4 | Take ownership of `loans` + `guarantees` inventory (Procedure 8); seed the tables |
| 5–6 | Sign off Phase 3 cashflow projection model; validate against trailing 12 weeks |
| 7–8 | Sign off Phase 4 consolidated P&L; reconcile Q1 + Q2 2026 |
| 9–10 | Negotiate Working Capital Floor per PC; lock in writing |
| 11–12 | First Phase 5 dividend-readiness review using live tooling |

**Compensation framing**

- Base salary: €90k–€140k full-time, pro-rated for fractional
- Equity / phantom equity: not applicable at holding level today
- KPI bonus tied to: zero stale months in consolidated P&L close; cashflow forecast accuracy ±5% at 4-week horizon; covenant compliance across all venture loans; max-dividend signed off on schedule each quarter

---

# Appendix F — Implementation roadmap

| Phase | Effort | Dependencies | Target |
|-------|--------|--------------|--------|
| 0 — CFO hire | 6–10 weeks search | Holding CEO sign-off on profile (Appendix E) | 2026-Q3 |
| 1 — Structure documentation | 2 h | None | This week |
| 2 — Governance framework (this SOP) | 4 h + sign-off | Phase 1 | Next 2 weeks |
| 3 — Cash-flow automation | 16–20 h | Bank API access (Appendix B), Slack channel | 2026-Q3 |
| 4 — Consolidated P&L dashboard | 12–16 h | Phase 3 elimination data; insurance / grocery / transfer margin models finalised | 2026-Q3 / Q4 |
| 5 — Dividend policy + calculator | 16–20 h | Phase 4; Legal review of covenants; CFO in seat | 2026-Q4 (in time for 2026-Q3 close run) |
| 5b — Balance Sheet extension | 12–16 h | Phase 5 | 2027-Q1 |

---

# Appendix G — Open questions for kickoff

1. CFO hire — start search now? engage a recruiter? fractional or full-time first 6 months?
2. Bank API access — confirmed list per Appendix B; CFO (or Holding Ops Lead until CFO seated) to collect credentials.
3. Working Capital Floor — initial proposals in Appendix C ready for Stream BO negotiation; approval by CFO + Holding CEO in writing.
4. Loan inventory — collection procedure in Procedure 8; deadline 30 days after CFO start or 30 days after this SOP is approved if no CFO yet.
5. Guarantee inventory — same procedure as loans; CFO + Legal jointly responsible.
6. Statutory accounts sign-off — signing officers per Appendix A; CFO validates monthly.
7. Reporting currency — confirm EUR-only is acceptable; ventures with USD / CHF cost bases may prefer alt-reporting.

---

## Sign-off

| Name | Role | Signature | Date |
|------|------|-----------|------|
| Raitis Bullits | Holding CEO (Approver) | _________________________ | __________ |
| Artjoms Fokejevs | Holding Ops Lead (interim Document Owner) | _________________________ | __________ |
| Rita | Bookkeeper (Reviewer) | _________________________ | __________ |
| TBD | Group CFO (Document Owner once hired) | _________________________ | __________ |

*End of SOP-FIN-001 v1.0*
