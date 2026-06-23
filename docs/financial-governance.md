# Amitours Holding — Financial Governance Framework

> **Status**: Draft v2 · 2026-06-23 · Author: Artjoms Fokejevs (Holding Ops Lead) · Approver: Raitis Bullits (Holding CEO)
>
> **Purpose**: Move holding-level financial oversight from fragmented / manual
> reporting to a single automated source of truth. Closes the gap surfaced in
> the executive-committee discussion (minutes 71–75) on holding P&L,
> max-dividend, and cash-flow governance.
>
> **Source of truth for org structure**: `bt4you_snapshot/data/holding_config.json`
> (synced from BT4YOU Executive Bot).
>
> **Source of truth for financial transactions**: FIO Accounting Bot
> (`https://fio-amitours.fly.dev/`).
>
> **v2 changes vs v1**: added CFO profile (§0), expanded bank API table (§3.4),
> concrete Working Capital Floor proposals per PC (§5.3), full loan/guarantee
> inventory procedure with the Balance Sheet environment (§5.7–5.8), explicit
> Signing Officer link to FIO Admin → fio_users (§1.5), bi-monthly
> business-model + budget review cadence (§2.1).

---

## 0. CFO Profile — Target Hire

The framework below assumes a CFO role exists. Today the function is split
between the Holding CEO (Raitis), hold_ops_lead (Artjoms), and the Bookkeeper
(Rita). A dedicated CFO is the single accountable owner of this entire
framework once hired. The vacancy is the largest single risk to delivery.

### 0.1 Profile

| Dimension | Target |
|-----------|--------|
| **Title** | Group CFO — Amitours Holding |
| **Reports to** | Holding CEO (Raitis Bullits) |
| **Direct reports** | Finance Director / Bookkeeper (Rita today), future FP&A analyst, future Treasury controller |
| **Engagement model** | Fractional (3 days / week) acceptable for first 6 months; full-time when consolidated revenue exceeds €5M run-rate |
| **Base location** | Riga or Tallinn preferred; remote with monthly on-site acceptable |

### 0.2 Required experience

- 8+ years in finance leadership at a group / holding (not single-entity)
- Hands-on consolidation experience under IFRS or local GAAP across **multiple legal entities** (Estonia OÜ, Latvia SIA at minimum)
- Treasury and cash-flow forecasting at multi-currency operations (EUR, USD, CHF, HUF)
- Experience with **venture-portfolio finance**: shared services charge-back, intercompany elimination, parent-company guarantees, dividend up-stream timing
- Prior CFO or Finance Director role in at least one of: travel / mobility, fintech, SaaS, marketplace
- Lender / bank-covenant negotiation experience (refinance, term-sheet review, debt-service coverage modelling)

### 0.3 Required capabilities

- Builds 13-week rolling cash forecasts and 3-statement models in Google Sheets / Excel without an analyst
- Reads SQL well enough to validate FIO outputs against the source data; can write basic `SELECT … GROUP BY` against the FIO DB
- Comfortable with low-code tooling (Retool, Looker, Metabase) — will own the consolidated P&L dashboard
- Working knowledge of one bank API (Mercury, Revolut Business, or LHV) — sufficient to validate integrations IT builds
- Negotiation: can run a term-sheet negotiation with EU banks for ventures (€1M–€5M facility tickets)

### 0.4 Soft signals

