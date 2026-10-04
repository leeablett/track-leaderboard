#!/usr/bin/env python3
"""Scrape the VFM leaderboard (all pages) and store each run as a snapshot in SQLite.

Usage:
    python scraper.py --once              # scrape once and exit (use with cron)
    python scraper.py --loop 120          # scrape every 120 seconds until stopped
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = "https://vfm.bdynamicsstudio.com/leaderboard"
DEFAULT_DB = Path(__file__).parent / "data" / "leaderboard.db"
DEBUG_DIR = Path(__file__).parent / "data" / "debug"

# Header names used to pick out the common fields. Every column is also kept in `data`.
RANK_KEYS = ("rank", "position", "pos", "place", "#", "no")
NAME_KEYS = ("name", "player", "user", "username", "driver", "team", "manager", "club")
SCORE_KEYS = ("score", "points", "pts", "total", "rating", "value", "time")
NEXT_TEXT = {"next", "next page", "›", "»", ">", "→"}

log = logging.getLogger("scraper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scraped_at  TEXT NOT NULL,          -- UTC ISO-8601
    pages       INTEGER NOT NULL DEFAULT 0,
    row_count   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,          -- ok | error
    error       TEXT,
    content_hash TEXT                   -- hash of all rows; equal hashes = no change
);
CREATE TABLE IF NOT EXISTS entries (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    page        INTEGER NOT NULL,
    position    INTEGER NOT NULL,       -- order across all pages, 1-based
    rank        INTEGER,
    name        TEXT,
    score       REAL,
    data        TEXT NOT NULL           -- JSON object with every column as scraped
);
CREATE INDEX IF NOT EXISTS idx_entries_snapshot ON entries(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_entries_name ON entries(name);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(scraped_at);
"""


# --------------------------------------------------------------------------- parsing

def slug(text: str) -> str:
    text = text.strip().lower()
    if text == "#":
        return "#"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "col"


