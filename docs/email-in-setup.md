# Email-in setup (MCP-1)

Forward an invoice from any inbox → it lands in Approve queue ~30 s later, fully parsed + classified.

## Endpoint

`POST https://fio-amitours.fly.dev/api/email-in/invoice`

**Auth.** Set `fly secrets set FIO_EMAIL_IN_SECRET=<random-hex>`. Webhook must send `X-Email-Secret: <same>` header (or `?secret=…` query). Skip the secret → 401.

**Payload.** Postmark-compatible JSON:

```json
{
  "From": "rita@amitours.com",
  "Subject": "Forwarded: Hetzner DE €18.50",
  "Attachments": [
    {"Name": "hetzner-2026-06.pdf",
     "ContentType": "application/pdf",
     "Content": "<base64>"}
  ]
}
```

SES inbound (lowercase `attachments`) is also accepted.

## Provider setup — Postmark

1. Create a Postmark **inbound** server, region EU (Frankfurt).
2. Postmark assigns an address like `<token>@inbound.postmarkapp.com`.
3. Optionally configure DNS MX on `invoices@fio.amitours.com` to forward to that address (or just send to the Postmark address directly).
4. Set the inbound webhook URL to:
   ```
   https://fio-amitours.fly.dev/api/email-in/invoice?secret=<FIO_EMAIL_IN_SECRET>
   ```
5. Postmark POSTs the JSON above on every inbound message.

## Provider setup — AWS SES

1. SES rule set with action **Save to S3** + **Invoke Lambda**.
2. Lambda parses the MIME, base64s attachments, POSTs to the same endpoint with `X-Email-Secret` header.
3. SES region must be `eu-central-1` (or another EU) for EU-only retention.

## Smoke test

```bash
curl -X POST 'https://fio-amitours.fly.dev/api/email-in/invoice' \
  -H 'Content-Type: application/json' \
  -H "X-Email-Secret: $FIO_EMAIL_IN_SECRET" \
  -d '{"From":"smoke@test","Subject":"smoke","Attachments":[
        {"Name":"smoke.pdf","Content":"'"$(base64 < smoke.pdf)"'"}
      ]}'
```

Expect `201` with `processed[0].status="classified"`.

## What lands in app

* New row in `documents` with `uploaded_by="email:<sender>"`.
* Audit log entries `uploaded` + `parsed` + `classified` (+ `posted` if auto-post threshold met).
* Visible in Upload tab + Approve queue exactly like a manual upload.

## Retention

Same 5y / 75y rules as manual uploads (see `docs/compliance.md`). Original email body is **not** stored — only the attachment + sender address.
