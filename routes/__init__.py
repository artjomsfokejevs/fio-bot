"""Flask Blueprints — extracted from monolithic app.py per architecture.md Phase 7.

Each blueprint owns a vertical slice of the API surface:
- card_audit: month-close workflow (`/api/card-audit/*`)

Future phases (per docs/architecture.md):
- accounting (export endpoints)
- approve (upload / approve / reassign)
- analytics (KPI endpoints)
"""
from __future__ import annotations

__all__ = ["card_audit_bp"]

from routes.card_audit import card_audit_bp
