# Connecting Slack to FIO — guide for Artjoms (admin) + CEO

> ~7 minutes. Creates a Slack channel for CEO urgent-payment alerts, generates a webhook URL, deploys it as a Fly secret.

## What this unlocks

When you (or Rita) click **🚨 Send to CEO** on an invoice in the Awaiting Payment stage, FIO will:
1. Post a formatted alert to the configured Slack channel (CEO sees it immediately)
2. Create an in-app notification (bell icon top-right) — so even without Slack, the bell still works

Until SLACK_CEO_WEBHOOK is set, only the bell fires (graceful degradation).

---

## Step 1 — Create a Slack channel (1 min)

In Slack, **+ Add channels → Create a new channel**:
- Name: `fio-ceo-alerts` (private recommended)
- Add members: CEO, Artjoms, optionally Rita
- Done

---

## Step 2 — Create a Slack App (2 min)

1. Go to **https://api.slack.com/apps** → **Create New App** → **From scratch**
2. App name: `FIO Accounting Bot`
3. Workspace: Amitours (or your main one)
4. **Create App**

---

## Step 3 — Enable Incoming Webhooks (2 min)

1. Left sidebar → **Features → Incoming Webhooks**
2. Toggle **Activate Incoming Webhooks** → **On**
3. Scroll down → **Add New Webhook to Workspace**
4. Select channel `#fio-ceo-alerts`
5. **Allow**
6. Copy the URL (starts with `https://hooks.slack.com/services/T.../B.../...`)

---

## Step 4 — Deploy as Fly secret (1 min)

Artjoms (admin) runs:
```bash
flyctl secrets set SLACK_CEO_WEBHOOK="https://hooks.slack.com/services/..." -a fio-amitours
```

Fly auto-redeploys (~30s).

---

## Step 5 — Test (1 min)

1. Open FIO → **Admin** tab
2. Scroll to **💬 Slack integration** section
3. Click **📤 Send test ping**
4. You should see `🟢 FIO Slack integration is live` in the Slack channel

If not — see Troubleshooting.

---

## Troubleshooting

| Error message | Meaning | Fix |
|---|---|---|
| **`not_configured`** | Secret not set | Re-run Step 4 |
| **`http_error 403`** | Webhook URL wrong or revoked | Re-create webhook (Step 3) and update secret |
| **`network_error`** | Fly can't reach hooks.slack.com | Check Fly outbound connectivity / Slack status |
| **No message in channel but `status: sent`** | Channel deleted or app removed | Re-add app to workspace; create new webhook |

## Security checklist

- [ ] Channel is **private** (CEO + ops only)
- [ ] Webhook URL stored as Fly secret (NOT in code)
- [ ] App name is recognisable (`FIO Accounting Bot`) so you can revoke it specifically
- [ ] Webhook rotated every **90 days** (delete + recreate + redeploy secret)

## How to revoke

If you ever need to disconnect:
1. Go to https://api.slack.com/apps → **FIO Accounting Bot** → **Incoming Webhooks**
2. Find the webhook → **Delete**
3. Artjoms: `flyctl secrets unset SLACK_CEO_WEBHOOK -a fio-amitours`

The "🚨 Send to CEO" button will keep working — just falls back to in-app bell only.
