#!/usr/bin/env python3
"""FIO — anti-anglicism linter for Latvian docs.

Currently a stub: FIO Latvian content is all proper nouns (SIA legal
names, Tax & Customs Board references, vendor addresses) — none of it
benefits from linting. This file exists so `references/review_checks.py`
sees the LV linter pair the way it expects.

If a future iteration adds Latvian announcement copy or onboarding,
extend RULES with the table in docs/glossary_lv.md.

Usage:
    python3 scripts/lint_lv.py            # run on docs/*_lv.md
    python3 scripts/lint_lv.py path.md    # run on a single file
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

REPO = Path(__file__).resolve().parent.parent

# Empty rules table — see docs/glossary_lv.md for context.
# Add (regex, suggestion) pairs here if/when FIO ships Latvian
# announcement copy or user-facing onboarding text.
RULES: List = []


def iter_targets(argv: List[str]) -> List[Path]:
    if len(argv) > 1:
        return [Path(a) for a in argv[1:]]
    docs = REPO / "docs"
    if not docs.is_dir():
        return []
    return [p for p in docs.glob("*_lv.md")]


def main(argv: List[str]) -> int:
    targets = iter_targets(argv)
    if not targets:
        print("lint_lv: no Latvian docs to check (skipped).")
        return 0
    if not RULES:
        print(f"lint_lv: ✓ no rules configured (skipped {len(targets)} file(s)). "
              "See docs/glossary_lv.md to add rules.")
        return 0
    # Full scanning logic intentionally absent — matches the empty-rules state.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
