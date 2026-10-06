#!/usr/bin/env python3
"""Scrape the VFM leaderboard (all pages) and store each run as a snapshot in SQLite.

Usage:
    python scraper.py                     # scrape every 2 minutes until stopped (Ctrl+C)
    python scraper.py --once              # scrape once and exit (a test run, or for cron)
    python scraper.py --once -v           # same, showing every page it reads
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = "https://vfm.bdynamicsstudio.com/leaderboard?season=4"
DEFAULT_DB = Path(__file__).parent / "data" / "leaderboard.db"
DEBUG_DIR = Path(__file__).parent / "data" / "debug"

# Header names used to pick out the common fields. Every column is also kept in `data`.
RANK_KEYS = ("rank", "position", "pos", "place", "#", "no")
NAME_KEYS = ("name", "player", "user", "username", "driver", "team", "manager", "club")
SCORE_KEYS = ("score", "points", "pts", "total", "rating", "value", "time")

log = logging.getLogger("scraper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    season      INTEGER,                -- leaderboard season (NULL = unknown, e.g. older data)
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
# Indexes on columns added after the first release, and handy views (recreated each time so
# they stay current). Views are plain SELECTs; they never change stored data.
SCHEMA_LATE = """
CREATE INDEX IF NOT EXISTS idx_snapshots_season ON snapshots(season, status, id);

DROP VIEW IF EXISTS latest;
CREATE VIEW latest AS                    -- the most recent successful snapshot of each season
    SELECT s.season, e.rank, e.name, e.score, e.page, e.position, s.scraped_at, e.data
    FROM entries e JOIN snapshots s ON s.id = e.snapshot_id
    WHERE s.id IN (SELECT MAX(id) FROM snapshots WHERE status = 'ok' GROUP BY season)
    ORDER BY s.season DESC, e.position;

DROP VIEW IF EXISTS history;
CREATE VIEW history AS                   -- every successful snapshot
    SELECT s.id AS snapshot_id, s.season, s.scraped_at, e.rank, e.name, e.score, e.page, e.position, e.data
    FROM entries e JOIN snapshots s ON s.id = e.snapshot_id
    WHERE s.status = 'ok';

DROP VIEW IF EXISTS seasons;
CREATE VIEW seasons AS                   -- one row per season: how much data, and when
    SELECT g.season, g.snapshots, g.first_scraped, g.last_scraped, x.row_count AS players
    FROM (SELECT season, COUNT(*) AS snapshots, MIN(scraped_at) AS first_scraped,
                 MAX(scraped_at) AS last_scraped, MAX(id) AS last_id
          FROM snapshots WHERE status = 'ok' GROUP BY season) g
    JOIN snapshots x ON x.id = g.last_id
    ORDER BY g.season;
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


def is_pager_row(values: list[str]) -> bool:
    """A "<<  Page 2  >>" row inside the table rather than a player."""
    return (len(values) <= 5 and bool(re.search(r"\bpage\s*\d+", " ".join(values), re.I))
            and any(re.fullmatch(r"[<>«»‹›]+", v) for v in values))


def own_rows(table) -> list:
    """Rows of this table only, not rows of tables nested inside its cells."""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def own_cells(tr) -> list:
    return tr.find_all(["td", "th"], recursive=False)


RANK_PREFIX = re.compile(r"#\s*\d+")
RANK_AND_NAME = re.compile(r"(#\s*\d+)\s+(.+)")


def cell_parts(cell) -> list[str]:
    """A cell's text. A cell holding its own little table (e.g. "#1 | NRG-DFC") gives one part
    per inner cell, so the rank and the name can be told apart."""
    inner = cell.find("table")
    if inner is not None:
        parts = [c.get_text(" ", strip=True) for c in inner.find_all(["td", "th"])]
        parts = [p for p in parts if p]
        if parts:
            return parts
    return [cell.get_text(" ", strip=True)]


def find_header_row(rows):
    """The column-headings row: a row of <th> cells, or (if the site uses ordinary cells) the
    first row of all-text cells followed by rows containing numbers. Pager rows are skipped."""
    first = []
    for tr in rows[:5]:
        cells = own_cells(tr)
        values = [c.get_text(" ", strip=True) for c in cells]
        if not cells or not any(values) or is_pager_row(values):
            continue
        if all(c.name == "th" for c in cells):
            return tr
        first.append((tr, values))
    if first:
        tr, values = first[0]
        looks_like_text = all(v and not re.search(r"\d", v) and len(v) <= 30 for v in values)
        numbers_below = any(re.search(r"\d", " ".join(v)) for _, v in first[1:])
        if len(values) >= 2 and looks_like_text and numbers_below:
            return tr
    return None


