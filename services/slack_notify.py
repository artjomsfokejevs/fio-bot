"""Slack notifier — Phase 2 (2026-06-16) + Phase 2.5 dual-mode (2026-06-16).

Supports TWO mutually-non-exclusive transports:

  1) Bot token mode — preferred when set. Reuses an existing Slack App
     (e.g. BT4YOU Executive Bot). Set:
       SLACK_BOT_TOKEN          (xoxb-...)
       SLACK_CEO_CHANNEL        (channel ID like C0123ABC or '#name')
     Uses chat.postMessage; supports rich blocks + edit/delete later.

  2) Incoming Webhook mode — fallback when no bot token. Set:
       SLACK_CEO_WEBHOOK        (https://hooks.slack.com/services/...)

If neither is set, returns {"status":"not_configured", "hint":...}
and the UI still posts an in-app bell (graceful degradation).

Reads env at call time so a Fly secret rotation takes effect
without a restart.
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
    "transport",
]


def _webhook_url() -> Optional[str]:
    url = (os.getenv("SLACK_CEO_WEBHOOK") or "").strip()
    return url or None


def _bot_token() -> Optional[str]:
    t = (os.getenv("SLACK_BOT_TOKEN") or "").strip()
    return t or None


def _bot_channel() -> Optional[str]:
    c = (os.getenv("SLACK_CEO_CHANNEL") or "").strip()
    return c or None


def transport() -> str:
    """Which transport will be used: 'bot' / 'webhook' / 'none'."""
    if _bot_token() and _bot_channel():
        return "bot"
    if _webhook_url():
        return "webhook"
    return "none"


def is_configured() -> bool:
    return transport() != "none"


def _post_webhook(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = _webhook_url()
    body = json.dumps(payload).encode("utf-8")
    req = _ur.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            code = resp.getcode()
            txt = resp.read().decode("utf-8", errors="replace")
        if code == 200:
            return {"status": "sent", "transport": "webhook", "slack_response": txt[:200]}
        return {"status": "error", "transport": "webhook", "code": code, "body": txt[:200]}
    except HTTPError as e:
        return {"status": "http_error", "transport": "webhook", "code": e.code, "body": str(e)[:200]}
    except URLError as e:
        return {"status": "network_error", "transport": "webhook", "reason": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        logger.exception("Slack webhook POST failed")
        return {"status": "error", "transport": "webhook", "reason": str(e)[:200]}


def _post_bot(payload: Dict[str, Any]) -> Dict[str, Any]:
    token = _bot_token()
    channel = _bot_channel()
    # chat.postMessage expects 'channel' + same blocks/text shape as webhook
    body = dict(payload)
    body["channel"] = channel
    body_bytes = json.dumps(body).encode("utf-8")
    req = _ur.Request(
        "https://slack.com/api/chat.postMessage",
        data=body_bytes,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with _ur.urlopen(req, timeout=10) as resp:
            code = resp.getcode()
            txt = resp.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = {}
        if code == 200 and parsed.get("ok"):
            return {
                "status": "sent",
                "transport": "bot",
                "channel": parsed.get("channel"),
                "ts": parsed.get("ts"),
            }
        return {
            "status": "error",
            "transport": "bot",
            "code": code,
            "slack_error": parsed.get("error") or txt[:200],
            "hint": ("Slack reported '%s'. Common fixes: invite the bot to "
                     "the channel (`/invite @<bot-name>`); ensure scope "
                     "`chat:write` is granted; verify SLACK_CEO_CHANNEL is "
                     "either a channel ID (C0...) or '#name'.")
                    % (parsed.get("error") or "unknown"),
        }
    except HTTPError as e:
        return {"status": "http_error", "transport": "bot", "code": e.code, "body": str(e)[:200]}
    except URLError as e:
        return {"status": "network_error", "transport": "bot", "reason": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        logger.exception("Slack chat.postMessage failed")
        return {"status": "error", "transport": "bot", "reason": str(e)[:200]}


def _post(payload: Dict[str, Any]) -> Dict[str, Any]:
    mode = transport()
    if mode == "bot":
        return _post_bot(payload)
    if mode == "webhook":
        return _post_webhook(payload)
    return {
        "status": "not_configured",
        "transport": "none",
        "hint": "No Slack transport configured. Set EITHER "
                "(SLACK_BOT_TOKEN + SLACK_CEO_CHANNEL) to reuse an existing "
                "Slack App like BT4YOU Executive Bot, OR SLACK_CEO_WEBHOOK "
                "for a single-channel incoming webhook. See docs/slack-setup-for-ceo.md.",
    }


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
