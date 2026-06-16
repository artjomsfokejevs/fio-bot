# Meet & Greet ↔ FIO Accounting Bot — Developer Handoff
> Bilingual EN / RU. Self-contained spec for **external developers** building API bridges + infrastructure for the Meet & Greet (M&G) integration with FIO.

**Audience**: external dev team (not Rita the bookkeeper, not internal product team). You receive this doc + access to FIO staging once you confirm scope.

**Goal**: deliver a two-way API bridge so that every M&G shift booked in the M&G provider system creates a corresponding cost record in FIO automatically, with the M&G provider invoice routed through FIO's standard approval pipeline.

**Status**: FIO side ships the UI stubs + graceful-503 endpoints in this sprint (P85 pattern). Once your API endpoints come online, FIO switches from stub → live with one config change (Fly secret `MNG_API_BASE_URL` + `MNG_API_TOKEN`).

---

## 0. Context — what is Meet & Greet (M&G)?

M&G = a paid service Alps2Alps offers on top of airport/station transfers: a uniformed host meets the passenger at arrival, helps with luggage, walks them to the driver, hands off, optionally provides a welcome pack. Booked per-transfer, billed monthly by the M&G provider (third-party company supplying the host staff) to Alps2Alps.

Today this flow is **manual**: M&G provider emails Alps2Alps a monthly Excel with shifts performed → Rita reconciles by hand → uploads PDF invoice to FIO → goes through standard approval. We want to replace the Excel reconciliation step with an API integration so shifts flow into FIO in real time.

**Что такое M&G (RU)**: платный сервис встречи пассажиров в аэропортах. Униформированный встречающий помогает с багажом, доводит до водителя. Сейчас провайдер шлёт Excel ежемесячно — мы хотим автоматизировать через API.

---

## 1. Architecture — three contracts to build

```
┌──────────────────┐     1. Webhook (M&G → FIO)      ┌──────────────────┐
│ M&G provider     │ ──────────────────────────────▶ │ FIO              │
│ system           │     shift.completed event       │ Accounting Bot   │
│ (external)       │                                 │ (you don't touch)│
│                  │ ◀────────────────────────────── │                  │
│                  │  2. Pull API (FIO → M&G)        │                  │
│                  │     GET /shifts?period=YYYY-MM  │                  │
│                  │                                 │                  │
│                  │  3. OAuth / Bearer auth         │                  │
└──────────────────┘                                 └──────────────────┘
```

Three contracts:

1. **Webhook FROM M&G provider** — on each shift completion, M&G POSTs to FIO. **Realtime cost recording.** This is your **MUST-HAVE**.
2. **Pull API ON M&G provider** — for monthly reconciliation Rita can request "give me everything for May 2026". Use case: catch-up after webhook outage. **MUST-HAVE.**
3. **Auth model** — Bearer token issued by your team to FIO. Single token per environment (staging + prod). Rotation procedure documented below. **MUST-HAVE.**

---

## 2. Contract 1 — Webhook from M&G provider → FIO

**Endpoint FIO will expose** (built in this sprint, currently returns 503 stub):
```
POST https://fio-amitours.fly.dev/api/mng/webhook
Headers:
  Authorization: Bearer <MNG_WEBHOOK_TOKEN>     # issued by FIO to you
  Content-Type: application/json
  X-Idempotency-Key: <unique-per-event-uuid>    # required, FIO dedupes on this
```

**Body — `shift.completed` event**:
```json
{
  "event": "shift.completed",
  "event_id": "evt_abc123",
  "event_ts": "2026-06-16T14:30:00Z",
  "shift": {
    "shift_id": "mng_shift_98765",
    "transfer_id": "a2a_transfer_55512",
    "host_name": "Anna Müller",
    "host_id": "host_42",
    "location": "Munich Airport — Terminal 2",
    "arrival_iata": "MUC",
    "passenger_name": "John Smith",
    "passenger_count": 4,
    "service_tier": "premium",
    "scheduled_at": "2026-06-16T14:00:00Z",
    "started_at": "2026-06-16T13:55:00Z",
    "completed_at": "2026-06-16T14:25:00Z",
    "currency": "EUR",
    "amount": 25.00,
    "amount_breakdown": {
      "base_fee": 20.00,
      "tier_premium": 5.00,
      "tip": 0.00
    },
    "vendor_legal_entity": "Munich Host Services GmbH",
    "vendor_vat_id": "DE123456789",
    "invoice_period": "2026-06"
  }
}
```

