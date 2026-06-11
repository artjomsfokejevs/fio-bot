# Connecting FIO to Asana — guide for Rita

> ~10 minutes total. You'll generate one access token in Asana, send it to Artjoms securely, then start using "✓ Create in Asana" on chase tasks.

## What this unlocks

When you click **🚀 Generate chase tasks** at month-close in FIO,
every unmatched bank transaction gets a templated task. Today you can
**📋 Copy** that text or open Asana with it pre-filled. Once we connect
your Asana token, a third button — **✓ Create in Asana** — actually
creates the task in your account in one click and opens it for review.

---

## Step 1 — Find your Asana Workspace ID (1 minute)

1. Open Asana in your browser → log in.
2. Look at the address bar. The URL pattern is:
   ```
   https://app.asana.com/0/<WORKSPACE_ID>/<project_id>/...
   ```
3. The first big number after `/0/` is your **workspace ID** — usually
   16–18 digits.

**Example**: if the URL is `https://app.asana.com/0/1234567890123456/list`,
your workspace ID is `1234567890123456`.

**Save this number** — you'll paste it into FIO the first time you
create a task (after that it's cached).

---

## Step 2 — Generate a Personal Access Token (3 minutes)

1. Go to **https://app.asana.com/0/my-apps**
2. Scroll to **Personal access tokens** section
3. Click **+ Create new token**
4. Name it: `FIO Accounting Bot` (so you can revoke it later by name)
5. Click **Create token**
6. **COPY THE TOKEN IMMEDIATELY** — Asana shows it ONCE. If you close
   the page without copying, you'll need to delete it and start over.
   The token starts with `1/` and looks like:
   ```
   1/1234567890123456:abcdef1234567890abcdef1234567890
   ```

---

## Step 3 — Send the token to Artjoms securely

The token gives **full access** to your Asana on your behalf. Treat it
like a password.

**Send via**:
- ✅ 1Password Secure Share (https://share.1password.com)
- ✅ Signal / Telegram (auto-delete enabled)
- ✅ In-person / phone (Artjoms can type it)

**DO NOT send via**:
- ❌ Plain email
- ❌ Slack DM (it's logged forever)
- ❌ Screenshot anywhere

Include in your message:
- The token (Step 2)
- Your workspace ID (Step 1)

---

## Step 4 — Wait for Artjoms (≈ 2 minutes)

Artjoms runs ONE command on the FIO Fly app:
```bash
flyctl secrets set ASANA_PAT="<your-token>" -a fio-amitours
```

Fly automatically redeploys the app with the new secret. You'll see the
"✓ Create in Asana" button start working ~1 minute after he confirms.

---

## Step 5 — First chase task in Asana (1 minute)

1. Open https://fio-amitours.fly.dev → **Bank Statement Audit** tab
2. Upload a bank statement → click **Reconcile**
3. Scroll to **🔐 Month-close checklist** → if there are unmatched
   transactions, click **🚀 Generate chase tasks**
4. Find a transaction → click **▶** to expand → click
   **✓ Create in Asana** (green button)
5. **First click only**: a prompt asks for your workspace ID — paste
   the number from Step 1. It's saved in your browser; you won't be
   asked again.
6. Asana opens the new task in a new tab. The text comes from the
   template you can edit in **Admin → 📝 Chase task template**.

---

## Troubleshooting

| Error message | What it means | Fix |
|---|---|---|
| **ASANA_PAT not configured — set ASANA_PAT secret on the Fly app** | Token not deployed yet | Wait for Artjoms to finish Step 4 |
| **Asana HTTP 401** | Token revoked or expired | Generate a new one (Step 2), send to Artjoms |
| **Asana HTTP 403** | Token doesn't have access to that workspace | Make sure you're a member of the workspace in Asana |
| **Asana HTTP 400 — invalid workspace** | Workspace ID is wrong | Re-check Step 1; clear browser localStorage (DevTools → Application → Local storage → delete `fio_asana_workspace_id`) and try again |
| **Permalink doesn't open** | Browser blocked the popup | Allow popups for `fio-amitours.fly.dev` |

## Security checklist

- [ ] Token starts with `1/` and is 50+ characters
- [ ] Token sent via a secure channel (not email/Slack)
- [ ] Token name in Asana is recognisable (`FIO Accounting Bot`) so it can be revoked specifically
- [ ] Rotate the token every 90 days (delete old one, generate new, send to Artjoms)

## How to revoke

If you ever need to disconnect FIO from Asana:
1. Go to https://app.asana.com/0/my-apps
2. Find **FIO Accounting Bot** in Personal access tokens
3. Click **Delete**
4. Artjoms removes the secret from Fly: `flyctl secrets unset ASANA_PAT -a fio-amitours`

---

## Template customisation (after setup)

Once Asana works, you can edit what the task says **without touching code**.

In FIO: **Admin tab → 📝 Chase task template**.

Available placeholders:
- `{amount}` — EUR amount
- `{vendor}` — counterparty / merchant name
- `{date}` — transaction date
- `{description}` — raw bank-line description
- `{counterparty}` — same as vendor (alias)
- `{reference}` — payment reference
- `{source}` — bank name (mercury / revolut / stripe / ...)
- `{pc}` — AI-suggested business stream code
- `{reason}` — why this stream was suggested

Save with **💾 Save template**. The next chase run uses your edits. Reset
to defaults with **↩ Reset to defaults** if you mess up.
