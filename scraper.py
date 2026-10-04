#!/usr/bin/env python3
"""Scrape the VFM leaderboard (all pages) and store each run as a snapshot in SQLite.

Usage:
    python scraper.py --once              # scrape once and exit (use with cron)
    python scraper.py --loop 120          # scrape every 120 seconds until stopped
    python scraper.py --once --browser    # use a real browser (JavaScript sites, firewalls)
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
            if resp.status_code in (403, 406) or "Mod_Security" in resp.text[:2000]:
                raise RuntimeError(
                    f"HTTP {resp.status_code}: the site's firewall blocked the request; "
                    "try running with --browser"
                )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            # Retrying won't fix a client error (except rate limiting); it only adds load.
            if attempt == retries or (status and 400 <= status < 500 and status != 429):
                raise
            log.warning("fetch %s failed (%s), retry %d/%d", url, exc, attempt, retries)
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def save_debug(page_no: int, html: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"page_{page_no}.html"
    path.write_text(html, encoding="utf-8")
    return path


class HttpPager:
    """Fetches pages with plain HTTP requests (fast, but no JavaScript)."""

    def __init__(self):
        self.session = make_session()
        self.url = None
        self.visited: set[str] = set()

    def open(self, url: str) -> str:
        self.url = url
        self.visited.add(url)
        return fetch(self.session, url)

    def next(self, soup: BeautifulSoup, page_no: int) -> str | None:
        url = next_page_url(soup, self.url, page_no)
        if not url or url in self.visited:
            return None
        return self.open(url)

    def close(self):
        self.session.close()


NEXT_LABEL = re.compile(r"^\s*(next( page)?|›|»|>|→)\s*$", re.I)
FIRST_ROW_JS = """() => {
    const r = document.querySelector('table tbody tr') || document.querySelector('table tr:nth-child(2)');
    return r ? r.innerText : null;
}"""


class BrowserPager:
    """Drives a real Chromium browser, so JavaScript-rendered pages and
    click-to-paginate leaderboards work."""

    # Tried in order when no browser is named: Playwright's own Chromium, then the
    # browsers already on the PC (Edge is on every Windows 10/11 machine).
    CHANNELS = ("chromium", "msedge", "chrome")

    def __init__(self, headed: bool = False, channel: str = "auto"):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        tried = []
        for ch in (self.CHANNELS if channel == "auto" else (channel,)):
            try:
                kw = {} if ch == "chromium" else {"channel": ch}
                self.browser = self._pw.chromium.launch(headless=not headed, **kw)
                log.debug("using browser: %s", ch)
                break
            except Exception as exc:
                tried.append(f"{ch}: {str(exc).strip().splitlines()[0]}")
        else:
            self._pw.stop()
            raise RuntimeError(
                "couldn't start a browser. Install Microsoft Edge or Google Chrome, or run "
                "'python -m playwright install chromium'. Tried -> " + " | ".join(tried)
            )
        self.page = self.browser.new_page()
        self.status = None
        self.visited: set[str] = set()
        self.clicked = False  # paging by clicking Next; a missing/disabled Next then means the end

    def _settle(self):
        from playwright.sync_api import Error as PWError

        try:
            self.page.wait_for_selector("table tr td", timeout=20_000)
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PWError:
            pass  # no table (yet); the caller reports it

    def open(self, url: str) -> str:
        self.visited.add(url)
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.status = resp.status if resp else None
        self._settle()
        html = self.page.content()
        if "Mod_Security" in html[:2000]:
            raise RuntimeError(f"HTTP {self.status}: the site's firewall blocked the browser too")
        return html

    def _next_control(self):
        candidates = [
            self.page.locator("a[rel~='next']"),
            self.page.get_by_role("link", name=NEXT_LABEL),
            self.page.get_by_role("button", name=NEXT_LABEL),
        ]
        for loc in candidates:
            for i in range(loc.count()):
                c = loc.nth(i)
                if not (c.is_visible() and c.is_enabled()):
                    continue
                classes = (c.get_attribute("class") or "") + " " + (
                    c.evaluate("e => e.parentElement ? e.parentElement.className : ''") or "")
                if c.get_attribute("aria-disabled") == "true" or "disabled" in classes:
                    continue
                return c
        return None

    def next(self, soup: BeautifulSoup, page_no: int) -> str | None:
        from playwright.sync_api import Error as PWError

        control = self._next_control()
        if control is None:
            if self.clicked:
                return None
            # No next button/link at all: fall back to ?page=N in the URL.
            url = next_page_url(soup, self.page.url, page_no)
            if not url or url in self.visited:
                return None
            return self.open(url)

        before = self.page.evaluate(FIRST_ROW_JS)
        self.clicked = True
        control.click()
        try:
            # Wait until the table shows different rows (works for in-page and full-page navigation).
            self.page.wait_for_function(
                "prev => { const r = document.querySelector('table tbody tr') || "
                "document.querySelector('table tr:nth-child(2)'); return !r || r.innerText !== prev; }",
                arg=before, timeout=15_000,
            )
        except PWError:
            pass  # unchanged rows are caught by the duplicate-page check
        self._settle()
        return self.page.content()

    def close(self):
        self.browser.close()
        self._pw.stop()


def scrape(pager, url: str, max_pages: int, delay: float) -> tuple[list[dict], int]:
    """Return (entries, pages_scraped). Each entry has page/position/rank/name/score/data."""
    entries: list[dict] = []
    seen: set[str] = set()
    headers: list[str] | None = None
    pages = 0
    html = pager.open(url)

    while html is not None and pages < max_pages:
        page_no = pages + 1
        soup = BeautifulSoup(html, "html.parser")
        table = find_table(soup, headers)
        if table is None:
            if page_no == 1:
                path = save_debug(page_no, html)
                raise RuntimeError(
                    f"no leaderboard <table> found on {url}; page saved to {path}"
                    + ("" if isinstance(pager, BrowserPager) else
                       ". If the page is drawn by JavaScript, try --browser")
                )
            break

        page_headers, records = parse_table(table)
        if headers is None:
            headers = page_headers
        signature = hashlib.sha1(json.dumps(records, sort_keys=True).encode()).hexdigest()
        if not records or signature in seen:
            break  # empty page, or the site served a page we already have: we're past the end
        seen.add(signature)
        pages = page_no

        for rec in records:
            entries.append({
                "page": page_no,
                "position": len(entries) + 1,
                "rank": (lambda n: int(n) if n is not None else None)(to_number(pick(rec, RANK_KEYS))),
                "name": pick(rec, NAME_KEYS),
                "score": to_number(pick(rec, SCORE_KEYS)),
                "data": rec,
            })
        log.debug("page %d: %d rows", page_no, len(records))

        if delay:
            time.sleep(delay)
        html = pager.next(soup, page_no)

    return entries, pages


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
    pager = None
    try:
        pager = BrowserPager(headed=args.headed, channel=args.browser_channel) if args.browser else HttpPager()
        entries, pages = scrape(pager, args.url, args.max_pages, args.page_delay)
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
        if pager is not None:
            pager.close()
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
    p.add_argument("--browser", action="store_true",
                   help="use a real Chromium browser (needed if the site blocks plain requests "
                        "or draws the leaderboard with JavaScript)")
    p.add_argument("--headed", action="store_true", help="with --browser: show the browser window")
    p.add_argument("--browser-channel", default="auto", choices=["auto", *BrowserPager.CHANNELS],
                   help="with --browser: which browser to use (default: Playwright's Chromium if "
                        "installed, otherwise Microsoft Edge, otherwise Google Chrome)")
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