def parse_table(table) -> tuple[list[str], list[dict[str, str]]]:
    rows = own_rows(table)
    headers: list[str] = []
    head = table.find("thead")
    header_row = head.find("tr") if head is not None and head.find_parent("table") is table else None
    if header_row is None:
        header_row = find_header_row(rows)
    if header_row is not None:
        headers = [slug(c.get_text(" ", strip=True)) for c in own_cells(header_row)]

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
        cells = own_cells(tr)
        if not cells or not any(c.name == "td" for c in cells):
            continue
        values = [c.get_text(" ", strip=True) for c in cells]
        if not any(values):
            continue
        if is_pager_row(values):
            continue  # a pager row ("<<  Page 2  >>") inside the table, not a player
        record, rank = {}, None
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) and headers[i] else f"col_{i + 1}"
            parts = cell_parts(cell)
            # "#1 NRG-DFC" in one cell (or split across an inner table): keep the rank separately.
            if rank is None and "rank" not in headers:
                if len(parts) >= 2 and RANK_PREFIX.fullmatch(parts[0]):
                    rank, parts = parts[0], parts[1:]
                elif len(parts) == 1 and (m := RANK_AND_NAME.fullmatch(parts[0])):
                    rank, parts = m.group(1), [m.group(2)]
            record[key] = " ".join(parts)
        if rank is not None:
            record = {"rank": rank.replace(" ", ""), **record}
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


# How a "next page" control is recognised, in order of preference:
#   1. rel="next"
#   2. its label: "Next", "Next »", "Next page ›", "Go to next page", or just an arrow (›, », >, →)
#   3. "next" in its class or id (e.g. pagination-next, page-next)
#   4. a link/button labelled with the next page number (numbered pagination: 1 2 3 … 9)
#   5. "Load more" / "Show more" (browser mode only)
# A matching control that is disabled means we're on the last page.
NEXT_LABEL = re.compile(r"(go to |show |view )?(the )?next( page)?\W*|[›»>→⟩❯▶]+", re.I)
NEXT_CLASS = re.compile(r"(^|[-_\s])next($|[-_\s])", re.I)
MORE_LABEL = re.compile(r"(load|show|view|see)\s+more\b.{0,20}", re.I)


def _label(el) -> str:
    return " ".join((el.get_text(" ", strip=True) or el.get("aria-label") or el.get("title") or "").split())


def _is_disabled(el) -> bool:
    if el.has_attr("disabled") or el.get("aria-disabled") == "true":
        return True
    for node in (el, el.parent):
        if node is not None and "disabled" in " ".join(node.get("class", [])).lower():
            return True
    return False


def next_page_url(soup: BeautifulSoup, current_url: str, page_no: int) -> tuple[str | None, str]:
    """Return (url of the next page or None, how it was found / why we stopped)."""
    controls = [el for el in soup.find_all(["a", "button", "span", "li"]) if not el.find_parent("table")]

    def classes(el):
        return " ".join(el.get("class", []) + [el.get("id", "")] +
                        (el.parent.get("class", []) if el.parent else []))

    tiers = [
        ("rel=next link", lambda el: el.name == "a" and "next" in (el.get("rel") or [])),
        ("Next label", lambda el: el.name in ("a", "button", "span") and bool(NEXT_LABEL.fullmatch(_label(el)))),
        ("'next' class", lambda el: el.name in ("a", "button", "li") and bool(NEXT_CLASS.search(classes(el)))),
    ]
    for how, test in tiers:
        found = [el for el in controls if test(el)]
        if not found:
            continue
        el = found[0]
        if el.name == "li":
            el = el.find("a") or el
        href = el.get("href") or ""
        if _is_disabled(el) or not href or href.startswith(("#", "javascript")):
            if _is_disabled(el) or el.name == "span":
                return None, f"the {how} is disabled (last page)"
            continue
        return urljoin(current_url, href), how

    want = str(page_no + 1)
    for el in controls:
        if el.name == "a" and _label(el) == want and el.get("href") and not el["href"].startswith(("#", "javascript")):
            return urljoin(current_url, el["href"]), f"page number {want} link"

    parts = urlparse(current_url)
    query = parse_qs(parts.query)
    query["page"] = [str(page_no + 1)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True))), "guessed ?page= in the address"


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
            if "charset" not in resp.headers.get("Content-Type", "").lower():
                resp.encoding = resp.apparent_encoding  # else "›" in "Next ›" arrives garbled
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
        self.stop_reason = ""

    def open(self, url: str) -> str:
        self.url = url
        self.visited.add(url)
        return fetch(self.session, url)

    def next(self, soup: BeautifulSoup, page_no: int) -> str | None:
        url, how = next_page_url(soup, self.url, page_no)
        if not url:
            self.stop_reason = how
            return None
        if url in self.visited:
            self.stop_reason = "the next-page link points to a page already read"
            return None
        log.debug("page %d -> %d via %s", page_no, page_no + 1, how)
        return self.open(url)

    def retry(self, page_no: int) -> str | None:
        return None  # a plain request can't be "still loading"

    def log_controls(self):
        pass

    def screenshot(self, path: Path):
        pass

    def close(self):
        self.session.close()


