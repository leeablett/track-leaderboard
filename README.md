# track-leaderboard

Scrape and track the leaderboard at <https://vfm.bdynamicsstudio.com/leaderboard> on **Windows**.

Every run reads **all pages** of the leaderboard and saves them as one timestamped snapshot in
`data\leaderboard.db`, so you build up a full history of ranks and scores.

Run every command below in **PowerShell** or **Command Prompt**, one line at a time.

## 1. Install Python and Git (once)

1. Install Python from <https://www.python.org/downloads/>. On the first screen, tick **Add python.exe to PATH**.
2. Install Git from <https://git-scm.com/download/win>. The default options are fine.
3. Open a **new** PowerShell window and check both work:

```
python --version
```

```
git --version
```

## 2. Download the scraper (once)

Go to the folder you want it in, for example `D:\github`:

```
cd D:\github
```

```
git clone https://github.com/leeablett/track-leaderboard
```

```
cd track-leaderboard
```

## 3. Install the libraries (once)

```
python -m pip install -r requirements.txt
```

```
python -m playwright install chromium
```

The second command downloads the browser the scraper uses (about 150 MB).

## 4. Test run

```
python scraper.py --once --browser -v
```

✅ You should see a line like `INFO snapshot 1: 250 rows from 5 page(s)`. Check the page
count matches the website.

To watch the browser while it works, add `--headed`:

```
python scraper.py --once --browser --headed
```

## 5. Run it every 2 minutes

**Option A: in a window.** Runs until you close the window.

```
python scraper.py --loop 120 --browser
```

**Option B: automatically in the background.** Starts whenever you log in.

1. Find where Python is installed:

   ```
   where.exe python
   ```

   It prints something like `C:\Users\YOURNAME\AppData\Local\Programs\Python\Python312\python.exe`.
2. Open **Task Scheduler** and click **Create Basic Task…**
3. **Name:** `Track leaderboard`. **Trigger:** **When I log on**. **Action:** **Start a program**.
4. Fill in:
   - **Program/script:** the path from step 1 with `pythonw.exe` at the end, e.g. `C:\Users\YOURNAME\AppData\Local\Programs\Python\Python312\pythonw.exe`
   - **Add arguments:** `scraper.py --loop 120 --browser`
   - **Start in:** your scraper folder, e.g. `D:\github\track-leaderboard`
5. Tick **Open the Properties dialog…** and click **Finish**. On the **Settings** tab, **untick** *Stop the task if it runs longer than…* and click **OK**.
6. To start it now, right-click the task and choose **Run**.

## 6. Get the data out

Current leaderboard as a spreadsheet (open `latest.csv` in Excel):

```
python export.py latest -o latest.csv
```

Every snapshot ever taken:

```
python export.py history -o history.csv
```

One player's rank and score over time:

```
python export.py player "Some Name" -o player.csv
```

## 7. Look inside the database

All the data is in one file: `data\leaderboard.db`. There are two ways to look at it.
Both are safe to use while the scraper is running.

### Option A: DB Browser for SQLite (point and click)

1. Download and install **DB Browser for SQLite** from <https://sqlitebrowser.org/dl/>. The standard Windows installer is fine.
2. Open it, click **Open Database Read Only…** (in the drop-down next to **Open Database**), and choose `D:\github\track-leaderboard\data\leaderboard.db`.
3. Click the **Browse Data** tab and pick a table or view from the **Table** drop-down:
   - `latest`: the current leaderboard
   - `history`: every entry from every run
   - `snapshots`: one row per run, including failed ones
4. To run your own questions, use the **Execute SQL** tab: paste any query from Option B (without the `python query.py` part and the outer double quotes) and press **F5**.
5. To save what you're looking at, use **File → Export → Table(s) as CSV file…**

Opening it **read-only** means you can't change or lock the data by accident while the scraper is writing to it.

### Option B: one-line queries from PowerShell

`query.py` runs a question against the database and prints the answer as a table. Put the
question in double quotes, and any text inside it in single quotes.

List what's in the database:

```
python query.py --tables
```

Top 10 right now:

```
python query.py "SELECT rank, name, score FROM latest LIMIT 10"
```

Find a player by part of their name:

```
python query.py "SELECT rank, name, score FROM latest WHERE name LIKE '%smith%'"
```

