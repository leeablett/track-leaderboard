# track-leaderboard

Scrape and track the leaderboard at <https://vfm.bdynamicsstudio.com/leaderboard>.

Every run fetches **all pages** of the leaderboard and stores them as one timestamped
snapshot in a local SQLite database (`data/leaderboard.db`), so you have the full history
of ranks and scores to process later.

## Setup

```bash
git clone https://github.com/leeablett/track-leaderboard
cd track-leaderboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt
mkdir -p logs
```

Do a test run first:

```bash
.venv/bin/python scraper.py --once -v
```

If it prints `no <table> found`, the page HTML is saved to `data/debug/page_1.html`.
Send that file over and the parser can be adjusted.

## Running every 2 minutes

Choose one option:

| Option | Command / file | Best for |
|---|---|---|
| Built-in loop | `.venv/bin/python scraper.py --loop 120` | Quick start, any OS (keep the terminal open) |
| cron | [`deploy/crontab.example`](deploy/crontab.example) | Linux / macOS / Raspberry Pi |
| systemd | [`deploy/track-leaderboard.service`](deploy/track-leaderboard.service) | Linux servers: starts on boot, restarts on failure |
| Task Scheduler | Run `python scraper.py --once`, trigger repeating every 2 minutes | Windows |

Options: `--url`, `--db PATH`, `--max-pages` (safety cap, default 200),
`--page-delay` (seconds between page requests, default 0.5), `-v` for debug logs.

## Storage

SQLite: one file, no server, readable from Python, pandas, Excel (via export), or
[DB Browser for SQLite](https://sqlitebrowser.org/).

**`snapshots`**: one row per scrape run

| column | meaning |
|---|---|
| `id` | snapshot id |
| `scraped_at` | UTC ISO-8601 timestamp |
| `pages`, `row_count` | how much was scraped |
| `status`, `error` | `ok` or `error` (failed runs are recorded so gaps are explainable) |
| `content_hash` | hash of all rows; equal to the previous snapshot means nothing changed |

**`entries`**: one row per leaderboard line per snapshot

| column | meaning |
|---|---|
| `snapshot_id` | links to `snapshots.id` |
| `page`, `position` | page number and overall order on the site |
| `rank`, `name`, `score` | common fields, picked out by column name |
| `data` | JSON with **every** column exactly as scraped |

At every-2-minutes the database grows by ~720 snapshots/day; with a few hundred rows per
snapshot that is tens of MB per day. Use `content_hash` to skip unchanged snapshots during
analysis, or prune old ones if needed.

## Getting the data out

```bash
python export.py latest  -o latest.csv          # current leaderboard
python export.py history -o history.csv         # everything
python export.py player "Some Name"             # one player's rank/score over time
```

Or query directly:

```python
import sqlite3, pandas as pd
con = sqlite3.connect("data/leaderboard.db")
df = pd.read_sql("""
    SELECT s.scraped_at, e.rank, e.name, e.score
    FROM entries e JOIN snapshots s ON s.id = e.snapshot_id
    WHERE s.status = 'ok'
""", con, parse_dates=["scraped_at"])
```
