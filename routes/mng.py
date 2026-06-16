"""Meet & Greet integration endpoints — Phase 3 pre-deploy STUB.

All endpoints return 503 with a helpful hint UNTIL the external M&G
provider's side ships their webhook outbound + pull API. The stub still
gives them a deterministic endpoint to point at, with correct status
codes for their retry queue testing.

Contract spec: docs/meet-and-greet-dev-handoff.md (bilingual).
P85 pattern: graceful pre-deploy stub. P99 pattern: paired with the
self-contained dev-handoff doc for external implementers.

When ready to go live:
    flyctl secrets set MNG_API_BASE_URL=... MNG_API_TOKEN=... \\
                        MNG_WEBHOOK_SHARED_SECRET=... -a fio-amitours
Then replace the bodies below with the real processing logic.

Added 2026-06-16 (Phase 3 prep).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

mng_bp = Blueprint("mng", __name__, url_prefix="/api/mng")


def _live() -> bool:
    return bool(os.getenv("MNG_API_TOKEN"))


@mng_bp.route("/webhook", methods=["POST"])
def mng_webhook() -> Any:
    """Receive shift.completed events from the M&G provider system.

    Contract: see docs/meet-and-greet-dev-handoff.md §2. While the
    secret is unset we 503 so the provider's retry queue can be
    exercised before go-live.
    """
    if not _live():
        return jsonify({
            "status": "not_configured",
            "hint": "MNG_API_TOKEN secret not set. Admin must run "
                    "`flyctl secrets set MNG_API_TOKEN=... -a fio-amitours`. "
                    "See docs/meet-and-greet-dev-handoff.md §4 for token issuance.",
            "spec_url": "/docs/meet-and-greet-dev-handoff.md",
        }), 503
    # When live: validate Authorization header, dedupe by
    # X-Idempotency-Key, persist to documents with source='mng_webhook'.
    return jsonify({"status": "not_implemented"}), 501


@mng_bp.route("/pull", methods=["POST"])
def mng_pull() -> Any:
    """Trigger a one-off reconciliation pull from the M&G provider.

    Used by the bookkeeper when a webhook batch was missed. Calls the
    provider's GET /v1/shifts?period=YYYY-MM endpoint.
    """
    if not _live():
        return jsonify({
            "status": "not_configured",
            "hint": "MNG_API_BASE_URL + MNG_API_TOKEN secrets not set. "
                    "Pull-reconciliation will be available once the M&G "
                    "provider ships their /v1/shifts endpoint (see "
                    "docs/meet-and-greet-dev-handoff.md §3) and the admin "
                    "deploys the secrets.",
        }), 503
    return jsonify({"status": "not_implemented"}), 501


@mng_bp.route("/status", methods=["GET"])
def mng_status() -> Any:
    """Health/config check — UI can use this to decide whether to show
    the M&G tab as 'connected' or 'awaiting setup'."""
    return jsonify({
        "configured": _live(),
        "secrets_required": ["MNG_API_BASE_URL", "MNG_API_TOKEN",
                             "MNG_WEBHOOK_SHARED_SECRET"],
        "spec": "/docs/meet-and-greet-dev-handoff.md",
        "phase": "pre-deploy stub",
    })
