#!/usr/bin/env python3
"""Check season support: upgrading an old database, scraping several seasons, and that the
views, exports and dashboard pick the right season.

Run from the project folder:
    python tests/run_season_tests.py
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import dashboard  # noqa: E402
import export  # noqa: E402
import mock_sites  # noqa: E402
import scraper  # noqa: E402

failures = 0


def check(what: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  ({detail})" if detail and not ok else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    # --- address helpers
    check("season read from the address", scraper.season_of(scraper.DEFAULT_URL) == 4)
    check("season set in the address",
          scraper.with_season("https://x/leaderboard?season=4", 2) == "https://x/leaderboard?season=2")
    check("--season lists and ranges", scraper.parse_seasons("1-3, 5") == [1, 2, 3, 5])

    # --- a database made before seasons existed is upgraded, keeping its data
    db = tmp / "old.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, scraped_at TEXT NOT NULL,
            pages INTEGER NOT NULL DEFAULT 0, row_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, error TEXT, content_hash TEXT);
        CREATE TABLE entries (snapshot_id INTEGER NOT NULL, page INTEGER NOT NULL, position INTEGER NOT NULL,
            rank INTEGER, name TEXT, score REAL, data TEXT NOT NULL);
        INSERT INTO snapshots (scraped_at, pages, row_count, status) VALUES ('2026-10-01T10:00:00+00:00', 1, 1, 'ok');
        INSERT INTO entries VALUES (1, 1, 1, 1, 'Old Timer', 1500, '{"player": "Old Timer"}');
    """)
    old.commit()
    old.close()
    conn = scraper.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    check("old database gets a season column", "season" in cols)
    check("old data kept, season unknown",
          conn.execute("SELECT season, row_count FROM snapshots WHERE id = 1").fetchone() == (None, 1))

    # --- new snapshots carry their season
    row = {"page": 1, "position": 1, "rank": 1, "name": "NRG-DFC", "score": 1552.0, "data": {"player": "NRG-DFC"}}
    scraper.store(conn, "2026-10-06T10:00:00+00:00", [row], 1, season=4)
    scraper.store(conn, "2026-10-06T10:02:00+00:00", [], 0, error="Timeout", season=4)
    scraper.store(conn, "2026-09-20T10:00:00+00:00", [dict(row, name="Champion3", data={"player": "Champion3"})], 1, season=3)
    check("current season = highest season, even after backfilling an older one", scraper.current_season(conn) == 4)
    scraper.store(conn, "2026-10-06T10:04:00+00:00", [row], 1, season=4)
    scraper.store(conn, "2026-10-06T10:06:00+00:00", [dict(row, name="New5", data={"player": "New5"})], 1, season=5)
    check("a new, higher season becomes current", scraper.current_season(conn) == 5)
    with conn:
        conn.execute("DELETE FROM snapshots WHERE season = 5")
    seasons = conn.execute("SELECT season, snapshots, players FROM seasons").fetchall()
    check("seasons view", seasons == [(None, 1, 1), (3, 1, 1), (4, 2, 1)], str(seasons))
    latest = conn.execute("SELECT season, name FROM latest ORDER BY season").fetchall()
    check("latest view: one snapshot per season", latest == [(None, "Old Timer"), (3, "Champion3"), (4, "NRG-DFC")], str(latest))
    conn.close()

    # --- export picks the current season by default, or the one asked for
    def export_rows(*args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            export.main([*args, "--db", str(db)])
        return out.getvalue().splitlines()[1:]
    check("export latest = current season", [r.split(",")[0] for r in export_rows("latest")] == ["4"])
    check("export --season 3", any("Champion3" in r for r in export_rows("latest", "--season", "3")))

    # --- dashboard shows one season at a time
    ro = sqlite3.connect(db)
    d = dashboard.build(ro, "all", "current")
    check("dashboard: current season", d["season"] == 4 and d["kpi"]["players"] == 1)
    check("dashboard: season list", [x["season"] for x in d["seasons"]] == [4, 3, None], str(d["seasons"]))
    check("dashboard: pick season 3", dashboard.build(ro, "all", "3")["season"] == 3)
    check("dashboard: unknown-season data", dashboard.build(ro, "all", "none")["kpi"]["players"] == 1)
    check("dashboard: a season with no data", dashboard.build(ro, "all", "9")["empty"])
    ro.close()

    # --- one run scrapes several seasons, each saved as its own snapshot
    server = mock_sites.start()
    db2 = tmp / "multi.db"
    url = f"http://127.0.0.1:{server.server_port}/ssr_next_text/leaderboard"
    rc = scraper.main(["--once", "--no-browser", "--page-delay", "0", "--url", url, "--season", "3,4", "--db", str(db2)])
    conn = sqlite3.connect(db2)
    got = conn.execute("SELECT season, status, row_count FROM snapshots ORDER BY id").fetchall()
    check("--season 3,4 saves one snapshot per season", rc == 0 and got == [(3, "ok", 35), (4, "ok", 35)], str(got))
    server.shutdown()

    print("All passed." if not failures else f"{failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
