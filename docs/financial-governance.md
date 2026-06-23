# Amitours Holding — Financial Governance Framework

> **Status**: Draft v1 · 2026-06-23 · Author: Artjoms Fokejevs (Holding Ops Lead) · Approver: Raitis Bullits (Holding CEO)
>
> **Purpose**: Move holding-level financial oversight from fragmented / manual
> reporting to a single automated source of truth. Closes the gap surfaced in
> the 2026-06-?? executive-committee discussion (minutes 71–75) on holding P&L,
> max-dividend, and cash-flow governance.
>
> **Source of truth for org structure**: `bt4you_snapshot/data/holding_config.json`
> (synced from BT4YOU Executive Bot). Single ledger of streams, roles, and
> stakeholder routing.
>
> **Source of truth for financial transactions**: FIO Accounting Bot
> (`https://fio-amitours.fly.dev/`) — invoices, bank statements, partial
> payments, revenue receipts, stream budgets, X-alarms.

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

**Legacy codes (read-time only — not in pickers)**: SR→SP, BK (Skibookers, decommissioned 2026-06-16), PK→CF. Translation handled centrally in `services/pc_codes.py` (pattern P118).

### 1.3 Inter-stream financial relationships

| Relationship | Description | Tracked in |
|--------------|-------------|------------|
| **AH → ventures** | Holding parent receives dividends; provides intercompany loans / cash injections | `documents.is_internal=1` + `documents.legal_entity` |
| **AG → ventures** | Shared services (legal, marketing, IT) charged out to streams | Allocations table (`documents.allocations` JSON) |
| **AA ↔ ventures** | Operational cross-sell (transfer-bookings → Skipasser ski-pass attach) | Not yet ledger-tracked; tracked operationally only |
| **Ventures → AH** | Profit distribution (subject to dividend policy — §5) | Not yet automated — manual Q4 calc today |

**Gap**: today inter-company transactions are recorded but not **eliminated** in a consolidated view. Phase 4 dashboard must implement elimination entries.

### 1.4 Action items — Phase 1

- [x] **AI-1.1** Pull stream/role tree from `bt4you_snapshot/data/holding_config.json` → table above
- [ ] **AI-1.2** Document each venture's legal-entity registration number (Estonia OÜ, Latvia SIA, etc.) — owner: Legal (vs_legal currently VACANT, falls back to hold_ops_lead)
- [ ] **AI-1.3** Map each PC code to its bank accounts in FIO Admin → Paying Accounts → owner: Rita (Bookkeeper)
- [ ] **AI-1.4** Confirm Rock2Rock (RR) and Medical Travel (MT) BO assignments — owner: Raitis (Holding CEO)
- [ ] **AI-1.5** Backfill `documents.legal_entity` for historical rows where missing — owner: Rita, script: `scripts/backfill_legal_entity.py`

---

## 2. Phase 2 — Financial Governance Framework

### 2.1 Reporting cadences

