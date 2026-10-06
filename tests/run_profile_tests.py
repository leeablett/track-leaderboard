#!/usr/bin/env python3
"""Check the saved browser profile: the site's code is downloaded once and then cached,
every run still starts from page 1, and a profile that's in use doesn't stop the scraper.

Run from the project folder:
    python tests/run_profile_tests.py
"""

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_sites  # noqa: E402
import scraper  # noqa: E402

failures = 0


def check(what: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  ({detail})" if detail else ""))


class Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def run(url: str, profile: Path | None) -> tuple[int, int, float]:
    """One scrape with a newly started browser: (rows, pages, seconds it took)."""
    pager = scraper.BrowserPager(profile=profile)
    try:
        t = time.monotonic()
        entries, pages = scraper.scrape(pager, url, 1000, 0)
        return len(entries), pages, time.monotonic() - t
    finally:
        pager.close()


HOLD_PROFILE = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import scraper
pager = scraper.BrowserPager(profile=Path(sys.argv[2]))
print("ready", pager.profile_dir is not None, flush=True)
time.sleep(120)
"""


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    warnings = Warnings()
    logging.getLogger("scraper").addHandler(warnings)
    logging.getLogger("scraper").setLevel(logging.WARNING)
    server = mock_sites.start()
    base = f"http://127.0.0.1:{server.server_port}"
    profile = Path(tempfile.mkdtemp()) / "browser-profile"

    # --- the site's code is cached in the saved profile
    downloads = mock_sites.FRAMEWORK_DOWNLOADS
    downloads.clear()
    rows1, _, first1 = run(f"{base}/app/leaderboard", profile)
    rows2, _, first2 = run(f"{base}/app/leaderboard", profile)
    check("with a saved profile, the site's code is downloaded only once across browser restarts",
          rows1 == rows2 == 35 and len(downloads) == 1,
          f"downloads {len(downloads)}; scrape {first1:.1f} s then {first2:.1f} s")
    downloads.clear()
    run(f"{base}/app/leaderboard", None)
    run(f"{base}/app/leaderboard", None)
    check("without a profile (--no-profile), it's downloaded every time", len(downloads) == 2,
          f"downloads {len(downloads)}")

    # --- a site that remembers your page must not make a run start part-way through
    # (sanity check first: the test site really does reopen where you left off)
    from playwright.sync_api import sync_playwright
    raw = Path(tempfile.mkdtemp()) / "raw-profile"
    with sync_playwright() as p:
        for _ in range(2):
            ctx = p.chromium.launch_persistent_context(str(raw), headless=True)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto(f"{base}/remember/leaderboard")
            pg.wait_for_selector("table tr td")
            pg.click(".page-selector button >> nth=1")  # ">>"
            pg.wait_for_timeout(500)
            reopened = pg.inner_text(".page-selector >> nth=1")
            ctx.close()
    check("(test site reopens on the page you were on)", reopened == "Page 3", reopened)
    results = [run(f"{base}/remember/leaderboard", profile)[:2] for _ in range(2)]
    check("every new browser still starts from page 1", results == [(35, 7), (35, 7)], str(results))
    pager = scraper.BrowserPager(profile=profile)
    try:
        again = []
        for _ in range(2):
            pager.reset()
            again.append(scraper.scrape(pager, f"{base}/remember/leaderboard", 1000, 0)[1])
    finally:
        pager.close()
    check("every run in the 2-minute loop (same browser) starts from page 1", again == [7, 7], str(again))

    # --- a profile that's in use (another scraper is running): carry on without it
    warnings.messages.clear()
    holder = subprocess.Popen([sys.executable, "-c", HOLD_PROFILE, str(Path(scraper.__file__).parent), str(profile)],
                              stdout=subprocess.PIPE, text=True)
    try:
        ready = holder.stdout.readline().split()
        check("(another program is holding the profile)", ready == ["ready", "True"], " ".join(ready))
        rows, pages, _ = run(f"{base}/blazor/leaderboard", profile)
    finally:
        holder.kill()
        holder.wait()
    check("profile in use: falls back to a fresh browser and still works",
          (rows, pages) == (35, 7) and any("saved browser profile" in m for m in warnings.messages),
          "; ".join(warnings.messages)[:120])

    server.shutdown()
    print("All passed." if not failures else f"{failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