One player's rank and score over time:

```
python query.py "SELECT scraped_at, rank, score FROM history WHERE name = 'Some Name' ORDER BY scraped_at"
```

How many runs have been saved, and when the first and last were:

```
python query.py "SELECT COUNT(*) AS runs, MIN(scraped_at) AS first, MAX(scraped_at) AS last FROM snapshots WHERE status = 'ok'"
```

Recent failed runs and why:

```
python query.py "SELECT scraped_at, error FROM snapshots WHERE status = 'error' ORDER BY id DESC LIMIT 10"
```

Biggest climbers over the last 24 hours:

```
python query.py "SELECT l.name, f.rank AS was, l.rank AS now, f.rank - l.rank AS climbed FROM latest l JOIN history f ON f.name = l.name AND f.snapshot_id = (SELECT MIN(id) FROM snapshots WHERE status = 'ok' AND scraped_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')) ORDER BY climbed DESC LIMIT 10"
```

See every column the site shows (the `rank`, `name` and `score` columns are picked out; the rest are kept in `data`):

```
python query.py "SELECT j.key AS column_name, j.value AS example FROM latest, json_each(latest.data) AS j WHERE latest.position = 1"
```

Use one of those extra columns, for example `country`. Swap in a name from the list above:

```
python query.py "SELECT rank, name, json_extract(data, '$.country') AS country FROM latest"
```

Save any result to a CSV file for Excel by adding `-o` and a file name:

```
python query.py "SELECT * FROM history WHERE name = 'Some Name'" -o some-name.csv
```

Times in `scraped_at` are UTC (UK winter time; one hour behind UK summer time).

## Updating the scraper

```
git pull
```

```
python -m pip install -r requirements.txt
```

## Options

| Option | What it does |
|---|---|
| `--once` | Scrape once and stop |
| `--loop 120` | Scrape every 120 seconds until stopped |
| `--browser` | Use a real browser (needed for this site) |
| `--headed` | Show the browser window (with `--browser`) |
| `--page-delay 0.5` | Seconds to wait between pages |
| `--max-pages 200` | Safety limit on pages per run |
| `--db PATH` | Use a different database file |
| `-v` | Show detailed logs |

## Problems

| What you see | What to do |
|---|---|
| `'python' is not recognized` | Python isn't on PATH. Re-run the Python installer, choose **Modify**, tick **Add Python to environment variables**, then open a new window. |
| Typing `python` opens the Microsoft Store | Use `py` in place of `python`, e.g. `py scraper.py --once --browser`. |
| `No module named 'bs4'` or `'playwright'` | Run `python -m pip install -r requirements.txt` again. |
| `Executable doesn't exist ... chromium` | Run `python -m playwright install chromium`. |
| `HTTP 406: the site's firewall blocked the request` | Add `--browser` to the command. |
| `the site's firewall blocked the browser too` | Try adding `--headed`. If it's still blocked, the site doesn't allow automated access; contact the site owner. |
| `no leaderboard <table> found` | The page was saved to `data\debug\page_1.html`. Send that file over so the scraper can be adjusted. |
| `from 1 page(s)` but the site has more | Note what the address bar shows on page 2 of the website and send it over. |

## How the data is stored

One SQLite file, `data\leaderboard.db`. See [Look inside the database](#7-look-inside-the-database) for how to open it.

Two handy **views** are built on top of the tables: `latest` (the most recent successful run) and `history` (every successful run). `query.py` creates them the first time you run it.

**`snapshots`**: one row per scrape run

| column | meaning |
|---|---|
| `id` | snapshot id |
| `scraped_at` | time of the run (UTC) |
| `pages`, `row_count` | how much was scraped |
| `status`, `error` | `ok` or `error` (failed runs are recorded too) |
| `content_hash` | the same as the previous snapshot means nothing changed |

**`entries`**: one row per leaderboard line per snapshot

| column | meaning |
|---|---|
| `snapshot_id` | which run it came from |
| `page`, `position` | page number and overall order on the site |
| `rank`, `name`, `score` | the main fields |
| `data` | every column exactly as shown on the site |

At one run every 2 minutes, the database grows by about 720 snapshots a day (tens of MB).

Using Mac or Linux? See [SETUP.md](SETUP.md).
