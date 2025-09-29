# Golf Simulator Availability Scraper (Python + Playwright)

Scrapes available booking slots for **Simulator 1–4** from the given booking page, clicking
**"Show more results"** as needed until slots for **today, tomorrow, and the next day**
are loaded. Exports **one CSV per day** with rows for **all 25 half-hour slots** per simulator
(marking `available` as 1 if shown on the site, 0 otherwise).

> ⚠️ The site only shows *available* slots; booked times are hidden. We therefore build the full
> 25-slot grid per day and mark 1 for times we see, 0 otherwise.

## Output format

One CSV per day in `./output/` named: `availability_YYYY-MM-DD.csv`

Columns:
- `date` (YYYY-MM-DD, Eastern time)
- `time` (HH:MM 24h)
- `simulator` ("Simulator 1".."Simulator 4")
- `available` (0 or 1)

## Config

Set via environment variables or command-line flags (env takes precedence):

- `BASE_URL` (default: `https://clients.uschedule.com/golfspotmiami/booking`)
- `TIMEZONE` (default: `America/New_York`)
- `SLOTS_PER_DAY` (default: `25`)
- `SLOT_MINUTES` (default: `30`)
- `START_TIME`  (default: `09:00`) — local opening time for the slot grid
- `HEADLESS`    (default: `true`)
- `USER_AGENT`  (default: a polite desktop UA)
- `RANDOM_DELAY_MS_MIN` (default: `200`)
- `RANDOM_DELAY_MS_MAX` (default: `600`)

You can also pass these as flags, e.g. `--start-time 08:00 --slots-per-day 25 --slot-minutes 30`.
Flags are merged with env vars; env wins.

> If you **know the exact daily start time**, set `START_TIME` to ensure the 25-slot grid aligns with the venue schedule.

## Install & run

### 1) Python env
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install --with-deps
```

### 2) Run (default config)
```bash
python scraper.py
```

### 3) Run with explicit opening time
```bash
START_TIME=08:00 python scraper.py
```

### 4) Example Railway schedule (optional)

If you do choose to schedule, here’s an example **Railway Cron** (three runs/day).
Create three separate jobs or use three cron entries. Replace the command as needed:

- **10:55 am ET**: `python scraper.py`
- **3:55 pm ET**: `python scraper.py`
- **11:55 pm ET**: `python scraper.py`

Ensure your Railway container’s timezone is set or pass `TZ=America/New_York`.

## How it works (high level)

1. Launches Chromium (Playwright) with a polite user-agent.
2. For each of **Simulator 1..4**:
   - Click the simulator tab/button.
   - Repeatedly click **“Show more results”** until we’ve seen slots through **today+2**.
   - Parse **visible slot times** and their **dates** from the DOM.
3. Build a canonical grid of **25 half-hour slots** starting from `START_TIME` for each day (today..today+2).
4. Mark `available=1` if the time was shown by the site; otherwise `0`.
5. Write **per-day CSVs** stacking all simulators.

## Failing loudly

- If a critical selector (simulator tab, “Show more results”, slot container) isn’t found, the script raises with a clear error message.
- Use `--debug` for verbose logging.

## Customizing selectors

This script tries **text-forward** locators first (e.g., button text like “Simulator 1” and “Show more results”).
If the site’s markup changes, update the selectors in `scraper.py` (search for “SELECTORS” section).

---

**Note:** This is a template intended to be robust out-of-the-box, but please verify selectors against the live DOM and adjust as needed.