# Browser-side version of the rules in next_page_url(). Marks the chosen control with
# data-lb-next so Python can click it.
NEXT_JS = r"""({ want, custom }) => {
  document.querySelectorAll('[data-lb-next]').forEach(e => e.removeAttribute('data-lb-next'));
  const LABEL = /^(?:(?:go to |show |view )?(?:the )?next(?: page)?\W*|[›»>→⟩❯▶]+)$/i;
  const CLASS = /(^|[-_\s])next($|[-_\s])/i;
  const ICON = /(angles?-right|double-right|chevron-right|angle-right|arrow-right|caret-right|arrow_forward|navigate_next|forward|next)/i;
  const MORE = /^(load|show|view|see)\s+more\b.{0,20}$/i;
  const PAGER = /pagina|pager|page-?nav|page-?select|pages\b|paging/i;
  const cls = e => [typeof e.className === 'string' ? e.className : '', e.id || '',
                    e.parentElement && typeof e.parentElement.className === 'string' ? e.parentElement.className : ''].join(' ');
  const label = e => ((e.tagName === 'INPUT' ? (e.value || '') : (e.innerText || '').trim()) ||
                      e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('alt') || '').replace(/\s+/g, ' ');
  const norm = t => t.replace(/»/g, '>>').replace(/«/g, '<<').replace(/›/g, '>').replace(/‹/g, '<').replace(/\s+/g, '').toLowerCase();
  const visible = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
                         return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const disabled = e => e.disabled || e.getAttribute('aria-disabled') === 'true' ||
                        /\bdisabled\b/i.test(cls(e)) || !!e.closest('[disabled], [aria-disabled="true"]');
  // Candidates: real links/buttons, plus any element whose own text is an arrow / "Next" /
  // "Load more" (sites often make a plain <div> or <span> clickable with script). Controls
  // inside tables count too: older sites lay out their pager with one.
  const CLICKABLE = 'a, button, input[type=submit], input[type=button], input[type=image], [role=button], [role=link], [onclick], [tabindex]';
  const pointer = e => { for (let n = e, i = 0; n && i < 3; i++, n = n.parentElement)
                           if (getComputedStyle(n).cursor === 'pointer') return n;
                         return e; };
  const lift = e => e.closest(CLICKABLE) || pointer(e);
  const leaves = [...document.body.querySelectorAll('*')].filter(e => e.children.length === 0 && visible(e) &&
                   (LABEL.test(label(e)) || MORE.test(label(e)) || (custom && norm(label(e)) === norm(custom))));
  const els = [...new Set([...document.querySelectorAll(CLICKABLE), ...leaves.map(lift)])].filter(visible);
  // The leaderboard table: the biggest one.
  const table = [...document.querySelectorAll('table')].sort((a, b) => b.rows.length - a.rows.length)[0];
  const tb = table ? table.getBoundingClientRect() : null;
  // Is this control part of the pagination bar? (a "pagination"-style container, or next to page numbers)
  const inPager = e => {
    let node = e.parentElement;
    for (let i = 0; node && i < 4; i++, node = node.parentElement) {
      if (table && node.contains(table)) return false;  // gone too far up: that's the whole page
      if (PAGER.test((typeof node.className === 'string' ? node.className : '') + ' ' + (node.id || '') + ' ' + (node.getAttribute('aria-label') || ''))) return true;
      const nums = [...node.querySelectorAll('a, button, input, [role=button]')].filter(x => /^\d+$/.test(label(x)));
      if (nums.length >= 2) return true;
      const text = (node.innerText || '').trim();  // a small bar like "<<  Page 2  >>"
      if (text.length < 80 && /\bpage\s*\d+/i.test(text)) return true;
    }
    return false;
  };
  // Prefer controls in the pagination bar, then ones below the table, then page order.
  const score = e => (inPager(e) ? 2 : 0) + (tb && e.getBoundingClientRect().top >= tb.bottom - 5 ? 1 : 0);
  const best = found => found.map((e, i) => [score(e), -i, e]).sort((a, b) => b[0] - a[0] || b[1] - a[1]).map(x => x[2]);
  const describe = e => `${e.tagName.toLowerCase()} "${label(e).slice(0, 30)}"` +
                        (typeof e.className === 'string' && e.className ? ` class="${e.className.slice(0, 40)}"` : '') +
                        (inPager(e) ? ' [in pagination]' : '') + (disabled(e) ? ' [disabled]' : '');
  let tiers;
  if (custom) {
    // --next: a label (">>" also matches "»") or a CSS selector
    let bySel = [];
    try { bySel = [...document.querySelectorAll(custom)].filter(visible); } catch (err) {}
    tiers = [['--next ' + custom, e => norm(label(e)) === norm(custom) || bySel.includes(e), true]];
  } else {
    tiers = [
      ['rel=next link', e => (e.getAttribute('rel') || '').split(/\s+/).includes('next'), true],
      ['Next label', e => LABEL.test(label(e)), true],
      ["'next' class", e => CLASS.test(cls(e)), true],
      ['next arrow icon', e => !/\w/.test(label(e)) && ICON.test(e.innerHTML) && inPager(e), true],
      ['page number ' + want, e => label(e) === String(want) && inPager(e), false],
      ['Load more button', e => MORE.test(label(e)), true],
    ];
  }
  const candidates = els.filter(e => inPager(e) || LABEL.test(label(e)) || CLASS.test(cls(e)) || /^\d+$/.test(label(e)))
                        .slice(0, 25).map(describe);
  for (const [how, test, endIfDisabled] of tiers) {
    const found = els.filter(test);
    if (!found.length) continue;
    // If the pagination bar has a match, only it counts: a disabled one there means the last
    // page, even if some other arrow elsewhere on the page (e.g. a round switcher) is enabled.
    const inBar = found.filter(inPager);
    const usable = best(inBar.length ? inBar : found).find(e => !disabled(e));
    if (!usable) { if (endIfDisabled) return { action: 'end', how: 'the ' + how + ' is disabled (last page)', candidates }; continue; }
    usable.setAttribute('data-lb-next', '1');
    return { action: 'click', how: how + ': ' + describe(usable), candidates };
  }
  return { action: 'none', candidates };
}"""

