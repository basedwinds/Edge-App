"""One-off College Football (FBS) game-history scraper from ESPN (free public
API) for the CFB baseline model (core-5 expansion). Feasibility phase: cache
file + scripts. Iterates each season's weeks via the scoreboard (groups=80 =
all FBS); stores completed games by ESPN team id, scores, date, home/away, and
neutral-site flag (bowl games / neutral kickoffs).
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402
import httpx  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "cfb_game_cache.json"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
UA = {"User-Agent": "Mozilla/5.0"}
DELAY = 0.2
# CFB season: late Aug through mid-Jan (bowls/playoff). Label season by fall year.
SEASONS = {y: (dt.date(y, 8, 20), dt.date(y + 1, 1, 25)) for y in range(2021, 2026)}


def scrape():
    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    client = httpx.Client(timeout=30.0, headers=UA)
    for season, (start, end) in SEASONS.items():
        day = start
        got = 0
        while day <= end and day <= dt.date.today():
            key_prefix = day.isoformat()
            already = any(g["date"] == key_prefix for g in cache.values())
            # CFB games cluster on Thu-Sat; still sweep every day for midweek/bowls
            if not already:
                try:
                    r = client.get(SCOREBOARD, params={"dates": day.strftime("%Y%m%d"), "groups": 80, "limit": 200})
                    events = r.json().get("events", []) if r.status_code == 200 else []
                except httpx.HTTPError:
                    events = []
                    time.sleep(DELAY)
                for e in events:
                    comp = e.get("competitions", [{}])[0]
                    status = comp.get("status", {}).get("type", {})
                    if not status.get("completed"):
                        continue
                    cs = comp.get("competitors", [])
                    if len(cs) != 2:
                        continue
                    home = next((c for c in cs if c.get("homeAway") == "home"), None)
                    away = next((c for c in cs if c.get("homeAway") == "away"), None)
                    if not home or not away:
                        continue
                    try:
                        hs, as_ = int(home["score"]), int(away["score"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    cache[e["id"]] = {
                        "id": e["id"], "season": season, "date": key_prefix,
                        "home": home["team"]["id"], "away": away["team"]["id"],
                        "home_abbr": home["team"].get("abbreviation", ""),
                        "away_abbr": away["team"].get("abbreviation", ""),
                        "home_score": hs, "away_score": as_,
                        "neutral": bool(comp.get("neutralSite")),
                    }
                    got += 1
                time.sleep(DELAY)
            day += dt.timedelta(days=1)
        OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
        print(f"season {season}: cache now {len(cache)} games (+{got} this season)", flush=True)
    print(f"\nDone. {len(cache)} CFB games -> {OUTPUT_PATH}")


if __name__ == "__main__":
    scrape()