- Bias toward automation over headcount — proposes "let FIO do it" before "hire an analyst"
- Comfortable challenging the CEO on capital-allocation decisions
- Has shipped at least one finance-tooling rollout that the operating team actually adopted (we don't want a CFO who lives in Excel-only)

### 0.5 First-90-day mandate

| Week | Deliverable |
|------|-------------|
| 1–2 | Review FIO + this document; meet each Stream BO; produce gap list |
| 3–4 | Take ownership of `loans` + `guarantees` inventory (§5.7); seed the tables |
| 5–6 | Sign off Phase 3 cashflow projection model (§3.3); validate against trailing 12 weeks |
| 7–8 | Sign off Phase 4 consolidated P&L (§4); reconcile Q1 + Q2 2026 against bookkeeper close |
| 9–10 | Negotiate working-capital floor per PC (§5.3) with each Stream BO; lock in writing |
| 11–12 | First Phase 5 dividend-readiness review (§5.5) using live tooling |

### 0.6 Compensation framing

- Base salary benchmark: €90k–€140k full-time, pro-rated for fractional
- Equity / phantom equity: not applicable at holding level today; revisit with first external funding round
- KPI bonus tied to: (a) zero stale months in consolidated P&L close, (b) cashflow forecast accuracy ±5 % at 4-week horizon, (c) covenant compliance across all venture loans, (d) max-dividend signed off on schedule each quarter

---

## 1. Phase 1 — Holding Structure Documentation

### 1.1 Holding tree (as registered in `holding_config.json`)

```
Amitours Holding OÜ (AH)
└── Raitis Bullits — Holding CEO
    ├── Diana Beisenova — PA / Executive Committee Coordinator
    ├── VACANT — Fractional MD (business-stream exec management)
    ├── Artjoms Fokejevs — Ops · Product · Finance · HR · Administration (hold_ops_lead)
    │   └── A2A CEO (operating execution as A2A CEO)
    ├── Denis Elkin — Group CMO (hold_marketing)
    ├── — — Group CTO (hold_tech, VACANT)
    ├── — — Legal · Compliance · M&A (hold_legal, VACANT)
    └── Venture Studio (vs_root)
        ├── Evgeny Kuryshev — ALVEDA Business Owner (vs_alveda)
        ├── Serge Cymbal — Skipasser Business Owner (vs_skipasser)
        ├── Rihards S. — MyPeak Finance Business Owner (vs_mypeak)
        └── Katia Sogreeva-Peyron — Mountly Super App Business Owner (vs_mountly)
```

### 1.2 Profit-center → legal-entity → business-owner map

| PC | Stream | Business Owner (BO) | Holding Parent | Status |
|----|--------|---------------------|----------------|--------|
| **AA** | Alps2Alps (core transfer business) | Artjoms Fokejevs | hold_ops_lead | Operating |
| **AH** | Amitours Holding OÜ (parent entity) | Raitis Bullits | — | Operating |
| **AG** | Amitours Group (shared ops bucket) | Artjoms Fokejevs | hold_ops_lead | Operating |
| **AL** | ALVEDA (Medical Travel platform) | Evgeny Kuryshev | vs_root | Venture |
| **CF** | MyPeak Finance | Rihards S. | vs_root | Venture |
| **MN** | Mountly (Super App) | Katia Sogreeva-Peyron | vs_root | Venture |
| **MT** | Medical Travel (separate from ALVEDA) | TBD | vs_root | Venture |
| **SP** | Skipasser | Serge Cymbal | vs_root | Venture |
| **RR** | Rock2Rock | TBD | vs_root | New venture |

**Legacy codes (read-time only — not in pickers)**: SR→SP, BK (Skibookers, decommissioned 2026-06-16), PK→CF. Translation handled centrally in `services/pc_codes.py`.

### 1.3 Inter-stream financial relationships

| Relationship | Description | Tracked in |
|--------------|-------------|------------|
| **AH → ventures** | Holding parent receives dividends; provides intercompany loans / cash injections | `documents.is_internal=1` + `documents.legal_entity` |
| **AG → ventures** | Shared services (legal, marketing, IT) charged out to streams | Allocations table (`documents.allocations` JSON) |
| **AA ↔ ventures** | Operational cross-sell (transfer-bookings → Skipasser ski-pass attach) | Not yet ledger-tracked |
| **Ventures → AH** | Profit distribution (subject to dividend policy — §5) | Not yet automated — manual Q4 calc today |

**Gap**: today inter-company transactions are recorded but not **eliminated** in a consolidated view. Phase 4 dashboard must implement elimination entries.

### 1.4 Action items — Phase 1

- [x] **AI-1.1** Pull stream/role tree from `bt4you_snapshot/data/holding_config.json`
- [ ] **AI-1.2** Document each venture's legal-entity registration number (Estonia OÜ, Latvia SIA, etc.) — owner: Legal (hold_legal vacancy, falls back to hold_ops_lead)
- [ ] **AI-1.3** Map each PC code to its bank accounts in FIO Admin → Paying Accounts → owner: Bookkeeper (Rita)
- [ ] **AI-1.4** Confirm Rock2Rock (RR) and Medical Travel (MT) BO assignments — owner: Holding CEO
- [ ] **AI-1.5** Backfill `documents.legal_entity` for historical rows where missing — owner: Bookkeeper, script: `scripts/backfill_legal_entity.py`

### 1.5 Signing Officers per Entity (already in FIO)

Signing officers are derived from **FIO Admin → fio_users** (`services/roles.py` + the `fio_users` table). The Bookkeeper maintains this list; the CFO validates it monthly.

| Entity | Signing Officer (operating) | Co-signer for banking ≥ €25k |
|--------|------------------------------|------------------------------|
| Amitours Holding OÜ (AH) | Raitis Bullits | Artjoms Fokejevs |
| Alps2Alps (AA — Latvia SIA) | Artjoms Fokejevs | Raitis Bullits |
| Amitours Group (AG) | Artjoms Fokejevs | Raitis Bullits |
| ALVEDA (AL) | Evgeny Kuryshev | Raitis Bullits |
| MyPeak Finance (CF) | Rihards S. | Raitis Bullits |
| Mountly (MN) | Katia Sogreeva-Peyron | Raitis Bullits |
| Medical Travel (MT) | TBD | Raitis Bullits |
| Skipasser (SP) | Serge Cymbal | Raitis Bullits |
| Rock2Rock (RR) | TBD | Raitis Bullits |

**Validation rule**: any approval at tier T3 (≥ €10k) requires the signing officer of that entity to be the approver in FIO; the system enforces this via `services/roles.py` role + profit_center match.

---

## 2. Phase 2 — Financial Governance Framework

### 2.1 Reporting cadences

| Report | Cadence | Owner | Surface in FIO | Audience |
|--------|---------|-------|----------------|----------|
| **Daily cash position** | Every weekday morning | Bookkeeper | `/api/cashflow?from=…&to=…` | Holding CEO, hold_ops_lead, CFO |
| **Weekly payment batch** | Friday | Bookkeeper | Confirm-for-Payment tab → CEO approves → Bookkeeper executes | Holding CEO |
| **Monthly stream P&L** | 1st business day of next month | Each Stream BO | Analytics tab (per-PC drill) + Bookkeeper close checklist | All Stream BOs + Holding CEO + CFO |
| **Monthly X-alarm digest** | 1st business day of next month | System (X-alarm) → Stream BO + CFO | `/api/xalarm-log?period=YYYY-MM` | Holding CEO, Stream BOs, CFO |
| **Bi-monthly business-model & budget review** *(new in v2)* | Every 2 calendar months, 5th business day | CFO + each Stream BO | New: business-model review template + Stream Budget editor | Holding CEO, all Stream BOs, CFO |
| **Quarterly consolidated P&L** | 10th business day after quarter close | CFO (with hold_ops_lead) | New dashboard (Phase 4) | Holding CEO, board, CFO |
| **Quarterly dividend-readiness review** | 15th of month after quarter close | CFO + Legal | Dividend calculator (Phase 5) | Holding CEO, CFO |

#### 2.1.1 Bi-monthly business-model & budget review — procedure

Every 2 months (Feb / Apr / Jun / Aug / Oct / Dec):

1. **Day −5**: CFO opens a review draft per stream — pre-filled with trailing-60-day actuals, plan-vs-actual variance, and current `stream_budgets.budget_eur`.
2. **Day −3**: Each Stream BO submits a one-page memo: model assumptions still valid? unit-economics drift? proposed budget reset for next 2 months?
3. **Day 0 (5th business day of cycle month)**: 90-min review meeting — CFO chairs, Holding CEO attends, Stream BOs present. Decision: confirm / revise budget per stream.
4. **Day +1**: revised budgets posted to FIO Admin → Stream Budgets (audit-logged in `stream_budget_history`); memo archived in shared drive.
5. **Day +2 → next cycle**: X-alarm uses the revised budgets going forward.

### 2.2 RACI matrix (5-phase delivery)

| Activity | CFO | Bookkeeper / FD | IT / Dev | Bot (FIO + BT4YOU) | Stream BO | Holding CEO |
|---|---|---|---|---|---|---|
| Phase 1 — Structure documentation | C | R | I | A (sync from holding_config) | C | A |
| Phase 2 — Governance framework | A | R | C | I | C | A |
| Phase 3 — Cash flow automation | A | R | R | A (FIO + cashflow endpoints) | I | A |
| Phase 4 — Consolidated P&L dashboard | A | R | R | A (FIO Analytics extension) | I | A |
| Phase 5 — Dividend policy + calculator | A | R | C | C (FIO posts to bot) | C | A |
| Weekly payment batch approval | C | R | I | A (FIO Confirm-for-Payment) | I | A |
| Stream-level over-budget escalation | C | R | I | A (X-alarm) | A | C |
| Loan / guarantee covenant tracking | A | R | I | new module (§5.7) | I | C |
| Bi-monthly business-model & budget review | A | R | I | A (Stream Budgets + Analytics) | R | C |

A = Accountable · R = Responsible · C = Consulted · I = Informed.

### 2.3 Approval hierarchy

Spending thresholds (already enforced by `services/policy_rules.py`):

| Tier | Threshold (EUR / invoice) | Who approves |
|------|---------------------------|--------------|
| **T1** | < €1,000 | Stream BO (auto-approved if budget OK) |
| **T2** | €1,000 – €10,000 | Stream BO + Bookkeeper (Budget OK + Approved by Accounting) |
| **T3** | €10,000 – €50,000 | T2 + Holding CEO (Awaiting CEO stage); signing officer of entity must be on the approval chain |
| **T4** | > €50,000 | T3 + written board memo (manual, off-FIO) |

Editable in FIO Admin → Internal Policies & Limits. CFO owns review of these thresholds at each bi-monthly cycle.

### 2.4 Escalation triggers

| Trigger | Source | Routed to | Channel |
|---------|--------|-----------|---------|
| Stream monthly spend ≥ 100 % of `stream_budgets.budget_eur` | `services/xalarm.py` | Stream BO + hold_ops_lead + CFO | In-app bell + email |
| Stream monthly spend ≥ 80 % of budget | xalarm | Stream BO + CFO | In-app bell |
| Invoice flagged as policy violation (T2/T3 threshold breach) | `routes/policy.py` | Bookkeeper + assigned approver | FIO notifications |
| Bank statement audit: unmatched outflow ≥ €500 at month-close | Card Audit chase task | Stream BO (via `chase_routing.stakeholder_for()`) | Asana task auto-created |
| Cashflow projection shows runway < 90 days (any stream) | new check (Phase 3) | Holding CEO + CFO + hold_ops_lead | Email + Slack |
| Loan covenant variance | new check (§5.7) | Holding CEO + CFO + Legal | Email |
| Guarantee drawn down > 80 % of `max_call_amount_eur` | new check (§5.7) | Holding CEO + CFO + Legal | Email |

---

## 3. Phase 3 — Cash Flow Automation

### 3.1 What's already automated in FIO today

| Capability | Endpoint / Module | Status |
|---|---|---|
| Inbound expense capture | `/api/upload` → Claude parser → classification | Live |
| Bank-statement import + reconciliation | Bank Statement Audit tab + `card_transactions` | Live |
| Operational cashflow snapshot (paid-out by week) | `analytics_service.operational_cashflow()` | Live |
| Revenue (AR) receipts logging | `/api/revenue/<id>/receipts` | Live |
| Revenue-side cashflow (rev_in − exp_out) by month | `/api/cashflow` | Live |
| Bank-credit ↔ revenue auto-match | `/api/revenue/bank-match/*` | Live |
| Stream budgets + X-alarm on over-spend | `services/stream_budgets.py` + `services/xalarm.py` | Live |
| Confirm-for-Payment 4-stage approval flow | Confirm-for-Payment tab | Live |

### 3.2 Gaps to close in Phase 3

| Gap | Proposed solution | Owner | Est. effort |
|---|---|---|---|
| **No 13-week cash-flow projection** | New `services/cashflow_projection.py`: combines AR pipeline by due_date, AP pipeline by desired_payment_date, recurring salaries/rent, opening bank balance. Output: weekly net per stream + consolidated. | CFO + IT | 4 h |
| **Bank balance not in FIO** | Add `bank_account_balances` table + daily import via bank APIs (see §3.4) | IT | 6 h |
| **Inter-company transfers not eliminated** | New `services/intercompany.py` with `eliminate(pc_from, pc_to, amount, period)`. Plug into cashflow + P&L. | CFO + IT | 3 h |
| **Salary register exists but not tied to payment_executed_at** | Wire salary docs into the same cashflow stream so projections include payroll | Bookkeeper + IT | 2 h |
| **Slack/email digest not scheduled** | Add monthly cron at `/api/cashflow/digest` that posts to Slack `#exec-committee` | IT | 2 h |
| **FX assumption is spot-rate** | Roll forward ECB rate to projected dates; flag if FX exposure > 10 % of revenue | IT | 3 h |

### 3.3 Phase 3 deliverables (acceptance)

1. **13-week rolling projection** endpoint `/api/cashflow/projection?weeks=13&pc=ALL` returning `[{week_start, opening_bal, ar_in, ap_out, salaries_out, closing_bal, runway_weeks}]`.
2. **Daily digest** posted to Slack `#fio-cashflow` every 08:00 UTC: opening cash, today's expected in/out, 13-week runway, list of T3/T4 invoices awaiting CEO.
3. **Auto-X-alarm** when projection shows any week's closing balance < €X for any stream (X configurable per-PC in admin).
4. **Smoke-test on prod** — projection vs. actual reconciles within ±5 % for trailing 4 weeks.

### 3.4 Bank API access — what we need to collect

The team must collect bank API credentials and rate-limit info for each
provider currently used by any entity. CFO owns this inventory; IT
implements once credentials are in `flyctl secrets` for `fio-amitours`.

| Bank / Provider | Entities using it | What we need from the bank | Auth method | Data we pull | Rate limit | Owner of credentials |
|-----------------|-------------------|----------------------------|-------------|--------------|------------|----------------------|
| **Mercury** (US business banking) | AA, AH | API token + Workspace ID | Bearer token in `Authorization` header | Account list, balances (daily), transactions (incremental since last sync), wire status | 100 req/min | hold_ops_lead today; CFO post-hire |
| **Revolut Business** | AA, AG, AL, SP | API certificate + private key + OAuth client_id + client_secret | OAuth2 + JWT signed with private key | Account balances, statements (per account, per period), counterparty list, FX history | 120 req/min per app | hold_ops_lead today; CFO post-hire |
| **LHV Pank** (Estonia) | AH, MN | XML API key + AccountAdminAccess | Mutual TLS + API key | Daily statements (XML), balance, payment status | 60 req/min | Bookkeeper today; CFO post-hire |
| **Swedbank Latvia** | AA, CF | Connect API token (via Open Banking PSD2) | OAuth2 + PSD2 eIDAS cert | Account info, balance, transactions | 4 req/sec | Bookkeeper |
| **Wise (TransferWise) Business** | Multiple ventures FX | API token (private) | Bearer token | Balances per currency, transfers, FX rates | 100 req/min | hold_ops_lead |
| **Stripe** *(revenue side)* | AA, SP, MN, AL | Restricted API key (read-only on `charges`, `payouts`) | Bearer token | Charge list, payout list (for bank-match Phase 3 of revenue module) | 100 req/sec | Stream BO of each venture |
| **Paddle** *(revenue side, MyPeak)* | CF | API key | Bearer token | Transaction list, subscription list | 30 req/sec | Stream BO (Rihards) |

Collection procedure:

1. CFO (or hold_ops_lead until CFO hired) emails each Signing Officer (§1.5) with the row from the table above relevant to their entity, asking for the credentials and the **bank-side admin contact** in case the rate limit needs adjusting.
2. Each Signing Officer generates the credentials in the bank's portal and delivers via 1Password Secure Share, Signal, or in-person — **never plain email, never Slack DM**.
3. CFO posts the secret to Fly via `flyctl secrets set BANK_<PROVIDER>_<ENTITY>_TOKEN=... -a fio-amitours`.
4. IT confirms the secret is readable by FIO with a one-shot `/api/admin/bank-secret-check` (returns OK / FAIL without echoing the value).
5. IT enables the daily sync; first sync run is validated against a manual balance check by Bookkeeper.
6. Credentials are rotated every 90 days; calendar reminders auto-created in Holding CEO's calendar.

---

## 4. Phase 4 — Consolidated P&L Dashboard

### 4.1 Required slices

The consolidated dashboard must support:

- **By PC stream** (per-row P&L)
- **By legal entity** (rolled up for statutory reporting)
- **By ledger code group** (REVENUE / COGS / MARKETING / ADMIN / OPERATIONAL — already in `services/ledger.py`)
- **By month / quarter / YTD / TTM**
- **Currency**: all-EUR with FX trail at posting date (existing `documents.amount_eur` + `amount_orig`)

### 4.2 Elimination layer (critical for consolidation)

Without elimination the holding P&L double-counts shared-service charges:

```
Raw sum:    AA spend €100k + AG spend €30k + ventures €200k = €330k
            (but €30k of that AG spend IS the cost AA "buys" from shared services)

Eliminated: €330k − €30k intercompany = €300k consolidated
```

Implementation: every `documents` row with `is_internal=1` AND a non-null
`counterparty_pc` (new column) participates in elimination. New endpoint
`/api/pnl/consolidated?period=...` runs the eliminations and returns:

```json
{
  "period": "2026-Q2",
  "raw_revenue": 1450000.00,
  "raw_expense":  1080000.00,
  "eliminations": {
    "ag_to_streams": -120000.00,
    "ah_loans_to_ventures": -45000.00
  },
  "consolidated_revenue": 1450000.00,
  "consolidated_expense":  915000.00,
  "consolidated_net":      535000.00,
  "by_stream": [...],
  "by_ledger_group": {...}
}
```

### 4.3 Dashboard UI (Analytics tab extension)

New top-level card in Analytics tab: **"Consolidated P&L"** with:

- 4-tile KPI strip: Consolidated Revenue / Expense / Net / Margin %
- Toggle: Raw ⇄ Eliminated
- Per-stream table with drill-down to source invoices
- Quarterly trend chart (4 quarters trailing)
- Export → XLSX (for board pack) + PDF (one-pager for CEO)

### 4.4 Authorization (who sees consolidated)

Per `services/roles.py`:

| Role | Sees |
|------|------|
| `admin`, `holding_ceo`, `cfo` *(new role)* | Full consolidated, all streams, eliminations toggle |
| `bookkeeper` | Full consolidated, all streams, no eliminations toggle |
| `stream_owner` | Own stream's contribution only |
| `viewer` | No access |

Add `consolidated-pnl` to `ALL_TABS` + new `cfo` role-gated dashboard route.

### 4.5 Acceptance — Phase 4

- [ ] `/api/pnl/consolidated` returns reconciled numbers (raw − eliminations = consolidated)
- [ ] Q1 2026 numbers reconcile against Bookkeeper's manual close within ±0.5 %
- [ ] Dashboard renders in < 2 s for any quarter
- [ ] Export-to-XLSX produces a board-ready workbook with 4 sheets (Consolidated · By Stream · By Ledger · Eliminations)
- [ ] Smoke: Holding CEO + CFO + each Stream BO can open the dashboard and see only what their role allows

---

## 5. Phase 5 — Dividend Distribution Policy

### 5.1 Eligibility test (per-venture, per-period)

A venture (PC) is dividend-eligible for a period **only if all six conditions hold**:

```
1. consolidated_net(pc, period)               > 0
2. trailing_4q_consolidated_net(pc)           > 0
3. cash_balance(pc, end_of_period)            ≥ working_capital_floor(pc)
4. NO active loan-covenant breach              (see §5.7)
5. NO active guarantee-secured obligation > 80 % drawn  (see §5.7)
6. Stream BO + Holding CEO have signed-off statutory accounts for the period
```

If any condition fails → eligibility = 0 (max dividend = 0). Reason returned alongside.

### 5.2 Max-dividend formula

For eligible ventures:

```
free_cash         = cash_balance(pc, eop) − working_capital_floor(pc)
distributable     = min(consolidated_net(pc, period), free_cash)
loan_buffer       = sum(loan_principal_due_next_4q(pc)) × 1.25
guarantee_buffer  = sum(guarantee_max_call(pc)) × 0.50
max_dividend(pc)  = max(0, distributable − loan_buffer − guarantee_buffer)
```

Holding's max-dividend (to shareholders) is the sum across eligible PCs **minus** AH-level commitments (parent-company loans, holding-level guarantees).

### 5.3 Working Capital Floor — proposals per PC (to be refined by CFO + each Stream BO)

The Working Capital Floor (WCF) is the cash balance below which a venture
**must not** distribute dividends. It exists to protect operations from
short-term cash crunches and to keep the venture solvent for at least 90
days of OPEX even with zero new revenue.

Default formula proposed for v1:

```
WCF(pc) = max(
    90_days_of_OPEX(pc),         # trailing 90-day OPEX from FIO
    90_days_of_payroll(pc),      # from salary register
    next_4q_loan_repayments(pc), # from loans table (§5.7)
    minimum_per_PC               # absolute floor per entity, table below
)
```

Initial proposals (CFO will negotiate final values with each Stream BO; lock in writing during the first 90-day mandate, §0.5 week 9–10):

| PC | Stream | Initial WCF proposal (EUR) | Rationale |
|----|--------|----------------------------:|-----------|
| **AA** | Alps2Alps | €400,000 | Largest payroll + seasonal Q1/Q4 cash swings + transfer-supplier prepayments |
| **AH** | Amitours Holding OÜ | €150,000 | Holding-level legal / tax / committee costs; lower opex but no revenue buffer |
| **AG** | Amitours Group | €80,000 | Shared-services bucket; cost is rebilled to streams |
| **AL** | ALVEDA | €120,000 | Doctor network commitments + content-team payroll |
| **CF** | MyPeak Finance | €60,000 | Lean team; main risk is regulatory licence fees |
| **MN** | Mountly | €150,000 | BD team across 4 markets; SaaS infra spend |
| **MT** | Medical Travel | €40,000 | Early-stage; revisit when run-rate exceeds €100k/mo |
| **SP** | Skipasser | €180,000 | Multi-region BD team + dev + UX; pre-revenue scaling |
| **RR** | Rock2Rock | €30,000 | Earliest-stage; floor will rise quickly with revenue |
| **Holding-wide buffer** (separate from per-PC) | — | €300,000 | Held at AH; covers cross-venture emergency injection without diluting parent cash |

**Review cadence**: at each bi-monthly business-model review (§2.1.1), CFO confirms WCF unchanged or proposes a revision based on the trailing OPEX and any new loan obligations.

### 5.4 Dividend calculator UI

New Admin sub-tab: **"Dividend calculator"** (role: `admin`, `holding_ceo`, `cfo`).

For each venture and the holding:

```
Stream    Eligible?  Net (Q)    Free Cash   Loan Buffer  Guar. Buffer  Max Div
AA        ✓          €120,000   €380,000     €45,000      €10,000       €65,000
AL        ✗ (cov)    €18,000    €25,000      —            —             €0
                                                          (covenant fail)
MN        ✗ (loss)   −€8,000    €60,000      —            —             €0
SP        ✓          €40,000    €110,000     €15,000      €0            €25,000
…
TOTAL eligible to upstream → AH:                                        €90,000
AH commitments (parent loans, holding guarantees):                       −€20,000
Max dividend to shareholders this period:                                €70,000
```

Each row clickable → opens audit drawer showing the formula inputs + which condition failed (if any) + history of prior-quarter eligibility decisions.

### 5.5 Approval workflow

1. **15th of post-quarter month** — Bot auto-computes the table above and emails to Holding CEO + CFO + creates an Asana task in the holding's "Quarterly Close" project.
2. **CFO reviews** with Legal (covenant interpretation) and Bookkeeper (cash & accounts validation).
3. **Holding CEO posts decision** in the Dividend calculator UI: actual dividend per stream (must be ≤ max_dividend). Decision audit-logged with timestamp + actor + memo.
4. **Bookkeeper executes** transfer via existing Confirm-for-Payment flow (T4 tier, requires written board memo).
5. **Receipt at AH** logged as a `revenue_receipts` row against the AH-internal "Dividend in" doc.

### 5.6 Acceptance — Phase 5

- [ ] `loans`, `guarantees`, `covenant_checks` tables shipped + seeded from current obligations
- [ ] `/api/dividend/calculate?period=2026-Q2` returns per-stream max-dividend with reason codes
- [ ] Dividend calculator UI live, role-gated to `admin` / `holding_ceo` / `cfo` only
- [ ] All 6 eligibility conditions tested (positive + negative path each)
- [ ] First production run (2026-Q3 close) produces a calculator output that Legal signs off as covenant-compliant
- [ ] Quarterly auto-email + Asana task scheduled and tested

### 5.7 Loan and Guarantee inventory procedure

New module `services/financial_obligations.py` with three tables:

```sql
CREATE TABLE IF NOT EXISTS loans (
  id TEXT PRIMARY KEY,
  pc TEXT NOT NULL,                 -- borrowing entity (canonical PC)
  counterparty TEXT NOT NULL,       -- lender (bank name / related party)
  agreement_ref TEXT,               -- e.g. "LHV-2025-LN-014"
  agreement_file TEXT,              -- path / URL to the scanned signed PDF
  principal_eur REAL NOT NULL,
  outstanding_eur REAL NOT NULL,
  interest_rate REAL,
  start_date TEXT,
  maturity_date TEXT,
  next_payment_date TEXT,
  next_payment_amount_eur REAL,
  covenants_json TEXT,              -- e.g. {"min_debt_service_coverage": 1.2, "max_leverage": 3.0, "reporting": "quarterly"}
  collateral_description TEXT,
  status TEXT NOT NULL DEFAULT 'active',  -- active / repaid / defaulted / restructured
  created_at TEXT, created_by TEXT
);

CREATE TABLE IF NOT EXISTS guarantees (
  id TEXT PRIMARY KEY,
  pc TEXT NOT NULL,                 -- issuing entity (canonical PC)
  beneficiary TEXT NOT NULL,        -- whom we guaranteed to
  agreement_ref TEXT,
  agreement_file TEXT,
  max_call_amount_eur REAL NOT NULL,
  current_exposure_eur REAL,
  start_date TEXT,
  expiry_date TEXT,
  type TEXT NOT NULL,               -- bank_guarantee / parent_company_guarantee / surety / letter_of_credit
  underlying_obligation TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT, created_by TEXT
);

CREATE TABLE IF NOT EXISTS covenant_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loan_id TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  ratio TEXT NOT NULL,              -- e.g. 'debt_service_coverage'
  actual_value REAL,
  threshold REAL,
  passed INTEGER NOT NULL,          -- 0 / 1
  notes TEXT,
  FOREIGN KEY (loan_id) REFERENCES loans(id) ON DELETE CASCADE
);
```

**Inventory collection procedure** (CFO owns; first run in week 3–4 of the 90-day mandate):

1. CFO drafts a one-page request per Stream BO + Holding CEO:
   *"List every active loan agreement (term loan, line of credit, intercompany loan, related-party loan), every guarantee issued by your entity (bank guarantee, parent-company guarantee, surety, letter of credit), and every guarantee received from a third party. For each: counterparty, agreement reference, principal / max call, current outstanding, maturity, covenants, scanned PDF of the signed agreement."*
2. Each Stream BO replies within 5 business days with the requested data + PDF attachments.
3. CFO collates into the `loans` and `guarantees` tables via a one-shot CSV import (`scripts/seed_obligations.py`, to be written) or via the new Admin → Loans & Guarantees UI.
4. CFO uploads each scanned signed agreement PDF to the **Balance Sheet environment** (see §5.8) and stores the path in `agreement_file`.
5. CFO records covenant ratios in `covenants_json`. The quarterly covenant check (`covenant_checks` rows) is computed automatically by the bot from FIO data + consolidated P&L.
6. Quarterly: bot recomputes ratios; any breach raises an X-alarm to Holding CEO + Legal + CFO (§2.4).

### 5.8 Balance Sheet environment

The current FIO captures P&L items (invoices, receipts, paid expenses) but
does not yet model the full Balance Sheet (assets, liabilities, equity).
For governance the Balance Sheet must live alongside FIO. Proposal:

| Component | Where it lives | Owner |
|-----------|----------------|-------|
| **Cash & bank balances** | `bank_account_balances` table in FIO (new — Phase 3 §3.4) | CFO + Bookkeeper |
| **Accounts Receivable** | `revenue_documents` + `revenue_receipts` in FIO (live) | Bookkeeper |
| **Accounts Payable** | `documents` + `partial_payments` in FIO (live) | Bookkeeper |
| **Loans payable** | `loans` table (new — §5.7) | CFO |
| **Guarantees (off-balance-sheet)** | `guarantees` table (new — §5.7) | CFO |
| **Fixed assets** | New `fixed_assets` table (Phase 5b — scope tbd) | CFO + Bookkeeper |
| **Equity (paid-in capital, retained earnings)** | New `equity_movements` table (Phase 5b) | CFO |
| **Signed agreements (PDF archive)** | Cloud storage bucket or FIO file volume; path referenced from `loans.agreement_file` / `guarantees.agreement_file` | CFO |
| **Consolidated Balance Sheet view** | New endpoint `/api/balance-sheet/consolidated?as_of=YYYY-MM-DD` | CFO + IT |

The Balance Sheet environment is **a thin extension of FIO**, not a separate
system. Single source of truth, single role-gating, single audit log. The
CFO's first sprint (week 3–4, §0.5) is to design and seed the new tables.

---

## 6. Implementation roadmap

| Phase | Estimated effort | Dependencies | Target |
|-------|------------------|--------------|--------|
| 0 — CFO hire | 6–10 weeks search | Holding CEO sign-off on profile (§0) | 2026-Q3 |
| 1 — Structure documentation | 2 h | None (data already in `holding_config.json`) | This week |
| 2 — Governance framework | 4 h (this doc + sign-off) | Phase 1 | Next 2 weeks |
| 3 — Cash flow automation | 16–20 h | Bank API access (§3.4), Slack channel | 2026-Q3 |
| 4 — Consolidated P&L dashboard | 12–16 h | Phase 3 (eliminations data), Phase 1 (intercompany flagging), insurance/grocery/transfer margin models finalised | 2026-Q3 / Q4 |
| 5 — Dividend policy + calculator | 16–20 h | Phase 4, Legal review of covenants, CFO in seat | 2026-Q4 (in time for 2026-Q3 close run) |
| 5b — Balance Sheet extension | 12–16 h | Phase 5 | 2027-Q1 |

**Sequencing constraint**: the consolidated P&L (Phase 4) depends on the
insurance, grocery, and transfer margin models from earlier tasks being
finalised. Phase 4 cannot ship until those models are signed off.

---

## 7. Open questions for kickoff

1. **CFO hire** — start search now? engage a recruiter? fractional vs full-time first 6 months?
2. **Bank API access** — confirmed list of providers per entity in §3.4. CFO (or hold_ops_lead until CFO seated) to collect credentials per the procedure.
3. **Working-capital floor per PC** — initial proposals in §5.3 ready for Stream BO negotiation. Approval by CFO + Holding CEO in writing.
4. **Loan inventory** — collection procedure in §5.7. Initial sweep deadline: 30 days after CFO start, or 30 days after this document is approved if no CFO yet.
5. **Guarantee inventory** — same procedure as loans. CFO + Legal jointly responsible.
6. **Statutory accounts sign-off** — signing officers per entity already documented in §1.5 (sourced from FIO Admin → fio_users); CFO to validate monthly.
7. **Reporting currency** — confirm EUR-only is acceptable for statutory reporting; ventures with USD/CHF cost bases may prefer alt-reporting.

---

## 8. References

- BT4YOU Executive Bot org tree: `bt4you_snapshot/data/holding_config.json`
- FIO PC code catalog: `services/pc_codes.py`
- Stream budgets: `services/stream_budgets.py` + `routes/budgets.py`
- X-alarm logic: `services/xalarm.py`
- Cashflow API (existing): `services/cashflow.py` + `/api/cashflow`
- Internal Policies & Limits: `services/policy_rules.py` + Admin tab
- FIO users (signing officers): `fio_users` table + Admin → Users
- Pattern catalog (PRODUCT_BUILDER):
  - P118 Read-time canonical/legacy translation (PC codes)
  - P121 SQLite FK cascade pitfall (relevant for `loans`/`guarantees` cascade)
  - P122 Backend-first sweep for multi-phase modules (recommended for Phase 3–5)