# A short fingerprint of the leaderboard table: row count plus first and last row.
TABLE_STATE_JS = """() => {
  const t = [...document.querySelectorAll('table')].sort((a, b) => b.rows.length - a.rows.length)[0];
  if (!t) return '';
  const rows = t.querySelectorAll('tbody tr').length ? t.querySelectorAll('tbody tr') : t.querySelectorAll('tr');
  return rows.length + '|' + (rows[0] ? rows[0].innerText : '') + '|' + (rows.length ? rows[rows.length - 1].innerText : '');
}"""


# Addresses of downloads the scraper never needs (images, fonts, video).
SKIP_DOWNLOADS = re.compile(r"\.(png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|mp4|webm|mp3|wav)(\?|#|$)", re.I)


# Resolves once the table has looked the same for 300 ms (checked every 50 ms), or after 3 s.
TABLE_STABLE_JS = f"""async () => {{
  const state = {TABLE_STATE_JS.strip()};
  let last = state(), since = performance.now();
  const end = since + 3000;
  while (performance.now() < end) {{
    await new Promise(r => setTimeout(r, 50));
    const now = state();
    if (now !== last) {{ last = now; since = performance.now(); }}
    else if (performance.now() - since >= 300) return true;
  }}
  return false;
}}"""


class BrowserPager:
    """Drives a real Chromium browser, so JavaScript-rendered pages and
    click-to-paginate leaderboards work."""

    # Tried in order when no browser is named: Playwright's own Chromium, then the
    # browsers already on the PC (Edge is on every Windows 10/11 machine).
    CHANNELS = ("chromium", "msedge", "chrome")

    def __init__(self, headed: bool = False, channel: str = "auto", next_control: str | None = None):
        from playwright.sync_api import sync_playwright

        self.next_control = next_control  # --next: label or CSS selector of the next-page control

        started = time.monotonic()
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
        self.page = self.browser.new_page(viewport={"width": 1366, "height": 900})
        # Images, fonts and video aren't needed to read the table; skipping them saves work,
        # which matters on older PCs. (Only matching addresses are intercepted.)
        self.page.route(SKIP_DOWNLOADS, lambda route: route.abort())
        self.launch_seconds = time.monotonic() - started
        # How long the slowest page took to appear after clicking Next, learned as we go and
        # kept between runs. Waits for "has the page changed?" scale with it (see _wait_time).
        self.slowest: float | None = None
        self.runs = 0
        self.reset()

    def reset(self):
        """Forget the previous scrape (the browser itself is kept for the next run)."""
        self.status = None
        self.visited: set[str] = set()
        self.clicked = False  # paging by clicking; no control found afterwards means the end
        self.stop_reason = ""
        self.runs += 1

    def _wait_time(self, factor: float, low: float, high: float = 10.0) -> int:
        """Milliseconds to wait for a page change: a few times the slowest page seen so far,
        within [low, high] seconds. Until we've seen a page change, wait the full `high`.
        On a quick site this makes the last-page check take seconds instead of half a minute;
        on a slow PC or site the waits grow to match."""
        if self.slowest is None:
            return int(high * 1000)
        return int(min(high, max(low, factor * self.slowest)) * 1000)

    def _settle(self):
        """Wait until the leaderboard is on screen and has stopped changing.

        This deliberately doesn't wait for the network to go quiet: sites like this one keep a
        live connection to their server (Blazor), and on some PCs it falls back to a constant
        stream of small requests, so "network idle" never comes and every page cost 3 s extra.
        """
        from playwright.sync_api import Error as PWError

        try:
            self.page.wait_for_selector("table tr td", timeout=20_000)
        except PWError:
            return  # no table (yet); the caller reports it
        try:
            self.page.evaluate(TABLE_STABLE_JS)
        except PWError:
            pass  # the page navigated while we watched; the caller's checks cover that

    def open(self, url: str) -> str:
        self.visited.add(url)
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self.status = resp.status if resp else None
        self._settle()
        html = self.page.content()
        if "Mod_Security" in html[:2000]:
            raise RuntimeError(f"HTTP {self.status}: the site's firewall blocked the browser too")
        return html

    def next(self, soup: BeautifulSoup, page_no: int) -> str | None:
        from playwright.sync_api import Error as PWError

        found = self._find_next(page_no + 1)
        if found["action"] == "end":
            self.stop_reason = found["how"]
            return None
        if found["action"] == "none":
            if self.next_control:
                self.stop_reason = f"no control matching --next {self.next_control!r} after page {page_no}"
                return None
            if self.clicked:
                self.stop_reason = f"no next-page control after page {page_no}"
                return None
            url, how = next_page_url(soup, self.page.url, page_no)
            if not url or url in self.visited:
                self.stop_reason = how if not url else "no next-page control found"
                return None
            log.debug("page %d -> %d via %s", page_no, page_no + 1, how)
            return self.open(url)

        log.debug("page %d -> %d via %s", page_no, page_no + 1, found["how"])
        before = self.page.evaluate(TABLE_STATE_JS)
        self.clicked = True
        clicked_at = time.monotonic()
        self._click_next()
        # Wait until the table changes (new rows, more rows, or replaced while loading).
        # If it doesn't, the repeated-rows check in scrape() asks for a retry.
        if self._wait_for_change(before, self._wait_time(4, 4)):
            took = time.monotonic() - clicked_at
            self.slowest = max(self.slowest or 0.0, took)
        self._settle()
        return self.page.content()

    def _find_next(self, want: int) -> dict:
        found = self.page.evaluate(NEXT_JS, {"want": want, "custom": self.next_control})
        if want == 2 or found["action"] != "click":
            # Show what pagination controls were seen (with -v / --diagnose), to help if paging goes wrong.
            log.debug("pagination controls seen: %s", "; ".join(found.get("candidates") or []) or "none")
        return found

    def _click_next(self):
        # Mark the current document. Form buttons (like "<<" / ">>") reload the whole page;
        # the new page won't carry the mark, which tells _wait_for_change it has arrived.
        from playwright.sync_api import Error as PWError

        self.page.evaluate("document.documentElement.dataset.lbOld = '1'")
        try:
            self.page.locator("[data-lb-next]").first.click(timeout=5_000)
        except PWError as exc:
            # The control was redrawn or removed just as we clicked (e.g. a slow page finished
            # loading). Don't fail the run: the wait that follows checks whether the page moved on.
            log.debug("click on the next-page control didn't go through: %s", str(exc).splitlines()[0])

    def _wait_for_change(self, before: str, timeout_ms: int, reloaded: bool = True) -> bool:
        """Wait until the table differs from `before` (or, if `reloaded`, a new page has loaded)."""
        from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout

        deadline = time.monotonic() + timeout_ms / 1000
        check = (f"([prev, reloaded]) => (reloaded && !document.documentElement.dataset.lbOld) "
                 f"|| ({TABLE_STATE_JS})() !== prev")
        while (left := deadline - time.monotonic()) > 0:
            try:
                self.page.wait_for_function(check, arg=[before, reloaded], timeout=left * 1000)
                return True
            except PWTimeout:
                return False
            except PWError:
                # The page navigated while we were checking: wait for the new one, then re-check.
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=max(1, left * 1000))
                except PWError:
                    pass
        return False

    def retry(self, page_no: int) -> str | None:
        """Page `page_no` showed only rows we already have. It may still be loading, or the
        click may not have registered: wait a little longer, then click Next once more."""
        before = self.page.evaluate(TABLE_STATE_JS)
        if self._wait_for_change(before, self._wait_time(2, 2), reloaded=False):
            log.info("page %d was slow to load; read it again", page_no)
            self._settle()
            return self.page.content()
        found = self._find_next(page_no)
        if found["action"] != "click":
            return None
        shown = re.search(r'"([^"]*)"', found["how"])
        log.info("checking whether page %d is the last: clicking %s once more", page_no - 1,
                 shown.group(1) if shown else "Next")
        self._click_next()
        if not self._wait_for_change(before, self._wait_time(2, 2)):
            return None
        self._settle()
        return self.page.content()

    def log_controls(self):
        """Say which pagination controls are on the page (used when paging stops unexpectedly)."""
        try:
            found = self.page.evaluate(NEXT_JS, {"want": 0, "custom": self.next_control})
            log.info("pagination controls on the page: %s", "; ".join(found.get("candidates") or []) or "none found")
        except Exception:
            pass

    def screenshot(self, path: Path):
        self.page.screenshot(path=str(path), full_page=True)

    def close(self):
        self.browser.close()
        self._pw.stop()


