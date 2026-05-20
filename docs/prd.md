# FIO Accounting Bot — Product Requirements Document

> Retroactive PRD reconstructed from conversation history on 2026-05-20.
> Future changes MUST update this file BEFORE code, per Product Builder pipeline discipline.

## Problem (held tightly)

Amitours Holding finance team (Lilya, Rita, Katia, Denis) spends hours per week:
- Re-typing invoice line items into accounting software
- Guessing which profit center a vendor belongs to
- Reconciling corporate-card transactions against missing invoices at month-close
- Manually verifying EU/non-EU vendor legal status

This burns ~3 FTE-days/month across the holding's 5 business units.

## Target user (held tightly)

Finance staff inside Amitours Holding (5 business units, 65 named people in BT4YOU map).
Not external accountants. Not other companies. Single-tenant by design.

## User evidence

Direct quotes from initial feedback round (Phase 1):
- **Lilya**: "Currency показывается всё в евро, даже когда инвойс в USD/AMD"
- **Rita**: "Multi-line банковские выписки нельзя разделить по разным ledger codes"
- **Katia**: "Один счёт оплатили из Alps2Alps, но расход относится к Skibookers — нужен split"
- **Denis**: "Vendor verification только VIES, для non-EU ничего нет"
- **Dmitrijs**: "Auto profit_center не работает — AI пишет в classification_json.codes[0]"

## Solution (held loosely)

Web app on Fly.io behind Basic Auth. Tabs: Upload → Approve → Accounting → Card Audit → Analytics.

### Must-haves (v1)
1. Multi-currency display (USD/AMD/GBP/EUR with ECB FX)
2. Multi-line invoice ledger split
3. Per-document allocation across profit centers (60% AA + 40% BK)
4. Auto profit-center suggestion via BT4YOU domain map
5. Multi-source vendor verification (VIES + format + manual)
6. CSV / Google Sheets export with corporate header
7. Reassign approved doc with audit trail
8. 47 ledger codes (incl. HR_TB, L&D, SaaS, Contractor vs Consulting)

### Must-haves (v3 — Card Audit)
9. CSV import from banks (Mercury / Revolut / Stripe / Airwallex / generic)
10. Auto-reconcile card-tx ↔ approved invoices
11. Per-department breakdown via card_holder → BT4YOU map
12. Month-close workflow with unmatched/suggested/matched/excluded states
13. Export CSV with profit_center breakdown for accounting

## Time budget

3 weeks for v1 (Phases 1–5). +1 week for v3 Card Audit (Phase 6). Total: 4 weeks.
Phases 1–6 shipped on schedule.

## Out of scope (see `spec-cuts.md`)
- Direct bank API connectors (Stripe/Mercury/PayPal/EBICS) — deferred to fio-cashflow
- OpenCorporates non-EU registry — manual verification covers
- Multi-tenant / SaaS productization
- Mobile native app

## Success metric

≥3 named testers actively using the product weekly within 14 days of v3 ship.
Month-close time reduced from 3 FTE-days to <1 FTE-day by month 3.
