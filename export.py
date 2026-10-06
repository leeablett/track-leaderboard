#!/usr/bin/env python3
"""Export scraped leaderboard data from SQLite to CSV.

Usage:
    python export.py latest  -o latest.csv     # most recent successful snapshot
    python export.py history -o history.csv    # every entry from every snapshot
    python export.py player "Some Name"        # one player's rank/score over time
    python export.py latest --season 3         # a particular season (default: the current one)
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

from scraper import DEFAULT_DB, current_season


def rows_with_data(cur: sqlite3.Cursor) -> tuple[list[str], list[dict]]:
    """Flatten the JSON `data` column into its own CSV columns."""
    base_cols = [d[0] for d in cur.description if d[0] != "data"]
    out, extra_cols = [], []
    for row in cur:
        rec = dict(zip([d[0] for d in cur.description], row))
        data = json.loads(rec.pop("data") or "{}")
        for k in data:
            if k not in extra_cols:
                extra_cols.append(k)
        rec.update({f"data.{k}": v for k, v in data.items()})
        out.append(rec)
    return base_cols + [f"data.{k}" for k in extra_cols], out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", choices=["latest", "history", "player"])
    p.add_argument("name", nargs="?", help="player name (for 'player')")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("-o", "--output", help="CSV file (default: stdout)")
    p.add_argument("--season", type=int, help="season to export (default: the current season)")
    args = p.parse_args(argv)

    conn = sqlite3.connect(args.db)
    season = args.season if args.season is not None else current_season(conn)
    # "IS" also matches older snapshots saved before seasons were recorded (season NULL).
    if args.what == "latest":
        cur = conn.execute(
            "SELECT s.season, s.scraped_at, e.page, e.position, e.rank, e.name, e.score, e.data "
            "FROM entries e JOIN snapshots s ON s.id = e.snapshot_id "
            "WHERE s.id = (SELECT MAX(id) FROM snapshots WHERE status = 'ok' AND season IS ?) "
            "ORDER BY e.position",
            (season,),
        )
    elif args.what == "history":
        cur = conn.execute(
            "SELECT s.id AS snapshot_id, s.season, s.scraped_at, e.page, e.position, e.rank, e.name, e.score, e.data "
            "FROM entries e JOIN snapshots s ON s.id = e.snapshot_id "
            "WHERE s.status = 'ok' AND s.season IS ? ORDER BY s.id, e.position",
            (season,),
        )
    else:
        if not args.name:
            p.error("'player' needs a name")
        cur = conn.execute(
            "SELECT s.season, s.scraped_at, e.rank, e.score, e.data "
            "FROM entries e JOIN snapshots s ON s.id = e.snapshot_id "
            "WHERE e.name = ? AND s.status = 'ok' AND s.season IS ? ORDER BY s.id",
            (args.name, season),
        )

    cols, rows = rows_with_data(cur)
    fh = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.output:
            fh.close()
    if args.output:
        print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
