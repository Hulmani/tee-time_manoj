#!/usr/bin/env python3
import asyncio
import os
import sys
import re
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Set, Tuple

import pandas as pd
import pytz
from dateutil import tz

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ----------------------
# Config & CLI
# ----------------------

@dataclass
class Config:
    base_url: str
    timezone: str
    slots_per_day: int
    slot_minutes: int
    start_time: str  # "HH:MM" in local time
    headless: bool
    user_agent: str
    random_delay_ms_min: int
    random_delay_ms_max: int
    debug: bool

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def get_config(argv: List[str]) -> Config:
    # Defaults
    cfg = {
        "base_url": os.getenv("BASE_URL", "https://clients.uschedule.com/golfspotmiami/booking"),
        "timezone": os.getenv("TIMEZONE", "America/New_York"),
        "slots_per_day": int(os.getenv("SLOTS_PER_DAY", "25")),
        "slot_minutes": int(os.getenv("SLOT_MINUTES", "30")),
        "start_time": os.getenv("START_TIME", "09:00"),
        "headless": env_bool("HEADLESS", True),
        "user_agent": os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "random_delay_ms_min": int(os.getenv("RANDOM_DELAY_MS_MIN", "200")),
        "random_delay_ms_max": int(os.getenv("RANDOM_DELAY_MS_MAX", "600")),
        "debug": "--debug" in argv,
    }

    # CLI overrides (simple; env wins if both present)
    for i, arg in enumerate(argv):
        if arg == "--start-time" and i+1 < len(argv):
            cfg["start_time"] = argv[i+1]
        elif arg == "--slots-per-day" and i+1 < len(argv):
            cfg["slots_per_day"] = int(argv[i+1])
        elif arg == "--slot-minutes" and i+1 < len(argv):
            cfg["slot_minutes"] = int(argv[i+1])
        elif arg == "--timezone" and i+1 < len(argv):
            cfg["timezone"] = argv[i+1]
        elif arg == "--headless" and i+1 < len(argv):
            cfg["headless"] = argv[i+1].lower() in ("1","true","yes","y","on")
        elif arg == "--base-url" and i+1 < len(argv):
            cfg["base_url"] = argv[i+1]

    return Config(**cfg)

# ----------------------
# Utils
# ----------------------

TIME_RE = re.compile(r"\b(1[0-2]|0?[1-9]):([0-5][0-9])\s*(AM|PM)\b")
# Accepts formats like "Monday, September 29, 2025" or "Sep 29, 2025" etc.
DATE_RE = re.compile(
    r"\b(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,\s*)?"
    r"([A-Z][a-z]{2,9})\s+([0-9]{1,2})(?:,\s*([0-9]{4}))?\b"
)

MONTHS = {m: i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], start=1)}
MONTHS.update({m[:3]: i for m,i in MONTHS.items()})

def rand_delay(cfg: Config):
    d = random.randint(cfg.random_delay_ms_min, cfg.random_delay_ms_max) / 1000.0
    return asyncio.sleep(d)

def parse_local_today(cfg: Config) -> datetime:
    tzinfo = pytz.timezone(cfg.timezone)
    return datetime.now(tzinfo).replace(hour=0, minute=0, second=0, microsecond=0)

def hhmm_to_minutes(hhmm: str) -> int:
    hh, mm = map(int, hhmm.split(":"))
    return hh*60 + mm

