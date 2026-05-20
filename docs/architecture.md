# FIO Accounting Bot — Architecture

> Retroactive architecture doc, 2026-05-20. Source of truth for code structure decisions.
> References Product Builder patterns from `PRODUCT_BUILDER/skills/architect/references/fio-patterns.md`.

## Stack (deviates from PB default)

| Layer | Choice | Default in PB | Why deviation |
|---|---|---|---|
| Language | Python 3.11+ | Same | — |
| Web framework | **Flask + Gunicorn** | FastAPI | Existing Amitours Flask conventions (BT4YOU) |
| DB | **SQLite + raw `sqlite3`** | PostgreSQL + SQLAlchemy | MVP scope, single-tenant, <100k rows |
| Frontend | **Vanilla JS SPA in `static/index.html`** | Jinja templates | Single-page UX, fast iteration |
| Auth | Hardened Basic Auth + owner-IP bypass | OAuth | One shared password for ≤10 testers |
| Hosting | Fly.io with persistent volume | — | Free tier + region near users |
| Migrations | Idempotent `ALTER TABLE` in `db.init_db()` | Alembic | Pattern **P3** from fio-patterns.md |
| LLM | Claude Sonnet (vision + text) | — | — |
| FX | Frankfurter API (ECB) | — | Free, no key |
| Vendor verification | VIES + format-only + manual | — | Pattern **P2** chain |

## Module layout

```
fio-bot/
├── app.py                  # Flask app + routes (TO BE SPLIT — see below)
├── config.py               # Env-driven settings
├── services/
│   ├── db.py               # SQLite schema + idempotent migrations (P3)
│   ├── parser.py           # Claude vision + OCR vendor hints
│   ├── classifier.py       # Ledger code + profit_center suggestions
│   ├── ledger.py           # 47-code schema loader
│   ├── fx.py               # ECB rates via Frankfurter (cached)
│   ├── vies.py             # EU VAT lookup (cached)
│   ├── company_registry.py # Multi-source verification chain (P2)
│   ├── bt4you_sync.py      # Org-chart map: 65 people × stream × PC (P1)
│   └── card_audit.py       # CSV sniffer + reconciler (P5)
├── routes/                 # Flask Blueprints (Phase 7 refactor)
│   └── card_audit.py       # Extracted from app.py
├── data/
│   ├── fio.db              # SQLite
│   ├── audit.jsonl         # Append-only audit (P4)
│   ├── ledger_schema.json  # 47 ledger codes
│   ├── *_cache/            # Provider caches (P2)
│   └── intake/             # Uploaded source PDFs/images
├── static/
│   └── index.html          # SPA (TO BE SPLIT in Phase 7)
├── tests/unit/             # pytest stubs per service module
├── docs/                   # Pipeline artifacts
├── fly.toml                # Rolling deploy + /health probe
└── Dockerfile
```

## Pattern citations (from `fio-patterns.md`)

| Pattern | Where applied |
|---|---|
| **P1** Domain map | `services/bt4you_sync.py` — 65 people, `suggest_pc_for_uploader()` |
| **P2** Multi-source fallback | `services/company_registry.py` — VIES → format → manual |
| **P3** Idempotent migrations | `services/db.py:init_db()` — try/except ALTER TABLE |
| **P4** Audit JSONL | `data/audit.jsonl` — every approve/reassign/import logged |
| **P5** CSV sniffer | `services/card_audit.py:_FORMAT_SPECS` — Mercury/Revolut/Stripe/Airwallex/generic |
| **P6** Allocation-as-first-class | `docs.allocations_json` column from Phase 3.5 |
| **P7** Feedback rounds | `docs/announcement-v2.md`, `docs/announcement-v3.md` |
| **P8** Ship hardening | Basic Auth + `/health` + secrets in Fly vault |

## Data model (key tables)

```sql
docs (
  id, source_path, vendor, vendor_country, vendor_vat,
  amount, currency, amount_eur, amount_orig, currency_orig,
  fx_rate, fx_date, fx_source, payment_method,
  subtotal, discount, credits,
  ledger_code, profit_center, allocations_json,
  classification_json, status, uploader, vendor_verified_by,
  vendor_verified_at, vendor_verified_note,
  created_at, approved_at
)

card_transactions (  -- Phase 6
  id, source, batch_id, period, posted_at,
  amount, currency, amount_eur, description, counterparty,
  card_holder, department, profit_center,
  matched_invoice_id, match_status, match_confidence,
  UNIQUE(source, posted_at, amount, description)
)
```

## Refactor plan (Phase 7, post-v3)

`app.py` is 2154 LOC. Target: <800 LOC. Extraction plan:
- `routes/card_audit.py` — 7 endpoints (~250 LOC) ✅ Phase 7.1
- `routes/accounting.py` — export endpoints (~200 LOC) — Phase 7.2
- `routes/approve.py` — upload/approve/reassign (~600 LOC) — Phase 7.3
- `routes/analytics.py` — KPI endpoints (~150 LOC) — Phase 7.4
- `static/components/` — split `index.html` (4338 LOC) into HTMX partials — Phase 7.5

## Security

- All routes except `/health` require Basic Auth (`tester` / 20-char password in Fly secret `FIO_PASS`)
- Owner-IP bypass: `X-Forwarded-For` / `Cf-Connecting-Ip` matched against `FIO_OWNER_IP` env
- No secrets in repo (verified by `git ls-files | xargs grep -lE 'sk-[a-zA-Z]{20}'`)
- `.dockerignore` excludes `*.before-*`, `data/audit.jsonl`, `data/*_cache/`, `.claude/`
- Rolling deploy strategy in `fly.toml` with health gate