def row_key(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, ensure_ascii=False)


def expected_totals(soup: BeautifulSoup) -> tuple[int | None, int | None]:
    """Page/row totals the site itself shows, e.g. "Page 1 of 37" or "1–25 of 912"."""
    text = " ".join(soup.get_text(" ").split())
    pages = re.search(r"\bpage\s+\d+\s*(?:of|/)\s*(\d[\d,]*)", text, re.I)
    rows = re.search(r"\b\d[\d,]*\s*[–-]\s*\d[\d,]*\s+of\s+(\d[\d,]*)", text)
    as_int = lambda m: int(m.group(1).replace(",", "")) if m else None
    return as_int(pages), as_int(rows)


def scrape(pager, url: str, max_pages: int, delay: float, diagnose: bool = False,
           label: str = "") -> tuple[list[dict], int]:
    """Return (entries, pages_scraped). Each entry has page/position/rank/name/score/data."""
    entries: list[dict] = []
    seen_rows: dict[str, int] = {}  # row -> page it was first read on
    retried = False
    headers: list[str] | None = None
    exp_pages = exp_rows = None
    pages = 0
    reason = ""
    started = time.monotonic()
    page_times: list[float] = []  # seconds from reading one page to having the next on screen
    log.info("%sreading %s", label, url)
    html = pager.open(url)
    first_page = time.monotonic() - started
    last_page_done = started  # when the most recent new page was read

    while True:
        page_no = pages + 1
        soup = BeautifulSoup(html, "html.parser")
        if diagnose:
            save_debug(page_no, html)
            pager.screenshot(DEBUG_DIR / f"page_{page_no}.png")
        table = find_table(soup, headers)
        if table is None:
            if page_no == 1:
                path = save_debug(page_no, html)
                raise RuntimeError(
                    f"no leaderboard <table> found on {url}; page saved to {path}"
                    + ("" if isinstance(pager, BrowserPager) else
                       ". If the page is drawn by JavaScript, try --browser")
                )
            reason = f"page {page_no} has no leaderboard table"
            break

        page_headers, records = parse_table(table)
        if headers is None:
            headers = page_headers
            exp_pages, exp_rows = expected_totals(soup)
        # Keep only rows we haven't stored yet. This handles "Load more" (the table grows)
        # and sites that show page 1 again when asked for a page past the end.
        new = [r for r in records if row_key(r) not in seen_rows]
        if not records:
            reason = f"page {page_no} is empty"
            break
        if not new:
            # Either we're past the end (some sites re-show the last or first page), or the
            # page hasn't finished loading / the click didn't register. Retry once to be sure.
            if not retried and page_no > 1:
                retried = True
                again = pager.retry(page_no)
                if again is not None:
                    html = again
                    continue
            same_as = sorted({seen_rows[row_key(r)] for r in records})
            if same_as == [page_no - 1]:
                # Clicking Next (twice) left the page as it was: some sites keep Next enabled
                # on the last page.
                reason = f"Next no longer changes the page, so page {page_no - 1} is the last"
            else:
                reason = (f"page {page_no} only repeated rows already read "
                          f"(the same rows as page {', '.join(map(str, same_as))})")
            break
        retried = False
        for r in new:
            seen_rows[row_key(r)] = page_no
        pages = page_no
        now = time.monotonic()
        # Progress: this page's time (getting it on screen and reading it) and the running total.
        log.info("%spage %d: %d rows in %.1f s (%.1f s so far)", label, page_no, len(new),
                 now - last_page_done, now - started)
        if page_no == 1:
            first_page = now - started
        last_page_done = now

        for rec in new:
            entries.append({
                "page": page_no,
                "position": len(entries) + 1,
                "rank": (lambda n: int(n) if n is not None else None)(to_number(pick(rec, RANK_KEYS))),
                "name": pick(rec, NAME_KEYS),
                "score": to_number(pick(rec, SCORE_KEYS)),
                "data": rec,
            })
        log.debug("page %d: %d new rows", page_no, len(new))

        if pages >= max_pages:
            reason = f"reached the --max-pages limit of {max_pages}"
            log.warning("stopped at --max-pages %d; there may be more pages (raise --max-pages)", max_pages)
            break
        if delay:
            time.sleep(delay)
        t = time.monotonic()
        html = pager.next(soup, page_no)
        page_times.append(time.monotonic() - t)
        if html is None:
            reason = pager.stop_reason
            break

    log.info("%sread %d page(s), %d rows; stopped because %s", label, pages, len(entries), reason or "done")
    # Where the time went, so a slow run can be explained: the first page, the other pages
    # (including the pause between pages and reading each one), and the end check (making
    # sure there's no further page). The three add up to the total.
    ended = time.monotonic()
    moves = page_times[:pages - 1]
    log.info("%stook %.1f s: first page %.1f s, %d more page(s) %.1f s (slowest %.1f s), end check %.1f s",
             label, ended - started, first_page, pages - 1, max(0.0, last_page_done - started - first_page),
             max(moves, default=0), ended - last_page_done)
    if "repeated rows" in reason or "no next-page control" in reason or "no control matching" in reason:
        pager.log_controls()
    if exp_pages and pages < exp_pages:
        log.warning("the site says there are %d pages but only %d were read", exp_pages, pages)
    if exp_rows and len(entries) < exp_rows:
        log.warning("the site says there are %d rows but only %d were read", exp_rows, len(entries))
    return entries, pages