| Report | Cadence | Owner | Surface in FIO | Audience |
|--------|---------|-------|----------------|----------|
| **Daily cash position** | Every weekday morning | Bookkeeper (Rita) | `/api/cashflow?from=…&to=…` (Phase 2 of #94) | Holding CEO, hold_ops_lead |
| **Weekly payment batch** | Friday | Bookkeeper | Confirm-for-Payment tab → CEO approves → Bookkeeper executes | Holding CEO |
| **Monthly stream P&L** | 1st business day of next month | Each Stream BO | Analytics tab (per-PC drill) + Bookkeeper close checklist | All Stream BOs + Holding CEO |
| **Monthly X-alarm digest** | 1st business day of next month | System (X-alarm) → Stream BO + CFO | `/api/xalarm-log?period=YYYY-MM` | Holding CEO, Stream BOs |
| **Quarterly consolidated P&L** | 10th business day after quarter close | hold_ops_lead | New dashboard (Phase 4, §4) | Holding CEO, board |
| **Quarterly dividend-readiness review** | 15th of month after quarter close | hold_ops_lead + Legal | Manual today → Phase 5 calculator | Holding CEO |

### 2.2 RACI matrix (5-phase delivery)

| Activity | CFO¹ | FD² | IT / Dev | Bot (FIO + BT4YOU) | Bookkeeper | Stream BO | Holding CEO |
|---|---|---|---|---|---|---|---|
| Phase 1 — Structure documentation | C | R | I | A (sync from holding_config) | I | C | A |
| Phase 2 — Governance framework | A | R | C | I | C | C | A |
| Phase 3 — Cash flow automation | A | R | R | A (FIO + cashflow endpoints) | R | I | A |
| Phase 4 — Consolidated P&L dashboard | A | R | R | A (FIO Analytics extension) | C | I | A |
| Phase 5 — Dividend policy + calculator | A | R | C | C (FIO posts to bot) | I | C | A |
| Weekly payment batch approval | C | I | I | A (FIO Confirm-for-Payment) | R | I | A |
| Stream-level over-budget escalation | C | C | I | A (X-alarm) | R | A | C |
| Loan / guarantee covenant tracking | A | R | I | new module (Phase 5b) | C | I | C |

¹ CFO = currently fractional / vacant — duties absorbed by hold_ops_lead (Artjoms) until hired.
² FD = Finance Director (Rita as Bookkeeper covers operational FD function today).
A = Accountable · R = Responsible · C = Consulted · I = Informed.

### 2.3 Approval hierarchy

Spending thresholds (already enforced by `services/policy_rules.py` in FIO):

| Tier | Threshold (EUR / invoice) | Who approves |
|------|---------------------------|--------------|
| **T1** | < €1,000 | Stream BO (auto-approved if budget OK) |
| **T2** | €1,000 – €10,000 | Stream BO + Bookkeeper (Budget OK + Approved by Accounting) |
| **T3** | €10,000 – €50,000 | T2 + Holding CEO (Awaiting CEO stage) |
| **T4** | > €50,000 | T3 + written board memo (manual, off-FIO) |

Editable in FIO Admin → Internal Policies & Limits (Phase 1 P1.2 of policies refactor). Holding CEO can change thresholds; every change audit-logged in `policy_rules_history`.

### 2.4 Escalation triggers

| Trigger | Source | Routed to | Channel |
|---------|--------|-----------|---------|
| Stream monthly spend ≥ 100 % of `stream_budgets.budget_eur` | `services/xalarm.py` | Stream BO + hold_ops_lead | In-app bell + email (when SMTP configured) |
| Stream monthly spend ≥ 80 % of budget | xalarm | Stream BO | In-app bell only |
| Invoice flagged as policy violation (T2/T3 threshold breach) | `routes/policy.py` | Bookkeeper + assigned approver | FIO `notifications` table → bell |
| Bank statement audit: unmatched outflow ≥ €500 at month-close | Card Audit chase task | Stream BO (resolved via `chase_routing.stakeholder_for()`) | Asana task auto-created |
| Cashflow projection (Phase 3 below) shows runway < 90 days | new check (Phase 3) | Holding CEO + hold_ops_lead | Email + Slack |
| Loan covenant variance (Phase 5) | new check (Phase 5b) | Holding CEO + Legal | Email |

---

## 3. Phase 3 — Cash Flow Automation

### 3.1 What's already automated in FIO today

| Capability | Endpoint / Module | Status |
|---|---|---|
| Inbound expense capture | `/api/upload` → Claude parser → classification | ✅ Live |
| Bank-statement import + reconciliation | Bank Statement Audit tab + `card_transactions` | ✅ Live |
| Operational cashflow snapshot (paid-out by week) | `analytics_service.operational_cashflow()` | ✅ Live |
| Revenue (AR) receipts logging | `/api/revenue/<id>/receipts` (#94 Phase 1) | ✅ Live |
| Revenue-side cashflow (rev_in − exp_out) by month | `/api/cashflow` (#94 Phase 2) | ✅ Live |
| Bank-credit ↔ revenue auto-match | `/api/revenue/bank-match/*` (#94 Phase 3) | ✅ Live |
| Stream budgets + X-alarm on over-spend | `services/stream_budgets.py` + `services/xalarm.py` | ✅ Live |
| Confirm-for-Payment 4-stage approval flow | Confirm-for-Payment tab | ✅ Live |

### 3.2 Gaps to close in Phase 3

| Gap | Proposed solution | Owner | Est. effort |
|---|---|---|---|
| **No 13-week cash-flow projection** | New `services/cashflow_projection.py`: combines (a) AR pipeline by due_date, (b) AP pipeline by desired_payment_date, (c) recurring salaries/rent, (d) opening bank balance. Output: weekly net per stream + consolidated. | hold_ops_lead | 4 h |
| **Bank balance not in FIO** | Add `bank_account_balances` table + daily import (Mercury API + Revolut CSV pull) | IT | 6 h |
| **Inter-company transfers not eliminated** | New `services/intercompany.py` with `eliminate(pc_from, pc_to, amount, period)`. Plug into cashflow + P&L. | hold_ops_lead | 3 h |
| **Salary register exists but not tied to payment_executed_at** | Wire salary docs into the same cashflow stream so projections include payroll | Bookkeeper + IT | 2 h |
| **Slack/email digest not scheduled** | Add monthly cron at `/api/cashflow/digest` that posts to `#exec-committee` Slack | IT | 2 h |
| **FX assumption is spot-rate** | Roll forward ECB rate to projected dates (small risk; flag if FX exposure > 10 %) | IT | 3 h |

### 3.3 Phase 3 deliverables (acceptance)

1. **13-week rolling projection** endpoint `/api/cashflow/projection?weeks=13&pc=ALL` returning `[{week_start, opening_bal, ar_in, ap_out, salaries_out, closing_bal, runway_weeks}]`.
2. **Daily digest** posted to Slack `#fio-cashflow` every 08:00 UTC: opening cash, today's expected in/out, 13-week runway, list of T3/T4 invoices awaiting CEO.
3. **Auto-X-alarm** when projection shows any week's closing balance < €X for any stream (X configurable per-PC in admin).
4. **Smoke-test on prod** — projection vs. actual reconciles within ±5 % for trailing 4 weeks.

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

New top-level card in Analytics tab: **"📊 Consolidated P&L"** with:

- 4-tile KPI strip: Consolidated Revenue / Expense / Net / Margin %
- Toggle: Raw ⇄ Eliminated
- Per-stream table with drill-down to source invoices
- Quarterly trend chart (4 quarters trailing)
- Export → XLSX (for board pack) + PDF (one-pager for CEO)

### 4.4 Authorization (who sees consolidated)

Per `services/roles.py`:

| Role | Sees |
|------|------|
| `admin`, `holding_ceo` | Full consolidated, all streams, eliminations toggle |
| `bookkeeper` | Full consolidated, all streams, no eliminations toggle |
| `stream_owner` | Own stream's contribution only — sees consolidated total but not other streams' detail |
| `viewer` | No access |

Add `consolidated-pnl` to `ALL_TABS` + role-gated dashboard route.

### 4.5 Acceptance — Phase 4

- [ ] `/api/pnl/consolidated` returns reconciled numbers (raw − eliminations = consolidated)
- [ ] Q1 2026 numbers reconcile against Rita's manual close within ±0.5 %
- [ ] Dashboard renders in < 2 s for any quarter
- [ ] Export-to-XLSX produces a board-ready workbook with 4 sheets (Consolidated · By Stream · By Ledger · Eliminations)
- [ ] Smoke: Holding CEO + each Stream BO can open the dashboard and see only what their role allows

---

## 5. Phase 5 — Dividend Distribution Policy

### 5.1 Eligibility test (per-venture, per-period)

A venture (PC) is dividend-eligible for a period **only if all six conditions hold**:

```
1. consolidated_net(pc, period)               > 0
2. trailing_4q_consolidated_net(pc)           > 0
3. cash_balance(pc, end_of_period)            ≥ working_capital_floor(pc)
4. NO active loan-covenant breach              (see 5.3)
5. NO active guarantee-secured obligation > 80 % drawn  (see 5.3)
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

### 5.3 Loan and guarantee constraints

This is the constraint the user flagged explicitly ("дистрибуция дивидендов может быть ограничена лоунда, гоморами").

**New module** `services/financial_obligations.py` + tables:

```sql
CREATE TABLE IF NOT EXISTS loans (
  id TEXT PRIMARY KEY,
  pc TEXT NOT NULL,                 -- borrowing entity
  counterparty TEXT NOT NULL,       -- lender (bank name / related party)
  principal_eur REAL NOT NULL,
  outstanding_eur REAL NOT NULL,
  interest_rate REAL,
  start_date TEXT,
  maturity_date TEXT,
  next_payment_date TEXT,
  next_payment_amount_eur REAL,
  covenants_json TEXT,              -- e.g. {"min_debt_service_coverage": 1.2, "max_leverage": 3.0}
  status TEXT NOT NULL DEFAULT 'active',  -- active / repaid / defaulted
  created_at TEXT, created_by TEXT
);

CREATE TABLE IF NOT EXISTS guarantees (
  id TEXT PRIMARY KEY,
  pc TEXT NOT NULL,                 -- issuing entity
  beneficiary TEXT NOT NULL,        -- whom we guaranteed to
  max_call_amount_eur REAL NOT NULL,
  current_exposure_eur REAL,
  start_date TEXT,
  expiry_date TEXT,
  type TEXT NOT NULL,               -- bank_guarantee / parent_company_guarantee / surety / letter_of_credit
  underlying_obligation TEXT,       -- description of what's secured
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

(Note B26: `services/db.py` will need explicit child-table delete or the FK pragma — see Pattern Catalog.)

### 5.4 Dividend calculator UI

New Admin sub-tab: **"💰 Dividend calculator"** (role: `admin` + `holding_ceo` only).

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
Max dividend to shareholders this period:                                 €70,000
```

Each row clickable → opens audit drawer showing the formula inputs + which condition failed (if any) + history of prior-quarter eligibility decisions.

### 5.5 Approval workflow

1. **15th of post-quarter month** — Bot auto-computes the table above and emails to Holding CEO + creates an Asana task in the holding's "Quarterly Close" project.
2. **Holding CEO reviews** with Legal (covenant interpretation) and Bookkeeper (cash & accounts validation).
3. **Holding CEO posts decision** in the Dividend calculator UI: actual dividend per stream (must be ≤ max_dividend). Decision audit-logged with timestamp + actor + memo.
4. **Bookkeeper executes** transfer via existing Confirm-for-Payment flow (T4 tier, requires written board memo).
5. **Receipt at AH** logged as a `revenue_receipts` row against the AH-internal "Dividend in" doc.

### 5.6 Acceptance — Phase 5

- [ ] `loans`, `guarantees`, `covenant_checks` tables shipped + seeded from current obligations
- [ ] `/api/dividend/calculate?period=2026-Q2` returns per-stream max-dividend with reason codes
- [ ] Dividend calculator UI live, role-gated to `admin` / `holding_ceo` only
- [ ] All 6 eligibility conditions tested (positive + negative path each)
- [ ] First production run (2026-Q3 close) produces a calculator output that Legal signs off as covenant-compliant
- [ ] Quarterly auto-email + Asana task scheduled and tested

---

## 6. Implementation roadmap

| Phase | Estimated effort | Dependencies | Target |
|-------|------------------|--------------|--------|
| 1 — Structure documentation | 2 h | None (data already in `holding_config.json`) | This week |
| 2 — Governance framework | 4 h (this doc + sign-off) | Phase 1 | Next 2 weeks |
| 3 — Cash flow automation | 16–20 h | Bank API access (Mercury + Revolut), Slack channel | 2026-Q3 |
| 4 — Consolidated P&L dashboard | 12–16 h | Phase 3 (eliminations data), Phase 1 (intercompany flagging) | 2026-Q3 |
| 5 — Dividend policy + calculator | 16–20 h | Phase 4 (consolidated P&L), Legal review of covenant interpretation | 2026-Q4 (in time for 2026-Q3 close run) |

**Sequencing constraint** (called out by user): the consolidated P&L (Phase 4)
**depends on** the insurance, grocery, and transfer margin models from earlier
tasks being finalised. Phase 4 cannot ship until those models are signed off.

---

## 7. Open questions for kickoff

1. **CFO vacancy** — at what point do we hire vs. expand bookkeeper into fractional FD role?
2. **Bank API access** — who holds Mercury + Revolut admin credentials? (Affects Phase 3 timeline.)
3. **Working-capital floor per PC** — does each venture have a defined operating buffer, or do we set it as % of trailing-12m OPEX?
4. **Loan inventory** — current full list with covenants — owned by? (We need to seed `loans` table.)
5. **Guarantee inventory** — same question for guarantees (parent-company guarantees, bank guarantees issued for ventures).
6. **Statutory accounts sign-off** — who is the signing officer per entity? Today informally Rita+Raitis; for Estonian / Latvian SIA the signing officer rules differ.
7. **Reporting currency** — confirm EUR-only is acceptable for statutory reporting; ventures with USD/CHF cost bases may prefer alt-reporting.

---

## 8. References

- BT4YOU Executive Bot org tree: `bt4you_snapshot/data/holding_config.json`
- FIO PC code catalog: `services/pc_codes.py`
- Stream budgets: `services/stream_budgets.py` + `routes/budgets.py`
- X-alarm logic: `services/xalarm.py`
- Cashflow API (existing): `services/cashflow.py` + `/api/cashflow` (Phase 2 of #94)
- Internal Policies & Limits: `services/policy_rules.py` + Admin tab
- Pattern catalog (PRODUCT_BUILDER):
  - P118 Read-time canonical/legacy translation (PC codes)
  - P121 SQLite FK cascade pitfall (relevant for `loans`/`guarantees` cascade)
  - P122 Backend-first sweep for multi-phase modules (recommended for Phase 3–5)

---

# RU краткое резюме

**Что это**: формальный фреймворк финансового управления Amitours Holding,
переводящий разрозненное / ручное оверсайт в единый автоматизированный
source-of-truth. Закрывает разрыв обозначенный на эксек-комитете (минуты
71–75) — holding P&L, max-dividend, governance cash-flow.

**5 фаз**:

1. **Структура холдинга** — синхронизация из BT4YOU Executive Bot
   (`holding_config.json`), 9 PC-кодов, 4 venture-стрима, маппинг BO→PC.
2. **Governance framework** — каденс отчётности (ежедневный cash, недельный
   payment batch, месячный P&L, квартальный консолидированный), RACI на
   все 5 фаз, иерархия одобрений по 4 tiers (€1k / €10k / €50k / >€50k),
   6 триггеров эскалации.
3. **Автоматизация cash flow** — что уже работает в FIO (бэкэнд готов),
   6 gaps на закрытие: 13-недельный rolling forecast, импорт банковских
   балансов, elimination межкомпанейских проводок, payroll в forecast,
   Slack-дайджест, FX rolling.
4. **Consolidated P&L дашборд** — по PC, по entity, по ledger group, по
   периоду, EUR-only с FX-трейлом. Критично: **elimination layer** для
   корректного консолидирования (без него shared-service двойной счёт).
   Расширение Analytics tab + role-gating + export XLSX/PDF.
5. **Dividend distribution policy** — 6-условный eligibility-тест + max-div
   формула с **loan_buffer + guarantee_buffer** (учли явное требование
   пользователя про "лоунда, гоморами"). Новый модуль `financial_obligations`
   с таблицами `loans`/`guarantees`/`covenant_checks`. Калькулятор-UI в
   Admin (только Holding CEO + admin), квартальный авто-расчёт + Asana
   таск, audit trail каждого решения.

**Sequencing**: Phase 4 блокирована моделями insurance/grocery/transfer margin
(не финализированы). Phase 5 идёт следом за Phase 4.

**Open questions**: CFO найм, банк-API доступы, working-capital floor по
ventures, инвентаризация loans/guarantees, signing officer по entities,
reporting currency.

Готово к approval Holding CEO → дальше Phase 1 → 2 → ... — пошагово, с
автоматизированной поставкой через FIO.
