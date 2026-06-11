# FIO — glossary for Latvian content

> Purpose: prevent anglicism drift in any LV-language UI labels, vendor
> address fields, or registry text that ships with the product.

## Context for FIO

FIO is primarily an English/Russian product. Latvian content appears in
three places only:

1. **VIES vendor records** — registered names of Latvian SIA / IK /
   companies (e.g. "Avēnija VP SIA", "BATSOFT SIA"). These are LEGAL
   NAMES — never translate or normalise.
2. **Tax & Customs Board references** ("Tax and Customs Board") in
   payment_reference fields — keep verbatim.
3. **Latvian bank statement notes** parsed from imported CSVs — surface
   the raw value, don't lint.

Because Latvian content in FIO is **all proper nouns or external
references**, the linter for `_lv.md` files is intentionally permissive.

## Anti-anglicism rules (if we ship LV docs)

If a future iteration adds Latvian announcement copy or onboarding:

| ❌ Anglicism | ✅ Latvian | Context |
|---|---|---|
| "deploy" | "izvietot", "publicēt" | release notes |
| "approve" | "apstiprināt" | workflow |
| "feedback" | "atsauksme" | tester comms |
| "dropdown" | "izvēlnes saraksts" | UI copy |

## What stays in English in LV text

- UI button labels (Approve, Reject, Mark Paid) — same as the running UI
- Feature names (Card Audit, Confirm for Payment) — searchable identifiers
- Tech vocabulary (deploy, refactor, codex) for the engineering team

## Rule of thumb

If a Latvian word is shorter and unambiguous, use Latvian. If the
English term is what's printed on the screen, keep English.
