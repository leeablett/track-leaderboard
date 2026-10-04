# Setup guide

This guide takes you from a fresh computer to the scraper running every 2 minutes.
Follow **only the section for your computer**:

- [Windows](#windows)
- [Mac](#mac)
- [Linux / Raspberry Pi](#linux--raspberry-pi)

It takes about 10 minutes. Each step ends with **✅ You should see**. If you don't
see it, stop and check [Problems](#problems) before you carry on.

---

## Windows

### Step 1: Install Python

1. Open <https://www.python.org/downloads/> and click the yellow **Download Python 3.x** button.
2. Open the downloaded file.
3. **Important:** at the bottom of the first screen, tick **Add python.exe to PATH**.
4. Click **Install Now** and wait for it to finish, then click **Close**.

### Step 2: Install Git

1. Open <https://git-scm.com/download/win>. The download starts automatically.
2. Open the downloaded file and click **Next** on every screen (the defaults are fine), then **Install**.

### Step 3: Check both are installed

1. Press the **Windows key**, type `cmd` and press **Enter**. A black Command Prompt window opens.
2. Type each line below and press **Enter** after each:

   ```
   python --version
   git --version
   ```

✅ **You should see** something like `Python 3.12.4` and `git version 2.45.1`.

> If the Command Prompt was already open before you installed, close it and open a new one.

### Step 4: Download the scraper

In the same Command Prompt window, run:

```
cd %USERPROFILE%
git clone https://github.com/leeablett/track-leaderboard
cd track-leaderboard
```

✅ **You should see** `Cloning into 'track-leaderboard'...` and then `done`.
The scraper is now in `C:\Users\<your name>\track-leaderboard`.

### Step 5: Install the scraper's libraries

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

✅ **You should see** `Successfully installed ... requests ... beautifulsoup4 ...` at the end.

### Step 6: Do a test run

```
.venv\Scripts\python scraper.py --once
```

✅ **You should see** a line like:

```
2026-10-04 12:00:00,000 INFO snapshot 1: 250 rows from 5 page(s)
```

Check that the number of pages matches what the website shows. If you see `ERROR`,
go to [Problems](#problems).

### Step 7: Run it every 2 minutes

**Option A: Easy (runs while the window is open)**

```
.venv\Scripts\python scraper.py --loop 120
```

Leave the window open. Closing it or shutting down the PC stops the scraper.

**Option B: Automatic (starts on its own whenever you log in)**

1. Press the **Windows key**, type `Task Scheduler` and open it.
2. On the right, click **Create Basic Task…**
3. **Name:** `Track leaderboard`, then click **Next**.
4. **Trigger:** choose **When I log on**, then click **Next**.
5. **Action:** choose **Start a program**, then click **Next**.
6. Fill in the three boxes. Replace `YOURNAME` with your Windows user name:
   - **Program/script:** `C:\Users\YOURNAME\track-leaderboard\.venv\Scripts\pythonw.exe`
   - **Add arguments:** `scraper.py --loop 120`
   - **Start in:** `C:\Users\YOURNAME\track-leaderboard`
7. Click **Next**, tick **Open the Properties dialog…**, then click **Finish**.
8. In Properties, open the **Settings** tab and **untick** *Stop the task if it runs longer than…*. Click **OK**.
9. To start it now without logging out, right-click the task and choose **Run**.

`pythonw.exe` runs it in the background with no window. To check it's working, see
[Checking it's working](#checking-its-working).

---

## Mac

### Step 1: Install Python and Git

1. Open <https://www.python.org/downloads/macos/> and download the latest **macOS 64-bit universal2 installer**.
2. Open it and click through the installer.
3. Open **Terminal**: press **⌘ + Space**, type `Terminal` and press **Enter**.
4. Type `git --version` and press **Enter**. If a box pops up asking to install
   *command line developer tools*, click **Install** and wait for it to finish.

### Step 2: Check both are installed

```
python3 --version
git --version
```

✅ **You should see** something like `Python 3.12.4` and `git version 2.39.3`.

Then follow the [Mac and Linux steps](#mac-and-linux-steps) below.

---

## Linux / Raspberry Pi

### Step 1: Install Python and Git

Open a terminal and run:

```
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### Step 2: Check both are installed

```
python3 --version
git --version
```

✅ **You should see** something like `Python 3.11.2` and `git version 2.39.2`.

Then follow the [Mac and Linux steps](#mac-and-linux-steps) below.

---

## Mac and Linux steps

### Step 3: Download the scraper

```
cd ~
git clone https://github.com/leeablett/track-leaderboard
cd track-leaderboard
```

✅ **You should see** `Cloning into 'track-leaderboard'...`

### Step 4: Install the scraper's libraries

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p logs
```

✅ **You should see** `Successfully installed ... requests ... beautifulsoup4 ...`

### Step 5: Do a test run

```
.venv/bin/python scraper.py --once
```

✅ **You should see** a line like:

```
2026-10-04 12:00:00,000 INFO snapshot 1: 250 rows from 5 page(s)
```

Check that the number of pages matches what the website shows.

### Step 6: Run it every 2 minutes

**Option A: Easy (runs while the terminal is open)**

```
.venv/bin/python scraper.py --loop 120
```

**Option B: Automatic with cron (Mac or Linux)**

1. Run `crontab -e`. If it asks which editor to use, choose `nano`.
2. Go to the bottom of the file and paste this line, replacing `YOURNAME` with your user name
   (run `whoami` if you're not sure):

   ```
   */2 * * * * cd /home/YOURNAME/track-leaderboard && .venv/bin/python scraper.py --once >> logs/scraper.log 2>&1
   ```

   On a **Mac**, use `/Users/YOURNAME/...` in place of `/home/YOURNAME/...`.
3. Save and exit. In nano, press **Ctrl+O**, **Enter**, then **Ctrl+X**.

✅ **You should see** new lines in `logs/scraper.log` every 2 minutes:

```
tail -f logs/scraper.log
```

(Press **Ctrl+C** to stop watching.)

**Option C: Linux server or Raspberry Pi that should run 24/7.** Use the systemd service
in [`deploy/track-leaderboard.service`](deploy/track-leaderboard.service). The install
steps are at the top of that file.

---

## Checking it's working

The data is saved in `data/leaderboard.db`. To see the latest leaderboard as a
spreadsheet, run this in the `track-leaderboard` folder:

| Windows | Mac / Linux |
|---|---|
| `.venv\Scripts\python export.py latest -o latest.csv` | `.venv/bin/python export.py latest -o latest.csv` |

Then open `latest.csv` in Excel, Numbers or Google Sheets. Run it again a few minutes
later; the `scraped_at` time should have moved on.

---

## Problems

| What you see | What to do |
|---|---|
| `'python' is not recognized` (Windows) | PATH wasn't ticked in Step 1. Run the Python installer again, choose **Modify** → **Next**, tick **Add Python to environment variables**, then **Install**. Open a new Command Prompt. |
| Typing `python` opens the Microsoft Store (Windows) | Use `py` in place of `python` in Steps 3 and 5, e.g. `py -m venv .venv`. |
| `'git' is not recognized` / `command not found: git` | Install Git (Step 2), then open a **new** terminal window. |
| `No module named venv` (Linux) | Run `sudo apt install -y python3-venv`, then repeat the step. |
| `ERROR scrape failed: no <table> found` | The site's layout isn't what the scraper expects. The page was saved to `data/debug/page_1.html`; send that file over so the scraper can be adjusted. |
| `from 1 page(s)` but the site has more pages | The scraper couldn't find the next-page link. Note what the address bar shows when you go to page 2 on the website, and send that over. |
| `ERROR scrape failed: ... ConnectionError` / `Timeout` | Check your internet connection and that the website opens in your browser. Single failures are fine; the next run tries again. |

## Updating the scraper later

In the `track-leaderboard` folder, run:

```
git pull
```