def to_number(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def find_table(soup: BeautifulSoup, want_headers: list[str] | None = None):
    """Return the leaderboard table.

    On the first page, pick the table with headers and the most rows. On later pages,
    only accept a table whose headers match the first page's, so an empty leaderboard
    past the last page isn't confused with some other table on the page.
    """
    tables = soup.find_all("table")
    if want_headers is not None:
        return next((t for t in tables if parse_table(t)[0] == want_headers), None)
    if not tables:
        return None
    return max(tables, key=lambda t: (bool(parse_table(t)[0]), len(t.find_all("tr"))))


def parse_table(table) -> tuple[list[str], list[dict[str, str]]]:
    rows = table.find_all("tr")
    headers: list[str] = []
    head = table.find("thead")
    header_row = head.find("tr") if head else None
    if header_row is None and rows and rows[0].find("th") and not rows[0].find("td"):
        header_row = rows[0]
    if header_row is not None:
        headers = [slug(c.get_text(" ", strip=True)) for c in header_row.find_all(["th", "td"])]

    # De-duplicate header names.
    seen: dict[str, int] = {}
    for i, h in enumerate(headers):
        if h in seen:
            seen[h] += 1
            headers[i] = f"{h}_{seen[h]}"
        else:
            seen[h] = 1

    records = []
    for tr in rows:
        if tr is header_row:
            continue
        cells = tr.find_all(["td", "th"])
        if not cells or not tr.find("td"):
            continue
        values = [c.get_text(" ", strip=True) for c in cells]
        if not any(values):
            continue
        record = {}
        for i, v in enumerate(values):
            key = headers[i] if i < len(headers) and headers[i] else f"col_{i + 1}"
            record[key] = v
        records.append(record)
    return headers, records


def pick(record: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        if k in record:
            return record[k]
    for col, val in record.items():
        if any(k in col for k in keys if len(k) > 2):
            return val
    return None


def next_page_url(soup: BeautifulSoup, current_url: str, page_no: int) -> str | None:
    """Find the next page link; fall back to incrementing a ?page= query parameter."""
    link = soup.find("a", rel=lambda r: r and "next" in r)
    if link is None:
        for a in soup.find_all("a", href=True):
            label = (a.get_text(" ", strip=True) or a.get("aria-label", "")).strip().lower()
            if label in NEXT_TEXT or a.get("aria-label", "").lower().startswith("next"):
                link = a
                break
    if link is not None and link.get("href") and not link.get("href").startswith(("#", "javascript")):
        classes = " ".join(link.get("class", [])) + " " + " ".join(link.parent.get("class", []))
        if "disabled" not in classes:
            return urljoin(current_url, link["href"])

    parts = urlparse(current_url)
    query = parse_qs(parts.query)
    query["page"] = [str(page_no + 1)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


# --------------------------------------------------------------------------- scraping

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "track-leaderboard/1.0 (+https://github.com/leeablett/track-leaderboard)",
        "Accept": "text/html,application/xhtml+xml",
    })
    return s


def fetch(session: requests.Session, url: str, retries: int = 3) -> str:
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            log.warning("fetch %s failed (%s), retry %d/%d", url, exc, attempt, retries)
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def save_debug(page_no: int, html: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"page_{page_no}.html"
    path.write_text(html, encoding="utf-8")
    return path


def scrape(url: str, max_pages: int, delay: float) -> tuple[list[dict], int]:
    """Return (entries, pages_scraped). Each entry has page/position/rank/name/score/data."""
    session = make_session()
    entries: list[dict] = []
    visited: set[str] = set()
    prev_signature = None
    headers: list[str] | None = None
    page_no = 1
    current = url

    while current and page_no <= max_pages and current not in visited:
        visited.add(current)
        html = fetch(session, current)
        soup = BeautifulSoup(html, "html.parser")
        table = find_table(soup, headers)
        if table is None:
            if page_no == 1:
                path = save_debug(page_no, html)
                raise RuntimeError(
                    f"no <table> found on {current}; page saved to {path}. "
                    "The leaderboard may be rendered by JavaScript or not use a table."
                )
            break

        page_headers, records = parse_table(table)
        if headers is None:
            headers = page_headers
        signature = hashlib.sha1(json.dumps(records, sort_keys=True).encode()).hexdigest()
        if not records or signature == prev_signature:
            break  # empty page or the site returned the same page again: we're past the end
        prev_signature = signature

        for rec in records:
            entries.append({
                "page": page_no,
                "position": len(entries) + 1,
                "rank": (lambda n: int(n) if n is not None else None)(to_number(pick(rec, RANK_KEYS))),
                "name": pick(rec, NAME_KEYS),
                "score": to_number(pick(rec, SCORE_KEYS)),
                "data": rec,
            })
        log.debug("page %d: %d rows (%s)", page_no, len(records), current)

        current = next_page_url(soup, current, page_no)
        page_no += 1
        if delay:
            time.sleep(delay)

    return entries, page_no - 1


# --------------------------------------------------------------------------- storage

def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def store(conn: sqlite3.Connection, scraped_at: str, entries: list[dict], pages: int,
          error: str | None = None) -> int:
    content_hash = hashlib.sha1(
        json.dumps([e["data"] for e in entries], sort_keys=True).encode()
    ).hexdigest() if entries else None
    with conn:
        cur = conn.execute(
            "INSERT INTO snapshots (scraped_at, pages, row_count, status, error, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (scraped_at, pages, len(entries), "error" if error else "ok", error, content_hash),
        )
        snapshot_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO entries (snapshot_id, page, position, rank, name, score, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(snapshot_id, e["page"], e["position"], e["rank"], e["name"], e["score"],
              json.dumps(e["data"], ensure_ascii=False)) for e in entries],
        )
    return snapshot_id


def run_once(args) -> bool:
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = connect(args.db)
    try:
        entries, pages = scrape(args.url, args.max_pages, args.page_delay)
        if not entries:
            raise RuntimeError("scrape returned no rows")
        sid = store(conn, scraped_at, entries, pages)
        log.info("snapshot %d: %d rows from %d page(s)", sid, len(entries), pages)
        return True
    except Exception as exc:  # record failures so gaps in the data are explainable
        store(conn, scraped_at, [], 0, error=str(exc))
        log.error("scrape failed: %s", exc)
        return False
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="scrape once and exit (default)")
    mode.add_argument("--loop", type=int, metavar="SECONDS", help="scrape repeatedly every SECONDS")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--max-pages", type=int, default=200, help="safety limit on pages per run")
    p.add_argument("--page-delay", type=float, default=0.5, help="seconds between page requests")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.loop:
        return 0 if run_once(args) else 1

    log.info("scraping %s every %ds (Ctrl+C to stop)", args.url, args.loop)
    try:
        while True:
            started = time.monotonic()
            run_once(args)
            time.sleep(max(0.0, args.loop - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
