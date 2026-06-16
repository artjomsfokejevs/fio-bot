"""Tiny SMTP wrapper — Phase 2 foundation for X-alarm in Phase 3.

Reads SMTP_HOST/PORT/USER/PASS env at call time. Defaults to Gmail SSL.
P85 graceful: returns {"status":"not_configured", "hint":...} when
SMTP_USER or SMTP_PASS missing, instead of raising.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = ["send", "is_configured"]


def is_configured() -> bool:
    return bool((os.getenv("SMTP_USER") or "").strip()) and \
           bool((os.getenv("SMTP_PASS") or "").strip())


def send(*, to: Iterable[str], subject: str, body_text: str,
         body_html: Optional[str] = None,
         from_addr: Optional[str] = None,
         reply_to: Optional[str] = None) -> Dict[str, Any]:
    user = (os.getenv("SMTP_USER") or "").strip()
    pwd = (os.getenv("SMTP_PASS") or "").strip()
    host = (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
    try:
        port = int(os.getenv("SMTP_PORT") or "465")
    except ValueError:
        port = 465

    recipients = [a.strip() for a in to if a and a.strip()]
    if not recipients:
        return {"status": "no_recipients"}
    if not user or not pwd:
        return {
            "status": "not_configured",
            "hint": "SMTP_USER + SMTP_PASS Fly secrets not set. Admin must run "
                    "`flyctl secrets set SMTP_USER=... SMTP_PASS=... -a fio-amitours` "
                    "(see docs/smtp-setup.md, to be added).",
            "would_send_to": recipients,
            "subject": subject,
        }

    msg = EmailMessage()
    msg["From"] = from_addr or user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as srv:
                srv.login(user, pwd)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as srv:
                srv.starttls()
                srv.login(user, pwd)
                srv.send_message(msg)
        logger.info("email sent to %s subject=%r", recipients, subject[:60])
        return {"status": "sent", "to": recipients}
    except smtplib.SMTPException as e:
        logger.exception("SMTP send failed")
        return {"status": "smtp_error", "reason": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        logger.exception("email send failed")
        return {"status": "error", "reason": str(e)[:200]}
