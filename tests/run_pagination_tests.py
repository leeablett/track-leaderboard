#!/usr/bin/env python3
"""Check the scraper reads every page of every common pagination style.

Run from the project folder:
    python tests/run_pagination_tests.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_sites  # noqa: E402
import scraper  # noqa: E402

CASES = [  # (style, use browser, expected rows)
    ("next_text", True, 35),       # "Next »" button
    ("numbers", True, 35),         # numbered page buttons + icon-only arrow
    ("mui", True, 35),             # icon button labelled "Go to next page"
    ("slow", True, 35),            # table replaced by a spinner for 3 s per page
    ("slow_keep", True, 35),       # old table stays visible for 3 s per page
    ("loadmore", True, 35),        # "Load more" appends rows
    ("bootstrap", True, 35),       # "Next ›" link with href="#"
    ("ssr_next_text", True, 35),   # server-rendered "Next ›" links
    ("ssr_next_text", False, 35),  # same, without a browser
    ("many", True, 1250),          # 250 pages
]


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    server = mock_sites.start()
    base = f"http://127.0.0.1:{server.server_port}"
    failures = 0
    for style, browser, want in CASES:
        pager = scraper.BrowserPager() if browser else scraper.HttpPager()
        try:
            entries, pages = scraper.scrape(pager, f"{base}/{style}/leaderboard", 1000, 0)
            names = {e["name"] for e in entries}
            ok = len(entries) == want and len(names) == want
            detail = f"{len(entries)} rows from {pages} pages"
        except Exception as exc:
            ok, detail = False, f"error: {exc}"
        finally:
            pager.close()
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {style:<14} {'browser' if browser else 'plain':<8} want {want:<5} {detail}")
    server.shutdown()
    print("All passed." if not failures else f"{failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
