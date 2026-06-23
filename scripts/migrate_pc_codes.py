"""One-shot PC-code migration utility (2026-06-22, #87 refactor).

Run when ready to consolidate historical data to canonical codes:

  flyctl ssh console -a fio-amitours -C \\
    "sh -c 'PYTHONPATH=/app python3 /app/scripts/migrate_pc_codes.py --dry-run'"

Then drop --dry-run to actually mutate.

Translations applied:
  • SR → SP  (Skipasser rename, no ambiguity)
  • PK → CF  (MyPeak alias)
  • BK → kept as-is (decommissioned; visible in archives only)
  • AH / MT → NOT auto-translated (current canonical meaning differs from legacy;
    needs vendor-pattern review — script prints candidates instead of mutating)
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

sys.path.insert(0, "/app")
from services import db  # noqa: E402

UNAMBIGUOUS = {
    "SR": "SP",   # Skipasser
    "PK": "CF",   # MyPeak Finance
}

AMBIGUOUS_LEGACY = {"AH", "MT"}  # ambiguous because canonical meaning changed
TABLES = [
    ("documents",        "profit_center"),
    ("stream_budgets",   "profit_center"),
    ("xalarm_log",       "profit_center"),
    ("card_transactions","profit_center"),
    ("fio_users",        "profit_center"),
    ("paying_accounts",  "legal_entity"),  # historic alias re-use
]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="report only, do not UPDATE")
    args = p.parse_args(argv)

    conn = db.get_connection()
    try:
        print("=== Pre-migration state ===")
        for tbl, col in TABLES:
            try:
                rows = conn.execute(
                    "SELECT %s, COUNT(*) AS n FROM %s GROUP BY %s ORDER BY n DESC" % (col, tbl, col)
                ).fetchall()
                print("\n%s.%s:" % (tbl, col))
                for r in rows:
                    val = r[col] or "(null)"
                    print("  %-12s %d" % (val, r["n"]))
            except Exception as e:
                print("  [skip] %s: %s" % (tbl, e))

        print("\n=== Unambiguous renames ===")
        for legacy, canonical in UNAMBIGUOUS.items():
            total = 0
            for tbl, col in TABLES:
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM %s WHERE %s = ?" % (tbl, col),
                        (legacy,)
                    ).fetchone()
                    n = row["n"] if row else 0
                    if n:
                        total += n
                        print("  %s.%s: %d rows (%s → %s)" % (tbl, col, n, legacy, canonical))
                        if not args.dry_run:
                            conn.execute(
                                "UPDATE %s SET %s = ? WHERE %s = ?" % (tbl, col, col),
                                (canonical, legacy)
                            )
                except Exception as e:
                    pass
            if not total:
                print("  %s → %s: 0 rows" % (legacy, canonical))

        print("\n=== Ambiguous (NOT auto-migrated, manual review needed) ===")
        for legacy in AMBIGUOUS_LEGACY:
            for tbl, col in TABLES:
                try:
                    rows = conn.execute(
                        "SELECT vendor, COUNT(*) AS n FROM %s WHERE %s = ? GROUP BY vendor LIMIT 10" % (tbl, col),
                        (legacy,)
                    ).fetchall()
                    if rows:
                        print("\n  %s.%s WHERE %s='%s' — top vendors:" % (tbl, col, col, legacy))
                        for r in rows:
                            print("    %-40s %d" % ((r["vendor"] or "(no vendor)")[:40], r["n"]))
                except Exception:
                    pass

        if args.dry_run:
            print("\n[dry-run] No changes committed. Drop --dry-run to apply.")
        else:
            conn.commit()
            print("\n✓ Committed unambiguous renames.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
