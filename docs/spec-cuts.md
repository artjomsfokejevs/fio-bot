# Spec Cuts — FIO Accounting Bot

> Append-only log of features deliberately deferred. Per Product Builder `/cut` pattern.
> Cut items can be revisited if `revisit_when` is set.

---

## Cut 2026-05-15 — OpenCorporates non-EU vendor verification

- **What**: Paid API integration with OpenCorporates for non-EU vendor legal-status lookup
- **Why cut**: Format-only check + "Mark verified manually" button covers ≥95% of cases; OpenCorporates costs $99+/month, low ROI for ≤10 testers
- **Replaced by**: Multi-source chain (VIES → format → manual) — Pattern P2
- **Tester quote**: User decision — "✅ Сделать «Mark verified manually» кнопку (10 минут) ❌ OpenCorporates не делать"
- **Revisit when**: ≥1 tester explicitly blocked by non-EU vendor compliance audit

---

## Cut 2026-05-18 — fio-cashflow bank API microservice (Sprint 2–5)

- **What**: Separate FastAPI microservice for direct bank API integration (Stripe / Mercury / PayPal / Enable Banking / EBICS)
- **Why cut**: CSV import in main FIO covers month-close use-case; bank API integration requires 1Password vault + 7 provider credentials + ongoing maintenance
- **Replaced by**: `services/card_audit.py` CSV sniffer (Pattern P5) — Mercury/Revolut/Stripe/Airwallex auto-detected
- **Tester quote**: User decision — "пока все задачи, которые связаны с подключением банков, оставь их в бэклоге"
- **Revisit when**: Volume exceeds ~500 card-tx/month and CSV upload becomes manual bottleneck
- **Code state**: `fio-cashflow/` directory exists with Sprint 1 skeleton (mock Stripe connector, onboarding seed) — paused, not deleted

---

## Cut 2026-05-19 — Multi-tenant SaaS productization

- **What**: Multiple Amitours-like orgs on a single FIO deployment with tenant isolation
- **Why cut**: Single-tenant by design — BT4YOU domain map (P1) is hardcoded org chart, not pluggable
- **Replaced by**: Per-deployment instance (fork-and-customize model)
- **Revisit when**: ≥1 external customer commits to paying for a hosted version

---

## Cut 2026-05-19 — Mobile native app

- **What**: iOS/Android app for invoice capture on-the-go
- **Why cut**: Web SPA is mobile-friendly enough; PWA install-to-home covers it
- **Revisit when**: ≥3 testers complain that mobile web is blocking them

---

## Cut 2026-05-20 — Real-time bank webhooks

- **What**: Push-based card-tx ingestion via Stripe / Mercury webhooks
- **Why cut**: Bundled with fio-cashflow cut above; manual CSV import sufficient for month-close cadence
- **Revisit when**: Same trigger as fio-cashflow
