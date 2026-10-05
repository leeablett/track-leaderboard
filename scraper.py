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

DEFAULT_URL = "https://vfm.bdynamicsstudio.com/leaderboard"
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
  const PAGER = /pagina|pager|page-nav|pages\b|paging/i;
  const cls = e => [typeof e.className === 'string' ? e.className : '', e.id || '',
                    e.parentElement && typeof e.parentElement.className === 'string' ? e.parentElement.className : ''].join(' ');
  const label = e => ((e.tagName === 'INPUT' ? (e.value || '') : (e.innerText || '').trim()) ||
                      e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('alt') || '').replace(/\s+/g, ' ');
  const norm = t => t.replace(/»/g, '>>').replace(/«/g, '<<').replace(/›/g, '>').replace(/‹/g, '<').replace(/\s+/g, '').toLowerCase();
  const visible = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
                         return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const disabled = e => e.disabled || e.getAttribute('aria-disabled') === 'true' ||
                        /\bdisabled\b/i.test(cls(e)) || !!e.closest('[disabled], [aria-disabled="true"]');
  const els = [...document.querySelectorAll('a, button, input[type=submit], input[type=button], input[type=image], [role=button], [role=link], [onclick]')]
                .filter(e => !e.closest('table') && visible(e));
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
      ['page number ' + want, e => label(e) === String(want), false],
      ['Load more button', e => MORE.test(label(e)), true],
    ];
  }
  const candidates = els.filter(e => inPager(e) || LABEL.test(label(e)) || CLASS.test(cls(e))).slice(0, 25).map(describe);
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


class BrowserPager:
    """Drives a real Chromium browser, so JavaScript-rendered pages and
    click-to-paginate leaderboards work."""

    # Tried in order when no browser is named: Playwright's own Chromium, then the
    # browsers already on the PC (Edge is on every Windows 10/11 machine).
    CHANNELS = ("chromium", "msedge", "chrome")

    def __init__(self, headed: bool = False, channel: str = "auto", next_control: str | None = None):
        from playwright.sync_api import sync_playwright

        self.next_control = next_control  # --next: label or CSS selector of the next-page control

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
        self.status = None
        self.visited: set[str] = set()
        self.clicked = False  # paging by clicking; no control found afterwards means the end
        self.stop_reason = ""

    def _settle(self):
        from playwright.sync_api import Error as PWError

        try:
            self.page.wait_for_selector("table tr td", timeout=20_000)
        except PWError:
            return  # no table (yet); the caller reports it
        try:
            self.page.wait_for_load_state("networkidle", timeout=3_000)
        except PWError:
            pass  # sites that keep a connection open never go idle; the table is already there

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
        self._click_next()
        # Wait until the table changes (new rows, more rows, or replaced while loading).
        # If it doesn't, the repeated-rows check in scrape() asks for a retry.
        self._wait_for_change(before, 20_000)
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
        self.page.evaluate("document.documentElement.dataset.lbOld = '1'")
        self.page.locator("[data-lb-next]").first.click()

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
        if self._wait_for_change(before, 10_000, reloaded=False):
            log.info("page %d was slow to load; read it again", page_no)
            self._settle()
            return self.page.content()
        found = self._find_next(page_no)
        if found["action"] != "click":
            return None
        log.info("page %d didn't change after clicking; clicking %s again", page_no, found["how"])
        self._click_next()
        if not self._wait_for_change(before, 10_000):
            return None
        self._settle()
        return self.page.content()

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


def scrape(pager, url: str, max_pages: int, delay: float, diagnose: bool = False) -> tuple[list[dict], int]:
    """Return (entries, pages_scraped). Each entry has page/position/rank/name/score/data."""
    entries: list[dict] = []
    seen_rows: dict[str, int] = {}  # row -> page it was first read on
    retried = False
    headers: list[str] | None = None
    exp_pages = exp_rows = None
    pages = 0
    reason = ""
    html = pager.open(url)

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
            reason = (f"page {page_no} only repeated rows already read "
                      f"(the same rows as page {', '.join(map(str, same_as))})")
            break
        retried = False
        for r in new:
            seen_rows[row_key(r)] = page_no
        pages = page_no

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
        html = pager.next(soup, page_no)
        if html is None:
            reason = pager.stop_reason
            break

    log.info("read %d page(s), %d rows; stopped because %s", pages, len(entries), reason or "done")
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
        pager = (BrowserPager(headed=args.headed, channel=args.browser_channel, next_control=args.next)
                 if args.browser else HttpPager())
        entries, pages = scrape(pager, args.url, args.max_pages, args.page_delay, args.diagnose)
        if not entries:
            raise RuntimeError("scrape returned no rows")
        sid = store(conn, scraped_at, entries, pages)
        log.info("snapshot %d: %d rows from %d page(s)", sid, len(entries), pages)
        return True
    except KeyboardInterrupt:
        # Ctrl+C mid-scrape: the browser connection is already cut, and closing it politely
        # would hang. Leave it; the browser exits with this program.
        pager = None
        raise
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
    mode.add_argument("--once", action="store_true", help="scrape once and exit")
    mode.add_argument("--loop", type=int, metavar="SECONDS", default=120,
                      help="scrape repeatedly every SECONDS (default: every 120 seconds)")
    p.add_argument("--url", default=DEFAULT_URL)
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
        log.info("scraping %s every %ds (Ctrl+C to stop)", args.url, args.loop)
        while True:
            started = time.monotonic()
            run_once(args)
            time.sleep(max(0.0, args.loop - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("stopped")
        logging.shutdown()
        os._exit(0)  # don't wait on a browser that was interrupted mid-page


if __name__ == "__main__":
    sys.exit(main())
