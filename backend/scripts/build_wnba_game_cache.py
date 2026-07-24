"""One-off WNBA game-history scraper from ESPN (same public API this app's NBA
ingestion uses), for the WNBA baseline-model build (task #40). Feasibility
phase: a cache file + scripts, before any full app/DB integration -- same
pattern as the esports builds.

Iterates dates across each WNBA season (roughly May-Oct) and pulls the
scoreboard; extracts each completed game's teams, scores, date, home/away, and
neutral-site flag. Rest days are computed afterward from the schedule.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402
import httpx  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "wnba_game_cache.json"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
UA = {"User-Agent": "Mozilla/5.0"}
DELAY = 0.25
# WNBA seasons run ~mid-May through mid-Oct (incl. playoffs).
SEASONS = {y: (dt.date(y, 5, 1), dt.date(y, 10, 31)) for y in range(2021, 2027)}


def scrape():
    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    client = httpx.Client(timeout=30.0, headers=UA)
    for season, (start, end) in SEASONS.items():
        day = start
        got = 0
        while day <= end and day <= dt.date.today():
            key_prefix = day.isoformat()
            # skip a date only if we've already cached at least one game for it
            already = any(g["date"] == key_prefix for g in cache.values())
            if not already:
                try:
                    r = client.get(SCOREBOARD, params={"dates": day.strftime("%Y%m%d")})
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
                        "home": home["team"]["abbreviation"], "away": away["team"]["abbreviation"],
                        "home_score": hs, "away_score": as_,
                        "neutral": bool(comp.get("neutralSite")),
                    }
                    got += 1
                time.sleep(DELAY)
            day += dt.timedelta(days=1)
        OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
        print(f"season {season}: cache now {len(cache)} games (+{got} this season)", flush=True)
    print(f"\nDone. {len(cache)} WNBA games -> {OUTPUT_PATH}")


if __name__ == "__main__":
    scrape()