**Response from FIO** (you must handle):
- `200 OK` `{"status":"recorded","doc_id":12345}` — shift recorded successfully.
- `202 Accepted` `{"status":"queued"}` — accepted, will reconcile later.
- `409 Conflict` `{"status":"duplicate","existing_doc_id":12345}` — already seen this `X-Idempotency-Key`. **Do not retry.**
- `401 Unauthorized` — token invalid. Stop sending; alert your ops.
- `503 Service Unavailable` `{"status":"not_configured","hint":"..."}` — FIO not yet connected. **Retry with exponential backoff** (max 24h, then dead-letter).
- `5xx` — server error. **Retry with exponential backoff** (1s, 5s, 30s, 5m, 30m, 2h, max 24h then dead-letter).

**Required behaviour on your side**:
1. **Idempotency**: same `event_id` MUST get same `X-Idempotency-Key`. Replays on retry use the SAME key.
2. **Dead-letter queue**: events that exhaust retries go to a queue that triggers alert to your ops + Rita.
3. **Backfill**: when FIO endpoint comes online after being down, you replay all dead-lettered events in order.

---

## 3. Contract 2 — Pull API on M&G provider system (FIO calls you)

**Endpoint YOU expose**:
```
GET https://api.<your-domain>.com/v1/shifts?period=2026-06&status=completed
Headers:
  Authorization: Bearer <FIO_PULL_TOKEN>        # issued by you to FIO
```

**Response**:
```json
{
  "period": "2026-06",
  "count": 142,
  "total_amount_eur": 3550.00,
  "shifts": [
    { /* same shape as shift in section 2 above */ },
    ...
  ],
  "next_cursor": null
}
```

If list > 500 items, paginate with `?cursor=<token>` returning `next_cursor`. Sort by `completed_at ASC`.

**Required filters** you must support:
- `period=YYYY-MM` (UTC calendar month)
- `status=completed|cancelled|disputed` (FIO will request `completed` 99% of the time)
- `vendor_legal_entity=<exact match>` — for multi-vendor M&G providers

**Latency target**: p95 < 2 seconds for 1-month query.

---

## 4. Contract 3 — Auth model

### Tokens
- **`MNG_WEBHOOK_TOKEN`** (FIO → M&G): FIO issues. M&G includes in `Authorization: Bearer ...` on every webhook POST. Length ≥ 40 chars, opaque. Rotated quarterly.
- **`FIO_PULL_TOKEN`** (M&G → FIO): M&G issues. FIO includes when calling your `/shifts` endpoint. Same constraints.

**Rotation procedure**:
1. New token issued; both tokens valid for 7-day overlap window.
2. Caller switches to new token.
3. Old token revoked after overlap.

**Revocation**:
- Either side can revoke immediately on suspected compromise.
- Revocation triggers 401 on the next call, which is your alert signal.

### Network
- IP allowlist NOT required (tokens sufficient at this scale).
- TLS 1.2+ required both directions.
- No mTLS unless you propose it.

---

## 5. Data model FIO uses on its side (FYI — you don't implement this)

For your reference, FIO creates a `documents` row per shift:

| FIO field | M&G payload source |
|---|---|
| `vendor` | `shift.vendor_legal_entity` |
| `vat_id` | `shift.vendor_vat_id` |
| `amount_eur` | `shift.amount` (converted if currency ≠ EUR) |
| `currency` | `shift.currency` |
| `description` | "M&G — {host_name} @ {location} for {passenger_name}" |
| `invoice_date` | `shift.completed_at` (date part) |
| `legal_entity` | resolved from vendor → Alps2Alps mapping table |
| `category` | hard-coded `"meet_and_greet"` |
| `is_internal` | 0 |
| `source` | `"mng_webhook"` |
| `external_ref` | `shift.shift_id` (for traceback) |

---

## 6. Error handling — what FIO will do when YOUR side is broken

| FIO scenario | FIO action | What you'll observe |
|---|---|---|
| Pull API returns 5xx | Retry with exp. backoff (1m, 5m, 30m, 2h) | Repeated GET attempts |
| Pull API > 30s timeout | Treated as 504 | Same |
| Webhook signature missing/invalid | 401 | No retry from FIO |
| Webhook payload shape wrong | 400 + Slack alert to Rita | One-off, no retry; you investigate |
| Pull API returns shifts NOT seen via webhook | FIO creates `documents` row from pull | Reconciliation completes; webhook missed |
| Pull API returns FEWER shifts than webhook | FIO flags as "potential dispute"; manual review | Discrepancy report emailed |

---

## 7. Testing checklist (deliver to FIO before staging cutover)

You must demonstrate ALL of these against FIO's staging URL (`https://fio-amitours-staging.fly.dev`):

