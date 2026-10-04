#!/usr/bin/env python3
"""Leaderboard dashboard: a small local web page with the main analysis from the database.

Usage:
    python dashboard.py              # open http://localhost:8050 (refreshes every 2 minutes)
    python dashboard.py --demo       # same, with made-up demo data (data/demo.db)
    python dashboard.py --port 9000  # use a different port
    python dashboard.py --share      # let other PCs on your network open it too
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scraper import DEFAULT_DB, connect, store

HERE = Path(__file__).parent
PAGE = HERE / "dashboard.html"
DEMO_DB = HERE / "data" / "demo.db"

WINDOWS = {  # key: (label, timedelta or None for all time)
    "1h": ("last hour", timedelta(hours=1)),
    "6h": ("last 6 hours", timedelta(hours=6)),
    "24h": ("last 24 hours", timedelta(hours=24)),
    "7d": ("last 7 days", timedelta(days=7)),
    "all": ("all time", None),
}
SAMPLE_POINTS = 120  # snapshots sampled for over-time charts (keeps the page fast)
RACE_PLAYERS = 5
VOLATILE_POOL = 100  # "most changeable" looks at the current top N
LIST_LEN = 8


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def sample(ids: list[int], n: int) -> list[int]:
    """Evenly spaced subset of ids that always keeps the first and last."""
    if len(ids) <= n:
        return ids
    step = (len(ids) - 1) / (n - 1)
    return sorted({ids[round(i * step)] for i in range(n)})


def snapshot_rows(conn: sqlite3.Connection, sid: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT name, COALESCE(rank, position), score FROM entries "
        "WHERE snapshot_id = ? AND name IS NOT NULL ORDER BY position",
        (sid,),
    )
    out: dict[str, dict] = {}
    for name, rank, score in rows:
        out.setdefault(name, {"rank": rank, "score": score})
    return out


def build(conn: sqlite3.Connection, window: str) -> dict:
    label, span = WINDOWS.get(window, WINDOWS["24h"])
    now = datetime.now(timezone.utc)

    latest = conn.execute(
        "SELECT id, scraped_at FROM snapshots WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_any = conn.execute(
        "SELECT scraped_at, status, error FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not latest:
        return {"empty": True, "last_run": last_any and {
            "at": last_any[0], "status": last_any[1], "error": last_any[2]}}

    latest_id, latest_at = latest
    cutoff = iso(now - span) if span else ""
    window_ids = [r[0] for r in conn.execute(
        "SELECT id FROM snapshots WHERE status = 'ok' AND scraped_at >= ? ORDER BY id", (cutoff,))]
    if not window_ids:  # nothing in the window: fall back to the latest snapshot alone
        window_ids = [latest_id]
    base_id = window_ids[0]
    base_at = conn.execute("SELECT scraped_at FROM snapshots WHERE id = ?", (base_id,)).fetchone()[0]

    now_rows = snapshot_rows(conn, latest_id)
    base_rows = snapshot_rows(conn, base_id) if base_id != latest_id else now_rows
    comparable = base_id != latest_id

    # --- 1. headline KPIs
    day_ago = iso(now - timedelta(hours=24))
    runs_24h, ok_24h = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(status = 'ok'), 0) FROM snapshots WHERE scraped_at >= ?",
        (day_ago,)).fetchone()
    total, since = conn.execute(
        "SELECT COUNT(*), MIN(scraped_at) FROM snapshots WHERE status = 'ok'").fetchone()
    kpi = {
        "players": len(now_rows),
        "players_change": len(now_rows) - len(base_rows) if comparable else None,
        "last_ok": latest_at,
        "last_run": {"at": last_any[0], "status": last_any[1], "error": last_any[2]},
        "runs_24h": runs_24h,
        "ok_24h": ok_24h,
        "snapshots": total,
        "since": since,
    }

    # --- 2. big movers (rank change between start of window and now)
    moves = []
    if comparable:
        for name, cur in now_rows.items():
            old = base_rows.get(name)
            if old and cur["rank"] is not None and old["rank"] is not None and old["rank"] != cur["rank"]:
                moves.append({"name": name, "from": old["rank"], "to": cur["rank"],
                              "change": old["rank"] - cur["rank"]})
    climbers = sorted((m for m in moves if m["change"] > 0), key=lambda m: (-m["change"], m["to"]))[:LIST_LEN]
    fallers = sorted((m for m in moves if m["change"] < 0), key=lambda m: (m["change"], m["to"]))[:LIST_LEN]

    # --- 3. score gainers
    has_score = any(r["score"] is not None for r in now_rows.values())
    gainers = []
    if comparable and has_score:
        for name, cur in now_rows.items():
            old = base_rows.get(name)
            if old and cur["score"] is not None and old["score"] is not None:
                gain = cur["score"] - old["score"]
                if gain > 0:
                    gainers.append({"name": name, "gain": gain, "score": cur["score"], "rank": cur["rank"]})
        gainers.sort(key=lambda g: -g["gain"])
        gainers = gainers[:2 * LIST_LEN]  # same height as the movers list beside it

    # --- 4 & 5. over-time: top-5 race and most changeable in the top 100
    sampled = sample(window_ids, SAMPLE_POINTS)
    times = dict(conn.execute(
        f"SELECT id, scraped_at FROM snapshots WHERE id IN ({','.join('?' * len(sampled))})", sampled))
    ranked_now = sorted((r["rank"], n) for n, r in now_rows.items() if r["rank"] is not None)
    race_names = [n for _, n in ranked_now[:RACE_PLAYERS]]
    pool = [n for r, n in ranked_now if r <= VOLATILE_POOL] or [n for _, n in ranked_now[:VOLATILE_POOL]]
    wanted = set(pool) | set(race_names)
    track: dict[str, dict[int, int]] = {n: {} for n in wanted}
    marks = ",".join("?" * len(sampled))
    for sid, name, rank in conn.execute(
            f"SELECT snapshot_id, name, COALESCE(rank, position) FROM entries "
            f"WHERE snapshot_id IN ({marks})", sampled):
        if name in track and sid not in track[name]:
            track[name][sid] = rank
    race = {
        "times": [times[s] for s in sampled],
        "series": [{"name": n, "ranks": [track[n].get(s) for s in sampled]} for n in race_names],
    }
    volatile = []
    for n in pool:
        ranks = [track[n][s] for s in sampled if s in track[n]]
        if len(ranks) < 2:
            continue
        changes = sum(1 for a, b in zip(ranks, ranks[1:]) if a != b)
        if changes == 0:
            continue
        volatile.append({"name": n, "now": now_rows[n]["rank"], "best": min(ranks), "worst": max(ranks),
                         "range": max(ranks) - min(ranks), "changes": changes,
                         "spark": [track[n].get(s) for s in sampled]})
    volatile.sort(key=lambda v: (-v["range"], -v["changes"], v["now"]))
    volatile = volatile[:LIST_LEN]

    # --- 6. joined / dropped off
    joined = sorted(({"name": n, "rank": r["rank"]} for n, r in now_rows.items() if n not in base_rows),
                    key=lambda x: (x["rank"] is None, x["rank"]))
    dropped = sorted(({"name": n, "rank": r["rank"]} for n, r in base_rows.items() if n not in now_rows),
                     key=lambda x: (x["rank"] is None, x["rank"]))

    return {
        "empty": False,
        "window": window, "window_label": label,
        "generated_at": iso(now), "base_at": base_at, "latest_at": latest_at,
        "comparable": comparable,
        "kpi": kpi,
        "movers": {"up": climbers, "down": fallers},
        "gainers": gainers, "has_score": has_score,
        "race": race,
        "volatile": volatile, "volatile_pool": VOLATILE_POOL,
        "churn": {"joined": joined[:LIST_LEN], "dropped": dropped[:LIST_LEN],
                  "joined_total": len(joined), "dropped_total": len(dropped)},
    }


# --------------------------------------------------------------------------- demo data

def make_demo(path: Path, days: float = 3, every_min: int = 2, players: int = 150) -> None:
    """Fill a database with realistic made-up leaderboard history."""
    if path.exists():
        path.unlink()
    rng = random.Random(42)
    first = ["Alex", "Sam", "Jo", "Chris", "Pat", "Max", "Robin", "Kim", "Lee", "Ash", "Jamie", "Drew",
             "Charlie", "Taylor", "Morgan", "Casey", "Riley", "Jordan", "Quinn", "Avery"]
    last = ["Racing", "Speed", "Motors", "GP", "Velocity", "Apex", "Turbo", "Drift", "Pitlane", "Grid"]
    names = list(dict.fromkeys(f"{rng.choice(first)} {rng.choice(last)} {i:02d}" for i in range(players + 30)))
    pace = {n: rng.uniform(0.2, 3.0) for n in names}
    score = {n: rng.uniform(1000, 9000) for n in names}
    active = set(names[:players])
    waiting = names[players:]
    conn = connect(path)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    steps = int(days * 24 * 60 / every_min)
    for i in range(steps + 1):
        t = start + timedelta(minutes=i * every_min)
        if rng.random() < 0.01:
            store(conn, iso(t), [], 0, error="Timeout while loading page 3")
            continue
        for n in active:
            burst = 25 if rng.random() < 0.002 else 1  # occasional hot streak
            score[n] += max(0.0, rng.gauss(pace[n], 1.5)) * burst
        if waiting and rng.random() < 0.004:
            active.add(waiting.pop())
        if len(active) > 20 and rng.random() < 0.002:
            active.discard(rng.choice(sorted(active)))
        order = sorted(active, key=lambda n: -score[n])
        entries = [{"page": p // 25 + 1, "position": p + 1, "rank": p + 1, "name": n,
                    "score": round(score[n], 1),
                    "data": {"rank": str(p + 1), "player": n, "points": f"{score[n]:,.1f}"}}
                   for p, n in enumerate(order)]
        store(conn, iso(t), entries, (len(entries) + 24) // 25)
    conn.close()


# --------------------------------------------------------------------------- server

def make_handler(db: Path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/data":
                window = parse_qs(url.query).get("window", ["24h"])[0]
                try:
                    if not db.exists():
                        data = {"empty": True, "last_run": None}
                    else:
                        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
                        try:
                            data = build(conn, window)
                        finally:
                            conn.close()
                    self._send(200, json.dumps(data).encode(), "application/json")
                except Exception as exc:  # show the problem on the page rather than a blank screen
                    self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args):
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--demo", action="store_true", help="use made-up demo data (data/demo.db)")
    p.add_argument("--share", action="store_true", help="allow other PCs on your network to open it")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = p.parse_args(argv)

    db = args.db
    if args.demo:
        db = DEMO_DB
        if not db.exists():
            print("Creating demo data (takes a few seconds)...")
            make_demo(db)
    elif not db.exists():
        print(f"Note: no database yet at {db}. Run the scraper first, or try --demo.")

    host = "0.0.0.0" if args.share else "127.0.0.1"
    try:
        server = ThreadingHTTPServer((host, args.port), make_handler(db))
    except OSError as exc:
        print(f"Can't start on port {args.port} ({exc}). Try another, e.g. --port 8051", file=sys.stderr)
        return 1
    url = f"http://localhost:{args.port}"
    print(f"Dashboard running at {url}  (reading {db})")
    if args.share:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"Other PCs on your network can open http://{ip}:{args.port}")
        except OSError:
            pass
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
