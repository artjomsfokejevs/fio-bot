# Data Retention & Compliance — FIO Accounting Bot

_Last reviewed: 2026-06-26_

## Where data lives

| Component | Region | Storage |
|---|---|---|
| Application + uploaded documents | Frankfurt (`fra` Fly.io region) | Fly persistent volume `data` (10 GB), encrypted at rest |
| Database (`fio.db` SQLite) | Frankfurt | Same volume, daily snapshot via Fly volumes |
| Slack / Asana / Gmail webhooks | EU endpoints only | Out-of-process metadata only — original docs stay in Fly |

No customer or invoice data leaves the EU. Postmark / SES inbound email (MCP-1) is configured for EU region only.

## Retention periods

| Document class | Retention | Statutory basis |
|---|---|---|
| Invoices and receipts (`documents` table + originals) | **5 years from end of fiscal year** | LV Law on Accounting §35 (par. 1) — *"primārie dokumenti glabājami 5 gadus"*; same period applies for EE Raamatupidamise seadus §12 |
| Salary / contract documents (`documents.is_salary=1`) | **75 years** (HR-only access) | LV Archives Law §6; EE §12 par. 4 |
| Audit log (`audit_log` table — who-did-what-when) | **10 years** | LV Tax Law §15.2 — extended for tax-relevant operations |
| Bank statements (`card_transactions` + `bank_statement_archive`) | **5 years from period close** | Aligns with primary doc retention; SOP §4.5 |
| Notification / Slack delivery log | 13 months | Operational only, no compliance need |

The retention column is computed on display from `uploaded_at`; deletion is **never automatic** — Rita reviews the *Expired Retention* queue once per year and confirms before any purge.

## Access controls (FB-L / D-coverage)

* 11-capability vocabulary gates every mutation endpoint (B28 scan: 0 findings as of 2026-06-26).
* `pc_scope` restricts accounting staff to their assigned profit centers.
* Holding CEO + Bookkeeper + Admin have unrestricted reads; everyone else is scoped by their `fio_users.profit_center` row.

## Encryption + transport

* HTTPS-only via Fly's auto-provisioned Let's Encrypt cert (TLS 1.2+).
* Database is sqlite on an encrypted Fly volume; no separate at-rest key rotation needed.
* Application secrets (`ANTHROPIC_API_KEY`, `ASANA_PAT`, `SLACK_BOT_TOKEN`, `XALARM_CEO_EMAIL`) live in `fly secrets`, never in repo.

## Compliance contact

Operator of record: Artjoms Fokejevs (`artjoms.fokejevs@gmail.com`). All data-subject requests (GDPR access / erasure) route to that mailbox; response window 30 days per Art. 12(3).
