#!/usr/bin/env python3
"""Daily runway-alarm trigger — G-xalarm (2026-06-26).

Run from cron (fly machines run / launchd / GitHub Actions) once per day:

    flyctl ssh console -a fio-amitours -C 'python3 scripts/check_runway.py'

Or via fly machines run on a schedule. Or locally:

    THRESHOLD_WEEKS=13 python3 scripts/check_runway.py

Exit codes:
  0 — checked successfully (alarm may or may not have fired)
  1 — failure (logged to stderr)

Env vars:
  THRESHOLD_WEEKS  — runway threshold (default 13 ≈ 90 days, per SOP §5.3)
  RUNWAY_PCS       — comma-separated PCs to check individually
                     (default: holding-wide only, pc=None)
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        threshold = int(os.getenv("THRESHOLD_WEEKS") or 13)
    except ValueError:
        threshold = 13
    pcs_raw = (os.getenv("RUNWAY_PCS") or "").strip()
    pcs = [p.strip() for p in pcs_raw.split(",") if p.strip()] or [None]

    # Ensure app root is on sys.path when invoked as `python3 scripts/...`
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(here))

    from services import xalarm  # noqa: WPS433

    summary = []
    for pc in pcs:
        try:
            result = xalarm.fire_if_low_runway(
                threshold_weeks=threshold, pc=pc, actor="cron"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"runway check failed for pc={pc!r}: {exc}", file=sys.stderr)
            summary.append({"pc": pc or "ALL", "error": str(exc)})
            continue
        if result is None:
            summary.append({"pc": pc or "ALL", "status": "healthy"})
            print(f"  {pc or 'ALL'}: healthy (runway > {threshold} wk)")
        else:
            summary.append({
                "pc": pc or "ALL", "status": "fired",
                "runway_weeks": result.get("runway_weeks"),
                "dedup_hit": result.get("dedup_hit"),
            })
            tag = " (dedup)" if result.get("dedup_hit") else ""
            print(
                f"  {pc or 'ALL'}: ALARM{tag} — runway "
                f"{result.get('runway_weeks')}wk, email "
                f"{result.get('email_status')}"
            )
    print(f"runway check complete: threshold={threshold}, pcs={len(pcs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