def build_daily_times(cfg: Config) -> List[str]:
    start_min = hhmm_to_minutes(cfg.start_time)
    times = []
    for i in range(cfg.slots_per_day):
        minutes_total = start_min + i*cfg.slot_minutes
        hh = (minutes_total // 60) % 24
        mm = minutes_total % 60
        times.append(f"{hh:02d}:{mm:02d}")
    return times

def to_24h(hh: int, mm: int, ampm: str) -> Tuple[int,int]:
    if ampm.upper() == "AM":
        if hh == 12: hh = 0
    else:
        if hh != 12: hh += 12
    return hh, mm

def normalize_date_str(match: re.Match, base_year: int) -> str:
    mon_txt = match.group(2)
    day = int(match.group(3))
    year = match.group(4)
    month = MONTHS.get(mon_txt, MONTHS.get(mon_txt[:3]))
    if year is None:
        year = base_year
    else:
        year = int(year)
    return f"{year:04d}-{month:02d}-{day:02d}"

def text_find_dates(text: str, base_year: int) -> Set[str]:
    out = set()
    for m in DATE_RE.finditer(text):
        try:
            d = normalize_date_str(m, base_year)
            out.add(d)
        except Exception:
            continue
    return out

def text_find_times(text: str) -> List[str]:
    out = []
    for m in TIME_RE.finditer(text):
        hh = int(m.group(1)); mm = int(m.group(2)); ampm = m.group(3)
        H,M = to_24h(hh, mm, ampm)
        out.append(f"{H:02d}:{M:02d}")
    return out

# ----------------------
# Scrape logic
# ----------------------

SIMS = [f"Simulator {i}" for i in range(1,5)]

async def click_by_text(page, text: str, exact: bool = True):
    # Try role=button
    loc = page.get_by_role("button", name=text, exact=exact)
    if await loc.count() > 0:
        await loc.first.click()
        return True
    # Try text locator
    loc = page.get_by_text(text, exact=exact)
    if await loc.count() > 0:
        await loc.first.click()
        return True
    return False

async def element_exists(page, text: str) -> bool:
    loc = page.get_by_text(text, exact=False)
    return (await loc.count()) > 0

async def collect_available_slots_for_sim(page, cfg: Config, sim_name: str, until_date_inclusive: str) -> Dict[str, Set[str]]:
    """
    Returns: { 'YYYY-MM-DD': set(['HH:MM', ...]), ... } of available slots by date for this simulator.
    """
    tzinfo = pytz.timezone(cfg.timezone)
    today_local = parse_local_today(cfg)
    base_year = today_local.year

    # Click the simulator tab/button
    if cfg.debug: print(f"[DEBUG] Selecting {sim_name}")
    clicked = await click_by_text(page, sim_name, exact=True)
    if not clicked:
        raise RuntimeError(f"Could not find simulator tab/button: '{sim_name}'")

    await rand_delay(cfg)

    # We will loop clicking "Show more results" until we have seen dates through 'until_date_inclusive'
    seen_by_date: Dict[str, Set[str]] = {}

    def max_seen_date() -> str:
        if not seen_by_date: return ""
        return max(seen_by_date.keys())

    # inner helper to parse current viewport text for dates/times
    async def parse_view():
        text = await page.evaluate("document.body.innerText")
        # Find candidate dates in the viewport text
        dates = sorted(text_find_dates(text, base_year))
        times = text_find_times(text)
        # Heuristic: slots usually appear near a date group. We won't try to bind times to specific dates by position;
        # instead we scan for explicit date labels on the page as we scroll forward and harvest times we can see right now
        # and attribute them to the *latest* date label present in the viewport. This is conservative and may under-assign,
        # but works when the page groups slots under a visible date header.
        latest_date = dates[-1] if dates else None
        if latest_date:
            if latest_date not in seen_by_date:
                seen_by_date[latest_date] = set()
            for t in times:
                seen_by_date[latest_date].add(t)

    # Parse initial view
    await parse_view()

    target_reached = lambda: max_seen_date() >= until_date_inclusive if max_seen_date() else False

    # Repeatedly click "Show more results"
    more_labels = ["Show more results", "Show More Results", "Show more", "Show More"]
    attempt = 0
    while not target_reached():
        attempt += 1
        clicked_more = False
        for lbl in more_labels:
            loc = page.get_by_text(lbl, exact=False)
            if await loc.count() > 0:
                try:
                    await loc.first.click()
                    clicked_more = True
                    break
                except PlaywrightTimeout:
                    continue
        if not clicked_more:
            # If we can't find the button, fail loudly as requested
            raise RuntimeError(f"'{sim_name}': Could not find 'Show more results' after {attempt} attempts. Latest date seen: {max_seen_date() or 'N/A'}")

        await page.wait_for_timeout(250)  # small wait for content to load
        await rand_delay(cfg)
        await parse_view()

        if attempt > 200:
            raise RuntimeError(f"'{sim_name}': Safety stop — too many 'Show more results' clicks. Latest date seen: {max_seen_date() or 'N/A'}")

    return seen_by_date

async def run(cfg: Config):
    tzinfo = pytz.timezone(cfg.timezone)
    today = parse_local_today(cfg)
    dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    until_date_inclusive = dates[-1]

    daily_grid_times = build_daily_times(cfg)

    # Playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=cfg.headless)
        context = await browser.new_context(user_agent=cfg.user_agent, viewport={"width": 1400, "height": 900})
        page = await context.new_page()
        await page.goto(cfg.base_url, wait_until="domcontentloaded", timeout=60000)
        await rand_delay(cfg)

        all_rows: List[Dict] = []

        for sim in SIMS:
            sim_avail_by_date = await collect_available_slots_for_sim(page, cfg, sim, until_date_inclusive)

            # Build rows for each of the 3 dates
            for d in dates:
                avail_times = sim_avail_by_date.get(d, set())
                for t in daily_grid_times:
                    all_rows.append({
                        "date": d,
                        "time": t,
                        "simulator": sim,
                        "available": 1 if t in avail_times else 0
                    })

        await browser.close()

    # Write one CSV per day, stacking all simulators
    os.makedirs("output", exist_ok=True)
    df = pd.DataFrame(all_rows, columns=["date","time","simulator","available"])
    for d in dates:
        out = df[df["date"] == d].sort_values(["simulator","time"])
        out_path = os.path.join("output", f"availability_{d}.csv")
        out.to_csv(out_path, index=False)
        print(f"Wrote {out_path} ({len(out)} rows)")

def main():
    cfg = get_config(sys.argv[1:])
    # Sanity
    if cfg.slots_per_day <= 0 or cfg.slot_minutes <= 0:
        raise SystemExit("SLOTS_PER_DAY and SLOT_MINUTES must be positive")
    if not re.match(r"^[0-2]\d:[0-5]\d$", cfg.start_time):
        raise SystemExit("START_TIME must be HH:MM (24h), e.g., 08:00")
    asyncio.run(run(cfg))

if __name__ == "__main__":
    main()
