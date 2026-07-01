# Cashflow: importing from a PRIVATE Google Sheet

Keel exposes 4 import paths in the "📋 Paste from Google Sheet" modal.
This doc explains the 3 that work for **private** sheets (financial data
you can't publish) and points to the trade-offs of each.

| # | Path | Auth | Setup | Best for |
|---|------|------|-------|----------|
| 1 | 📥 File upload | none | 0 min | One-off imports, weekly manual roll |
| 3 | 🤖 Chrome MCP prompt | your Claude session's Chrome tab | 0 min | Ongoing weekly updates, no downloads |
| 5 | 🔑 GCP service account | Google Cloud project | ~20 min | Fully automated cron-driven refresh |

Options 2 (public URL) is skipped here — it only works when the sheet
is shared with "Anyone with the link", which financial data usually
can't be.

---

## Option 1 — File upload

Fastest. No auth, no setup.

1. In Google Sheets: **File → Download → Comma-separated values (.csv)**
   (or TSV / XLSX — all three are accepted).
2. In Keel → Analytics → "🗓️ Weekly cashflow" → **📋 Paste from Sheet**.
3. In the green "Option 1" callout, click the file input and pick your
   downloaded file. Hit **🔍 Preview** first, then **📥 Import file**.

The file never leaves your Fly workspace. Only the parsed rows are
written to `cashflow_weekly`.

---

## Option 3 — Chrome MCP (Claude-side fetch, no download)

If you already have Claude running in Chrome with Sheets access, you
can skip the download-and-upload dance. Keel serves the endpoint;
Claude does the reading.

1. Open your Google Sheet in Chrome.
2. Open your Claude chat (Chrome MCP extension enabled).
3. In the "Option 3" purple callout inside Keel, click **📋 Copy prompt**.
4. Paste the prompt into Claude. Claude will read the tab, build the
   TSV, and `POST` it to `/api/cashflow/weekly/import-tsv` with your
   identity.
5. Claude reports back with `rows_imported` / `rows_skipped`. If the
   dry-run looks right, ask Claude to retry with `dry_run: false`.

Prompt template (Keel auto-fills your name):

```
Read the currently-open Google Sheet tab (Amitours Unified Cash Timeline).
Extract the rows into TSV (tab-separated) with header row.
Then POST to https://fio-amitours.fly.dev/api/cashflow/weekly/import-tsv
with X-FIO-User: <your full name>, Content-Type: application/json, and body:
{"text": "<TSV here>", "default_row_type": "forecast", "dry_run": true}
Show me the response (rows_imported / rows_skipped / unknown_columns).
If dry-run looks correct, retry with dry_run: false.
```

No sheet-sharing needed. Claude's view of the sheet stays within your
browser session.

---

## Option 5 — GCP service account (fully server-side, private sheet)

This is the long-term option: Keel authenticates to Google Sheets API
with a service-account key and reads private sheets directly. Once
set up, `POST /api/cashflow/weekly/import-gsheet-url` on a private
sheet works the same as it does today on a public one.

### One-time setup (~20 min, requires a GCP owner)

1. **Create a Google Cloud project** (or reuse an existing one).
2. **Enable the Sheets API** for the project:
   `https://console.cloud.google.com/apis/library/sheets.googleapis.com`
3. **Create a service account**:
   - IAM → Service accounts → Create service account.
   - Name it e.g. `keel-cashflow-reader`. No roles needed at project
     level (per-sheet share below is enough).
4. **Generate a JSON key** for the service account and download it.
5. **Share the Sheet with the service account's email**:
   - The service account email looks like
     `keel-cashflow-reader@<project-id>.iam.gserviceaccount.com`.
   - In Google Sheets: File → Share → paste that email → **Viewer** →
     uncheck "Notify people" → Share.
6. **Add the JSON key as a Fly secret**:
   ```bash
   flyctl secrets set GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON="$(cat keel-cashflow-reader.json)"
   ```
7. Fly rolls the app; the boot log confirms the service account loaded.

### Implementation status

**Not yet wired.** Backend needs `google-auth` + `google-api-python-client`
dependencies and a small `services/gsheets_private.py` module that
reads `os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON")` → builds a
`SheetsService` → calls `spreadsheets().values().get(spreadsheetId,
range="Sheet1!A:Z")` → normalises to TSV → hands off to the existing
`import_tsv()`.

When you're ready to ship this, provide the service-account JSON as
described in step 6 and Keel will detect it on next boot and enable
the `POST /api/cashflow/weekly/import-private-gsheet-url` endpoint.

Trade-off: adds two Python deps (~1.5 MB) and a Fly secret. Worth it
only if you want the daily / weekly cron auto-import path.

---

## Which option should I use?

* **Weekly manual roll**: Option 1 (file upload). 30 sec end-to-end.
* **You're already talking to Claude anyway**: Option 3 (MCP prompt).
  Cleanest — no downloads.
* **You want a cron job to auto-refresh forecasts nightly**: Option 5
  (service account). Ping me when you're ready to set up the GCP side.
