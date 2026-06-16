"""Slack incoming-webhook notifier — Phase 2 (2026-06-16).

Used to alert CEO when bookkeeper flags an invoice as urgent-pay.
Reads SLACK_CEO_WEBHOOK env (or fly secret) at call time so a fresh
secret takes effect without a restart.

P85 graceful: if the webhook is unset, send_* returns
{"status":"not_configured", "hint":...} INSTEAD of raising — caller
shows that in a toast. UI button stays visible even when not configured.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
from urllib import request as _ur
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

__all__ = [
    "send_urgent_payment",
    "send_ping",
    "is_configured",
]


def _webhook_url() -> Optional[str]:
    url = (os.getenv("SLACK_CEO_WEBHOOK") or "").strip()
    return url or None


def is_configured() -> bool:
    return bool(_webhook_url())


def _post(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = _webhook_url()
    if not url:
        return {
            "status": "not_configured",
            "hint": "SLACK_CEO_WEBHOOK secret not set. Admin must run "
                    "`flyctl secrets set SLACK_CEO_WEBHOOK='https://hooks.slack.com/...' -a fio-amitours`. "
                    "See docs/slack-setup-for-ceo.md (to be added) for the 5-step setup.",
        }
    body = json.dumps(payload).encode("utf-8")
    req = _ur.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            code = resp.getcode()
            txt = resp.read().decode("utf-8", errors="replace")
        if code == 200:
            return {"status": "sent", "slack_response": txt[:200]}
        return {"status": "error", "code": code, "body": txt[:200]}
    except HTTPError as e:
        return {"status": "http_error", "code": e.code, "body": str(e)[:200]}
    except URLError as e:
        return {"status": "network_error", "reason": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        logger.exception("Slack POST failed")
        return {"status": "error", "reason": str(e)[:200]}


def send_urgent_payment(*, vendor: str, amount: float, currency: str,
                        doc_url: str, reason: Optional[str] = None,
                        flagged_by: Optional[str] = None,
                        due_date: Optional[str] = None) -> Dict[str, Any]:
    """Post a CEO-facing alert about an invoice that needs urgent attention."""
    bullets = []
    bullets.append("*Vendor*: " + vendor)
    bullets.append("*Amount*: %s %.2f" % (currency or "EUR", float(amount or 0)))
    if due_date:
        bullets.append("*Due*: " + due_date)
    if flagged_by:
        bullets.append("*Flagged by*: " + flagged_by)
    if reason:
        bullets.append("*Reason*: " + reason)
    bullets.append("*Open*: " + doc_url)

    payload = {
        "text": "🚨 *Urgent payment needs CEO approval*",
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text", "text": "🚨 Urgent payment — CEO approval requested"}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": "\n".join(bullets)}},
            {"type": "actions",
             "elements": [
                 {"type": "button",
                  "text": {"type": "plain_text", "text": "Open in FIO"},
                  "url": doc_url, "style": "primary"},
             ]},
        ],
    }
    return _post(payload)


def send_ping() -> Dict[str, Any]:
    """Send a no-op test message — surfaced via the Admin → Slack section."""
    return _post({
        "text": "🟢 FIO Slack integration is live — this is a test ping. "
                "If you're seeing this, the webhook is correctly configured.",
    })