# --------------------------------------------------------------------------- storage

def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.executescript(SCHEMA_LATE)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a database made by an older version up to date. Existing data is kept."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    if "season" not in cols:
        with conn:
            conn.execute("ALTER TABLE snapshots ADD COLUMN season INTEGER")


def current_season(conn: sqlite3.Connection) -> int | None:
    """The current season: the highest season number with data (what "now" means by default).
    Backfilling an older season later doesn't change it. None if no data has a season."""
    row = conn.execute("SELECT MAX(season) FROM snapshots WHERE status = 'ok'").fetchone()
    return row[0] if row else None


def season_of(url: str) -> int | None:
    """The season in a leaderboard address (…/leaderboard?season=4), if any."""
    value = parse_qs(urlparse(url).query).get("season", [None])[0]
    return int(value) if value and value.isdigit() else None


def with_season(url: str, season: int) -> str:
    parts = urlparse(url)
    query = parse_qs(parts.query)
    query["season"] = [str(season)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def parse_seasons(text: str) -> list[int]:
    """'4' -> [4], '3,4' -> [3, 4], '1-4' -> [1, 2, 3, 4]."""
    seasons: list[int] = []
    for part in text.replace(" ", "").split(","):
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            seasons.extend(range(lo, hi + 1))
        elif part:
            seasons.append(int(part))
    return sorted(set(seasons))


def store(conn: sqlite3.Connection, scraped_at: str, entries: list[dict], pages: int,
          error: str | None = None, season: int | None = None) -> int:
    content_hash = hashlib.sha1(
        json.dumps([e["data"] for e in entries], sort_keys=True).encode()
    ).hexdigest() if entries else None
    with conn:
        cur = conn.execute(
            "INSERT INTO snapshots (season, scraped_at, pages, row_count, status, error, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (season, scraped_at, pages, len(entries), "error" if error else "ok", error, content_hash),
        )
        snapshot_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO entries (snapshot_id, page, position, rank, name, score, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(snapshot_id, e["page"], e["position"], e["rank"], e["name"], e["score"],
              json.dumps(e["data"], ensure_ascii=False)) for e in entries],
        )
    return snapshot_id


