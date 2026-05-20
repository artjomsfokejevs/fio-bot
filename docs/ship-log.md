# Ship Log — FIO Accounting Bot

## Ship 2026-05-12 — v1 (initial private launch)

- **What shipped**: Upload → Approve → Accounting flow on Fly.io
- **First user**: Lilya
- **Lesson**: Forgot Basic Auth — prod was open for 7 days before hardening
- **Fix**: Hardening middleware added to `app.py`, `FIO_PASS` secret set, force-redeployed

## Ship 2026-05-16 — v2 (first feedback round)

- **Triggered by**: Lilya/Rita/Katia/Denis/Dmitrijs feedback
- **Changed**: Currency display, multi-line split, allocations, auto profit-center, vendor verification chain, CSV/Sheets export, reassign, +12 ledger codes
- **Announcement**: `docs/announcement-v2.md` (bilingual)
- **Lesson**: Mojibake `\u{1F464}` literal text in HTML — caught post-deploy

## Ship 2026-05-20 — v3 (Card Audit / month-close)

- **What shipped**: `Card Audit` tab with CSV sniffer (Mercury/Revolut/Stripe/Airwallex), auto-reconcile, per-department breakdown
- **Pattern**: P5 (CSV sniffer) from `fio-patterns.md`
- **Announcement**: `docs/announcement-v3.md` (bilingual)
- **Lesson**: No `docs/` folder existed before this ship — pipeline discipline retrofitted post-hoc

## Ship 2026-05-20 — Pipeline retrofit (this commit)

- **What shipped**: `docs/` artifacts (prd / architecture / spec-cuts / first-user / this file), `tests/unit/` stubs per service, `routes/` Blueprint extraction for Card Audit, ship-hardening gate verified
- **Trigger**: Product Builder retrospective findings
- **Lesson**: Always start with the pipeline. Retrofit is 3× more expensive than upfront discipline.

### Hardening Gate Verification (2026-05-20)

All 6 BLOCKING checks from `PRODUCT_BUILDER/skills/ship-private/SKILL.md` pass:

| # | Check | Result |
|---|---|---|
| 1 | `/health` endpoint matches `fly.toml` probe path | ✅ both = `/health` |
| 2 | Basic Auth gate on every non-health route via `_demo_gate` `before_request` | ✅ |
| 3 | No hardcoded secrets in repo (`sk-…` / `password = "…"`) | ✅ none found |
| 4 | `.dockerignore` covers `*.before-*`, `*.bak`, `.claude/`, `tests/`, `docs/`, `data/audit.jsonl` | ✅ all present |
| 5 | Owner-IP bypass blocked when proxy headers present (Cf-Connecting-Ip / X-Forwarded-For) | ✅ |
| 6 | Rolling deploy strategy declared in `fly.toml` | ✅ `[deploy] strategy = "rolling"` |

### Findings (logged for follow-up)

- `services/parser.py:parse_document` silently swallows `FileNotFoundError` and returns
  an all-`None` doc — caller cannot distinguish missing file from bad OCR. Test
  `tests/unit/test_parser.py` pins current behaviour. Fix in next phase.
- `services/fx.py:get_rate` defaults unknown currencies to `1.0` with a "DANGER"
  warning log instead of returning `None` or raising. Test pins behaviour. Should
  surface as a user-visible error in the UI.

### Refactor progress

- `app.py`: 2154 → 1967 LOC after Card Audit Blueprint extraction (Phase 7.1)
- Remaining extractions per `docs/architecture.md`: 7.2 (accounting), 7.3 (approve), 7.4 (analytics), 7.5 (HTML partials)
- `tests/unit/`: 29 tests, all passing, covers 9 service modules + 1 blueprint integration
