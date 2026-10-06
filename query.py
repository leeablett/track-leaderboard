#!/usr/bin/env python3
"""Run a SQL query against the leaderboard database and print the result as a table.

Usage:
    python query.py "SELECT COUNT(*) FROM snapshots"
    python query.py "SELECT * FROM latest WHERE season = 4 LIMIT 10"
    python query.py "SELECT * FROM seasons"
    python query.py "SELECT * FROM latest" -o latest.csv     # save as CSV instead
    python query.py --tables                                  # list tables, views and columns
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from scraper import DEFAULT_DB, connect


def print_table(cols: list[str], rows: list[tuple], max_width: int = 40) -> None:
    def cell(v) -> str:
        s = "" if v is None else str(v)
        return s if len(s) <= max_width else s[: max_width - 1] + "…"

    body = [[cell(v) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in body]) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in body:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sql", nargs="?", help="SQL query to run")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("-o", "--output", help="save the result to this CSV file")
    p.add_argument("--tables", action="store_true", help="list tables, views and their columns")
    args = p.parse_args(argv)

    if not args.db.exists():
        print(f"No database at {args.db}. Run the scraper first.", file=sys.stderr)
        return 1
    conn = connect(args.db)  # makes sure tables and views exist, and are up to date

    if args.tables:
        for name, kind in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
            print(f"{kind:5}  {name}: {', '.join(cols)}")
        return 0
    if not args.sql:
        p.error("give a SQL query in quotes, or --tables")

    try:
        cur = conn.execute(args.sql)
    except sqlite3.Error as exc:
        print(f"SQL error: {exc}", file=sys.stderr)
        return 1
    if cur.description is None:  # not a SELECT
        conn.commit()
        print(f"OK ({cur.rowcount} row(s) affected)")
        return 0

    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows(rows)
        print(f"wrote {len(rows)} rows to {args.output}")
    else:
        print_table(cols, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