class Browsers:
    """Keeps one browser open between runs, instead of starting a new one every 2 minutes
    (slow on older PCs). It's replaced after an error and once an hour, to stay healthy."""

    RUNS_PER_BROWSER = 30

    def __init__(self, args):
        self.args = args
        self.pager: BrowserPager | None = None

    def get(self) -> BrowserPager:
        if self.pager is not None and self.pager.runs >= self.RUNS_PER_BROWSER:
            self.discard()
        if self.pager is None:
            a = self.args
            self.pager = BrowserPager(headed=a.headed, channel=a.browser_channel, next_control=a.next)
            log.info("browser started in %.1f s", self.pager.launch_seconds)
        else:
            self.pager.reset()
        return self.pager

    def discard(self):
        if self.pager is not None:
            try:
                self.pager.close()
            except Exception:
                pass
            self.pager = None


def scrape_season(args, conn: sqlite3.Connection, url: str, season: int | None,
                  browsers: Browsers | None = None) -> bool:
    """Scrape one season's leaderboard (all pages) and store it as one snapshot."""
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    label = f"season {season}: " if season is not None else ""
    pager = None
    keep = False  # a shared browser stays open after a successful run
    try:
        if args.browser and browsers is not None:
            pager = browsers.get()
        else:
            pager = (BrowserPager(headed=args.headed, channel=args.browser_channel, next_control=args.next)
                     if args.browser else HttpPager())
        entries, pages = scrape(pager, url, args.max_pages, args.page_delay, args.diagnose, label)
        if not entries:
            raise RuntimeError("scrape returned no rows")
        sid = store(conn, scraped_at, entries, pages, season=season)
        log.info("%ssnapshot %d: %d rows from %d page(s)", label, sid, len(entries), pages)
        keep = browsers is not None and pager is browsers.pager
        return True
    except KeyboardInterrupt:
        # Ctrl+C mid-scrape: the browser connection is already cut, and closing it politely
        # would hang. Leave it; the browser exits with this program.
        pager = None
        raise
    except Exception as exc:  # record failures so gaps in the data are explainable
        store(conn, scraped_at, [], 0, error=str(exc), season=season)
        log.error("%sscrape failed: %s", label, exc)
        return False
    finally:
        if pager is not None and not keep:
            if browsers is not None and pager is browsers.pager:
                browsers.discard()  # after an error, start the next run with a fresh browser
            else:
                pager.close()


