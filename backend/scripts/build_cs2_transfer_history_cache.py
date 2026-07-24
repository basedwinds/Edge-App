"""One-off cache builder for CS2's real full historical transfer log, for
the roster-tenure investigation (2026-07-20, user-requested model-quality
pass). roster_changes_cs2.py's own Portal:Transfers scrape only ever exposes
a rolling ~2-week window (confirmed live: 200 divRow entries spanning
2026-07-06 to 2026-07-20, no pagination) -- not deep enough to compute a
real "days since this team's last roster change" feature back to 2023-06-01
(this app's own CS2 match cache's start date).

REAL FIND (2026-07-20): Liquipedia's Transfers/{year} pages transclude a
SEPARATE real subpage per month -- confirmed live via action=raw
(`{{:Player Transfers/2023/December}}`) -- so the FULL historical log is
reachable via `Player_Transfers/{year}/{month}` fetches, one per real
calendar month in this app's own match-cache date range (~38 fetches for
2023-06 through 2026-07), not one fetch per TEAM (1,453 unique teams in the
match cache -- confirmed live, prohibitively expensive by comparison).

Same real divRow parsing as roster_changes_cs2.py (date, player(s), old
team, new team) -- reused directly, not reimplemented.

Run: backend/.venv/Scripts/python.exe scripts/build_cs2_transfer_history_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from app.ingestion.roster_changes_cs2 import _team_name_from_cell  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "cs2_transfer_history_cache.json"

BASE_URL = "https://liquipedia.net/counterstrike"
REQUEST_DELAY_SECONDS = 2.0  # same politeness delay as build_cs2_match_cache.py/build_cs2_map_pool_cache.py

# Covers this app's own real CS2 match-cache date range (2023-06-01 through
# 2026-07-18, see elo_cs2.py's own docstring) with one real calendar month
# of margin on each side.
MONTHS = [
    (year, month)
    for year in (2023, 2024, 2025, 2026)
    for month in range(1, 13)
    if (year, month) >= (2023, 5) and (year, month) <= (2026, 7)
]
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

_client = httpx.Client(
    timeout=30.0,
    headers={"User-Agent": "nfl-edge-app/0.1 (personal research project; contact via GitHub)"},
)


def fetch_month(year: int, month: int) -> list[dict]:
    """Returns real transfer events for this one real calendar month --
    each event is {date, player, team, direction} where direction is "in"
    (joined this team) or "out" (left this team). Same OldTeam/NewTeam divRow
    shape as roster_changes_cs2.py's own Portal:Transfers parse."""
    url = f"{BASE_URL}/Player_Transfers/{year}/{MONTH_NAMES[month]}"
    resp = _client.get(url)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    events = []
    for row in soup.find_all("div", class_=lambda c: c and "divRow" in c):
        date_cell = row.find("div", class_=lambda c: c and "Date" in c)
        if date_cell is None:
            continue
        date_text = date_cell.get_text(strip=True)
        if not date_text:
            continue
        # Player name(s) -- added 2026-07-21 for the roster-reconstruction
        # build (task #25). The original pass stored only team+date+direction
        # because the roster-tenure K-boost just needed "did ANYTHING change";
        # reconstructing an actual LINEUP needs player identity. Same Name-cell
        # parse roster_changes_cs2.py already uses. One divRow can legitimately
        # carry several players (a real group/lineup transfer), so this is a
        # list, not a scalar.
        name_cell = row.find("div", class_=lambda c: c and "Name" in c)
        players = [a.get_text(strip=True) for a in name_cell.find_all("a")] if name_cell else []
        players = [p for p in players if p]
        for cls, direction in (("OldTeam", "out"), ("NewTeam", "in")):
            cell = row.find("div", class_=lambda c: c and "Team" in c and cls in c)
            if cell is None:
                continue
            team = _team_name_from_cell(cell)
            if not team:
                continue
            events.append({"date": date_text, "team": team, "direction": direction, "players": players})
    return events


def main():
    all_events: list[dict] = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else []
    done_months = {(e["_year"], e["_month"]) for e in all_events if "_year" in e}
    remaining = [(y, m) for (y, m) in MONTHS if (y, m) not in done_months]
    print(f"{len(MONTHS)} total months, {len(done_months)} already done, {len(remaining)} remaining")

    for i, (year, month) in enumerate(remaining):
        print(f"[{i + 1}/{len(remaining)}] {MONTH_NAMES[month]} {year}...", end=" ", flush=True)
        try:
            events = fetch_month(year, month)
        except httpx.HTTPError as e:
            print(f"FAILED ({e}), will retry on next run")
            continue
        for e in events:
            e["_year"], e["_month"] = year, month
        all_events.extend(events)
        print(f"{len(events)} real transfer events")
        OUTPUT_PATH.write_text(json.dumps(all_events, indent=None), encoding="utf-8")
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nDone. {len(all_events)} total real transfer events cached at {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
