# Stream Budgets + X-alarm — Architecture Sketch

> Phase 3 design doc. Written 2026-06-16 during Phase 1 close-out so
> the next session can implement directly without re-deriving the schema.
> Bilingual EN / short RU summary at the bottom.

## Problem

Each business stream (AA Alps2Alps, BK Skibookers, SR Skipasser,
AH Mountly, CF MyPeak, AL Alveda) has a monthly cost budget agreed
between the stream owner and the Holding CEO. Today:

- The budget exists only in CEO's head + one-off Slack threads.
- FIO has no representation of it, so no programmatic detection of
  overrun.
- When an invoice arrives that would push the stream over budget,
  Rita can't see it; bookkeeper approves, money goes out, overrun is
  detected only at month-end report.

We want:

1. **Stream budget as data** — a table FIO can read, owned by Artjoms
   (per user's note: "информация о бюджете стрима будет подгружаться в
   саму FIO-программу Артема").
2. **Real-time overrun detection** — every approval-to-pay decision
   checks the running stream total against the stream budget.
3. **X-alarm** — when overrun detected, email goes to CEO + Artjoms +
   Rita + stream owner. Plus an Asana auto-task on the stream owner
   for "renegotiate with CEO" follow-up.

## Schema

```sql
CREATE TABLE stream_budgets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    profit_center     TEXT NOT NULL,             -- 'AA' / 'BK' / 'SR' / 'AH' / 'CF' / 'AL'
    period            TEXT NOT NULL,             -- 'YYYY-MM' (monthly grain to start)
    budget_eur        REAL NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'EUR',
    agreed_by_ceo_at  TEXT NOT NULL,             -- audit trail: when did CEO approve
    agreed_by_ceo     TEXT NOT NULL,             -- typically 'Holding CEO'
    notes             TEXT,                      -- "Q3 increase due to peak season"
    created_at        TEXT NOT NULL,
    created_by        TEXT,
    updated_at        TEXT,
    updated_by        TEXT,
    UNIQUE(profit_center, period)
);
CREATE INDEX ix_sb_pc_period ON stream_budgets(profit_center, period);

CREATE TABLE stream_budget_history (
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

CREATE TABLE xalarm_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_at  TEXT NOT NULL,
    profit_center TEXT NOT NULL,
    period        TEXT NOT NULL,
    budget_eur    REAL NOT NULL,
    actual_eur    REAL NOT NULL,
    overrun_eur   REAL NOT NULL,
    overrun_pct   REAL NOT NULL,
    trigger_doc_id TEXT,                         -- the invoice that crossed the line
    recipients    TEXT NOT NULL,                 -- JSON list of email addresses
    email_status  TEXT,                          -- 'sent' / 'failed' / 'queued'
    asana_task_url TEXT,                         -- nullable, P85 if ASANA_PAT absent
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
```

## Services

### `services/stream_budgets.py`

```python
def list_budgets(period: str | None = None) -> list[dict]: ...
def get_budget(pc: str, period: str) -> dict | None: ...
def set_budget(pc: str, period: str, eur: float, by: str,
               reason: str | None) -> dict: ...
def history_for(pc: str | None = None, period: str | None = None) -> list[dict]: ...

def actuals_for(pc: str, period: str) -> float:
    """Sum of all paid-or-confirmed-to-pay invoices for the (pc, period)."""
    # Reads from documents WHERE profit_center=pc AND period=period
    # AND status IN ('confirmed_to_pay', 'paid', 'archived')
    # Returns sum(amount_eur). Used by overrun check.

def is_over(pc: str, period: str) -> dict:
    """Returns {over: bool, budget: float, actual: float, remaining: float}."""
```

### `services/xalarm.py`

```python
def fire_if_overrun(doc_id: str, *, triggering_action: str,
                    actor: str) -> dict | None:
    """Call this from confirm-payment and budget-validate handlers
    after the state transition succeeds. If the (pc, period) is now
    over budget, send X-alarm and persist to xalarm_log."""
    # 1. Lookup doc → (pc, period)
    # 2. Call stream_budgets.is_over(pc, period)
    # 3. If over: dedupe — only fire once per (pc, period, trigger_doc_id)
    # 4. Build email body (templated from data/xalarm_email.txt)
    # 5. Send via existing SMTP helper (reuse chase email plumbing)
    # 6. Try services.asana_sync.create_task(...) — P85 graceful if no token
    # 7. INSERT INTO xalarm_log
    # 8. Return dict for the route to surface in response (UI shows toast)
```

## Routes

```
GET    /api/stream-budgets?period=YYYY-MM        — list
GET    /api/stream-budgets/<pc>/<period>         — single
POST   /api/stream-budgets                       — create/update
GET    /api/stream-budgets/<pc>/<period>/actuals — live overrun status
GET    /api/stream-budgets/history?pc=&period=   — change log
GET    /api/xalarm-log?pc=&period=               — past alarms (unacknowledged + recent)
POST   /api/xalarm-log/<id>/acknowledge          — mark seen
```

Role gates:
- `set_budget`: Admin + Holding CEO only (Artjoms also gets it via Admin role)
- Everyone else: read-only on budgets + own-stream actuals

## UI

### Admin tab → new section "💰 Stream Budgets"
- Period picker (current month default) + grid: one row per stream
  - PC code | Stream name | Budget | Actual | Remaining | Over? | [edit budget]
  - Color-code "Remaining" row: green > 20%, yellow 0–20%, red < 0
- Edit modal: amount + reason; PATCH writes new row to stream_budget_history

### Per-stream tab (Approve / Confirm-payment)
- Banner above the table:
  - `💰 AA Stream — May 2026: €82,400 / €100,000 (€17,600 remaining)` (green)
  - On overrun: `🚨 AA Stream — May 2026: €115,000 / €100,000 (€15,000 OVER BUDGET — X-alarm sent 2026-05-28 14:30)` (red, with link to xalarm_log entry)

### X-alarm email template (data/xalarm_email.txt)

```
Subject: 🚨 X-ALARM — {pc_name} stream {period}: €{actual:,.2f} of €{budget:,.2f} budget (€{overrun:,.2f} over, +{overrun_pct:.1f}%)

Triggering invoice: {vendor} — €{amount:,.2f} ({doc_url})
Triggered by: {actor} via {action}

This stream's monthly spending has exceeded the budget agreed with the
Holding CEO. Per protocol:

1. {stream_owner_name}, please schedule a meeting with the Holding CEO
   within 48 hours to discuss why the overrun happened and either:
   (a) renegotiate the budget upward with explicit reasons, OR
   (b) commit to specific cost cuts for the remainder of {period}.

2. Until that conversation happens, no further invoices in this
   stream above €5,000 should be approved.

3. Rita: please flag any new {pc_name} invoices > €5,000 as
   "on_hold" pending CEO sign-off.

Recent budget history for this stream:
{history_summary}

— FIO Accounting Bot (auto-generated)
```

Recipients computed at send time:
- Holding CEO email — from a new env var `XALARM_CEO_EMAIL`
- Artjoms — hardcoded fallback `artjoms.fokejevs@gmail.com`, override via `XALARM_OPS_EMAIL`
- Rita — looked up from `fio_users` WHERE role='bookkeeper' (first hit's email)
- Stream owner — from `bt4you_snapshot/data/holding_config.json` people-map
  (PC → person → email)

Dedup: one X-alarm per (pc, period) per ROLLING 24h. Subsequent
triggering invoices update the existing xalarm_log row's
`actual_eur` instead of sending a new email.

## Integration points

- `routes/card_audit.py` confirm-payment handler (existing line ~1608): after
  successful state transition, call `xalarm.fire_if_overrun(doc_id, ...)`.
- `routes/admin.py`: new `/api/stream-budgets` endpoint cluster.
- New `routes/budgets.py` (or extend admin.py) for the read-side endpoints.
- `services/asana_sync.create_task()` already exists (Phase 1 Top-9) —
  use directly with P85 graceful behavior.
- New `data/xalarm_email.txt` seed file (M81 — must be in `seed/` and
  copied via `_seed_data_volume()`).

## Migration

Single inline migration in `services/db.py` next to existing ones:

```python
# 2026-06-?? Phase 3 — stream budgets + X-alarm log
("STREAM_BUDGETS_TABLE_CREATED", "<append to SCHEMA_SQL>"),
```

For the small one-time seeding of June budgets: scripts/seed_stream_budgets.py
that Artjoms runs once with the agreed numbers.

## Open questions for Phase 3 kickoff

1. **Grain** — monthly only, or also quarterly rollups for cross-month
   stream owners?
2. **Currency** — assume EUR only or build multi-currency support upfront?
3. **Asana board** — where do auto-tasks land? One shared board or
   per-stream boards? (Decide before Phase 3 implementation.)
4. **Bot Slack message in addition to email** — covered separately by
   Phase 2 Slack integration; can reuse the same webhook.

---

# RU краткое резюме

Документ описывает архитектуру для:
1. Таблицы **stream_budgets** — Artjoms (через Admin UI) загружает
   согласованные с CEO бюджеты по стримам по месяцам, история всех
   изменений сохраняется.
2. **X-alarm** — при подтверждении оплаты счёта проверяется текущая
   сумма всех оплаченных + одобренных к оплате счетов стрима за этот
   месяц. Если превышает бюджет → отправка email на 4 адреса
   (CEO, Artjoms, Rita, владелец стрима) + автозадача в Asana владельцу
   стрима через существующий services/asana_sync.
3. **Anti-spam дедуп** — один X-alarm на (стрим, месяц) в течение 24
   часов; следующие триггеры просто обновляют запись в логе.
4. UI — банер на Approve/Confirm-payment страницах:
   зелёный если в бюджете, жёлтый при <20% остатка, красный при overrun.

Готово к имплементации в следующей сессии — никаких архитектурных
решений в процессе принимать не нужно, всё на бумаге.