def run_once(args, browsers: Browsers | None = None) -> bool:
    """Scrape every season asked for (--season), or the one in --url."""
    seasons = parse_seasons(args.season) if args.season else [season_of(args.url)]
    conn = connect(args.db)
    try:
        results = [scrape_season(args, conn, with_season(args.url, s) if s is not None else args.url, s, browsers)
                   for s in seasons]
    finally:
        conn.close()
    return all(results)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="scrape once and exit")
    mode.add_argument("--loop", type=int, metavar="SECONDS", default=120,
                      help="scrape repeatedly every SECONDS (default: every 120 seconds)")
    p.add_argument("--url", default=DEFAULT_URL, help=f"leaderboard address (default: {DEFAULT_URL})")
    p.add_argument("--season", metavar="N",
                   help="season(s) to scrape, e.g. 4, 3,4 or 1-4 (default: the season in --url). "
                        "Each season is saved as its own snapshot")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--max-pages", type=int, default=1000, help="safety limit on pages per run")
    p.add_argument("--page-delay", type=float, default=0.5, help="seconds between page requests")
    p.add_argument("--browser", action="store_true", default=True,
                   help="use a real browser (the default; this site needs it)")
    p.add_argument("--no-browser", dest="browser", action="store_false",
                   help="use plain HTTP requests instead of a browser")
    p.add_argument("--headed", action="store_true", help="with --browser: show the browser window")
    p.add_argument("--browser-channel", default="auto", choices=["auto", *BrowserPager.CHANNELS],
                   help="with --browser: which browser to use (default: Playwright's Chromium if "
                        "installed, otherwise Microsoft Edge, otherwise Google Chrome)")
    p.add_argument("--next", metavar="LABEL_OR_CSS",
                   help="with --browser: the control that goes to the next page, e.g. --next \">>\" "
                        "(its text) or a CSS selector. Default: detect it automatically")
    p.add_argument("--diagnose", action="store_true",
                   help="save every page (HTML, plus a screenshot with --browser) to data/debug "
                        "and log how each next page was found")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.diagnose) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        if args.once:
            return 0 if run_once(args) else 1
        which = f"season {args.season}" if args.season else args.url
        log.info("scraping %s every %ds (Ctrl+C to stop)", which, args.loop)
        browsers = Browsers(args) if args.browser else None
        while True:
            started = time.monotonic()
            run_once(args, browsers)
            time.sleep(max(0.0, args.loop - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("stopped")
        logging.shutdown()
        os._exit(0)  # don't wait on a browser that was interrupted mid-page


if __name__ == "__main__":
    sys.exit(main())