- [ ] Webhook with valid token → 200
- [ ] Webhook with invalid token → 401, your system stops retrying
- [ ] Webhook with duplicate `X-Idempotency-Key` → 409, your system stops retrying
- [ ] Webhook during FIO downtime simulation → retries with exp. backoff, eventual success
- [ ] Webhook for shift where `amount=0` (free tier) → 200, FIO records €0 row
- [ ] Pull `?period=2026-06` returns expected shifts, sorted, paginated
- [ ] Pull with revoked token → 401
- [ ] Pull with `period` 6 months in past → returns archived (or 410 Gone, your call)
- [ ] Currency != EUR shift → both sides convert correctly
- [ ] Cancelled shift webhook (`status=cancelled`) → FIO creates voided row OR ignores (decide jointly)
- [ ] Load: 200 webhooks/second sustained for 1 minute → no errors

---

## 8. Security checklist

- [ ] Tokens stored in your secret manager (NOT in code, NOT in env files committed to git)
- [ ] HTTPS only; HTTP rejected
- [ ] Webhook payloads logged with PII redacted (`passenger_name` redacted at rest after 30 days)
- [ ] No payment card data, no government IDs in payload
- [ ] Logs retained ≥ 90 days for audit; rotation policy documented
- [ ] Penetration test report (or SOC 2 / ISO 27001 cert) shared with FIO before staging cutover

---

## 9. Delivery timeline (proposed — adjust in kickoff)

| Week | Milestone | Owner |
|---|---|---|
| 1 | Kickoff call, freeze contracts in this doc | both |
| 2 | M&G side: webhook outbound + retry queue scaffolding | external dev |
| 3 | M&G side: pull endpoint `/shifts?period=...` + auth | external dev |
| 4 | FIO side: switch endpoint from 503 stub to live processing | FIO team (Artjoms) |
| 5 | Joint staging test (all checklist items in §7) | both |
| 6 | Soft-launch on prod for 1 vendor; manual Excel parallel-run continues | both |
| 7 | Reconciliation report; fix discrepancies | both |
| 8 | Cut-over: kill Excel flow, full prod live | both |

Total: 8 weeks. Aggressive — assume +2 for slippage.

---

## 10. How FIO will notify you of breakage

- **Slack channel**: `#mng-integration-ops` (joint channel created at kickoff)
- **Page-out (only for sustained breach)**: PagerDuty service `mng-bridge` — you provide the integration key.

FIO sends:
- Daily reconciliation summary: shifts received vs. expected (silent if 100% match).
- Webhook 4xx surge alert: > 10 `400` responses in 5 min.
- Pull API down alert: 3 consecutive failed pulls.

---

## 11. Out of scope (for clarity)

- ❌ M&G provider does NOT touch FIO's `documents` table directly. Webhook only.
- ❌ FIO does NOT push approvals/payment status back to M&G. M&G considers itself "done" when shift completed; FIO pays the monthly invoice through standard channels.
- ❌ Passenger PII (full name, contact) not stored long-term in FIO; redacted after 30 days.
- ❌ Real-time pricing logic stays on M&G side. FIO accepts whatever amount you send.
- ❌ Refunds / chargebacks handled out-of-band via Rita → M&G ops (manual email).

---

## 12. Kickoff agenda (60 min)

1. (10 min) Walkthrough of this doc + open questions
2. (15 min) Auth model + token exchange procedure
3. (10 min) Idempotency + retry semantics
4. (10 min) Staging environment access for both sides
5. (10 min) Slack channel + on-call rotation
6. (5 min) Next steps + owner for each milestone

---

## 13. Contact

- **FIO product owner**: Artjoms Fokejevs — artjoms.fokejevs@gmail.com
- **FIO bookkeeper (reconciliation lead)**: Rita Petukhova
- **Alps2Alps M&G product owner**: Marita (full email at kickoff)

---

# RU — короткое резюме для бизнес-стороны

Внешним разработчикам передаём этот документ. Они строят 3 контракта:
1. **Webhook от M&G в FIO** — на каждую завершённую встречу шлют POST с данными смены → FIO автоматически создаёт строку расхода.
2. **Pull API на стороне M&G** — для месячной сверки Rita может запросить "дай всё за май".
3. **Bearer-токены** — с ротацией каждые 90 дней.

Со стороны FIO endpoint уже задеплоен в виде P85 stub (503 пока не настроен). После того как внешние разработчики поднимут свою сторону — одна команда `flyctl secrets set MNG_API_BASE_URL=... MNG_API_TOKEN=... -a fio-amitours` переключает FIO в live-режим без передеплоя кода.

**Timeline**: 8 недель (с буфером 2 недели). Самый рискованный этап — Week 5 совместное тестирование.

**Что НЕ входит**: пуш approval-ов обратно в M&G, real-time pricing, refund-логика — всё это остаётся вне scope этого моста.
