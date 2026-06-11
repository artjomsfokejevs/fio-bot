#!/usr/bin/env python3
"""FIO — anti-anglicism linter for Russian docs.

Greps docs/announcement-*.md and docs/*_ru.md for banned anglicisms
listed in docs/glossary_ru.md. Exits non-zero if any are found.

Usage:
    python3 scripts/lint_ru.py            # run on docs/
    python3 scripts/lint_ru.py path.md    # run on a single file

CI-friendly: returns 0 if clean, 1 if findings.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

REPO = Path(__file__).resolve().parent.parent

# (regex, suggestion). Word-boundary regex; case-insensitive.
RULES: List[Tuple[str, str]] = [
    (r"\bзафейл(и|е)\w*", '"упал" / "не сработал"'),
    (r"\bзакоммит\w*", '"сделать коммит"'),
    (r"\bзашип(и|е)\w*", '"выкатить" / "задеплоить"'),
    (r"\bэппрув\w*",     '"одобрить" / "подтвердить"'),
    (r"\bэпрув\w*",      '"одобрить"'),
    (r"\bриджект\w*",    '"отклонить"'),
    (r"\bассайн\w*",     '"назначить"'),
    (r"\bриквест\w*",    '"запрос"'),
    (r"\bиссью\b",       '"задача" / "баг"'),
    (r"\bлегаси\b",      '"старый код"'),
    (r"\bмокать\b",      '"заглушка" / "фейковые данные"'),
    (r"\bрелонч\w*",     '"перезапуск"'),
    (r"\bпопап\b",       '"всплывающее окно" / "модалка"'),
    (r"\bдропдаун\w*",   '"список" / "выпадающий список"'),
    (r"\bпрелоадер\w*",  '"индикатор загрузки" / "спиннер"'),
    # Common typo + bad transliterations
    (r"\bзаалоццир\w*",  '"распределить"'),
]

COMPILED = [(re.compile(p, re.IGNORECASE), s) for p, s in RULES]


def scan_text(path: Path, text: str) -> List[str]:
    findings: List[str] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # Skip code fences entirely
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Skip indented code blocks
        if line.startswith("    "):
            continue
        # Skip markdown table rows — they typically illustrate banned terms
        # in the glossary itself; the linter's own dictionary is not lint
        # source.
        if stripped.startswith("|"):
            continue
        # Skip negative example bullets that quote the banned term
        # (lines starting with ❌ or "- ❌").
        if stripped.startswith("❌") or stripped.startswith("- ❌"):
            continue
        for rgx, suggestion in COMPILED:
            m = rgx.search(line)
            if m:
                findings.append(
                    f"{path}:{line_no} — anglicism `{m.group(0)}` "
                    f"→ use {suggestion}"
                )
    return findings


def iter_targets(argv: List[str]) -> List[Path]:
    if len(argv) > 1:
        return [Path(a) for a in argv[1:]]
    docs = REPO / "docs"
    if not docs.is_dir():
        return []
    out: List[Path] = []
    for p in docs.glob("*.md"):
        if p.name.startswith("announcement-") or p.name.endswith("_ru.md"):
            out.append(p)
    return out


def main(argv: List[str]) -> int:
    targets = iter_targets(argv)
    if not targets:
        print("lint_ru: no Russian docs to check (skipped).")
        return 0
    all_findings: List[str] = []
    for p in targets:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"lint_ru: cannot read {p}: {exc}", file=sys.stderr)
            continue
        all_findings.extend(scan_text(p, text))
    if all_findings:
        print(f"\nlint_ru: {len(all_findings)} anglicism(s) found:")
        for f in all_findings:
            print("  " + f)
        print("\nSee docs/glossary_ru.md for the full anti-anglicism table.")
        return 1
    print(f"lint_ru: ✓ clean on {len(targets)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
