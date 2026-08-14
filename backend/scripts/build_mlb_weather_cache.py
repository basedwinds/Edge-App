"""One-off local cache builder for real HISTORICAL weather at game time --
checks whether temperature/wind carry a real signal for MLB totals, a gap
this app has had since the NFL weather module was built (weather_rules.py's
own docstring: "no free historical-weather dataset on hand, only Open-Meteo's
forward-looking forecast" -- its TOTAL_WEATHER_MAX_SUPPRESSION_PTS constant
is hand-picked, never validated). Open-Meteo's ARCHIVE API (confirmed live,
free, no key: https://archive-api.open-meteo.com/v1/archive) covers
historical hourly weather by coordinate back decades -- this closes that gap
for MLB, and the same source could later validate NFL's own constant too
(not attempted here, out of this round's scope).

ONE call per team covers the ENTIRE 2021-2025 date range in one shot
(confirmed live: ~1.4MB, ~7s for 5 years of hourly data at one location) --
far cheaper than a per-game or per-season call pattern. Only the 21 OUTDOOR
teams in mlb_ballparks.py are fetched (retractable/dome teams deliberately
excluded, see that module's docstring).

For each home game, picks the hourly weather reading closest to the game's
own local start time (gametime is stored UTC -- see mlb_data.py -- converted
to the ballpark's own timezone via zoneinfo).

Run: backend/.venv/Scripts/python.exe scripts/build_mlb_weather_cache.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from app.clients.base import get_json  # noqa: E402
from app.data.mlb_ballparks import BALLPARKS  # noqa: E402

# 2026-08-14: extended to carry HUMIDITY and SURFACE PRESSURE alongside
# temperature and wind, so air density can be tested as a totals signal. Ball
# carry depends on the density of the air it moves through, and density is a
# function of all three -- temperature alone captures only part of it, and
# humid air is LESS dense than dry air (water vapour is lighter than the
# nitrogen/oxygen it displaces), which is the opposite of most people's
# intuition and is exactly why it deserves a measurement rather than a guess.
#
# Both fields come back from the SAME free Open-Meteo archive call already
# being made -- verified live before changing anything -- so this costs no new
# data source and no extra requests beyond the 30 per-park fetches.
#
# WRITES TO A NEW PATH ON PURPOSE. mlb_weather_cache.json is what TEMP_SLOPE
# and OUT_WIND_SLOPE were fitted against; regenerating it in place would rebase
# the evidence for two live constants. It also could not work even if it were
# safe: the resume check below skips any team whose games are all cached, so an
# in-place run would skip all 30 and never add the new fields.
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_weather_density_cache.json"
LEGACY_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_weather_cache.json"
SCHEDULE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
START_DATE = "2021-01-01"
END_DATE = "2025-12-31"


def _fetch_team_hourly(team: str) -> dict:
    bp = BALLPARKS[team]
    url = (
        f"{ARCHIVE_URL}?latitude={bp['lat']}&longitude={bp['lon']}"
        f"&start_date={START_DATE}&end_date={END_DATE}"
        "&hourly=temperature_2m,wind_speed_10m,wind_direction_10m"
        ",relative_humidity_2m,surface_pressure"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone={bp['tz'].replace('/', '%2F')}"
    )
    return get_json(url)


def main():
    games = json.loads(SCHEDULE_CACHE_PATH.read_text())
    games = [
        g for g in games
        if g["game_type"] == "R" and g["home_team"] in BALLPARKS
        and g.get("home_score") is not None and g.get("gametime")
        and START_DATE <= g["gameday"] <= END_DATE
    ]
    print(f"{len(games)} completed REG home games at the 21 outdoor ballparks, {START_DATE}..{END_DATE}")

    games_by_team: dict[str, list[dict]] = {}
    for g in games:
        games_by_team.setdefault(g["home_team"], []).append(g)

    cache: dict[str, dict] = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
        print(f"Resuming: {len(cache)} games already cached")

    t0 = time.monotonic()
    for team, team_games in sorted(games_by_team.items()):
        if all(g["id"] in cache for g in team_games):
            continue
        print(f"  fetching {team} ({len(team_games)} home games)...")
        hourly = _fetch_team_hourly(team)["hourly"]
        times = hourly["time"]  # ISO local strings, e.g. "2024-07-15T19:00"
        tz = ZoneInfo(BALLPARKS[team]["tz"])
        time_index = {t: i for i, t in enumerate(times)}

        for g in team_games:
            if g["id"] in cache:
                continue
            # gametime is a raw UTC clock reading with no date attached --
            # naively pairing it with gameday (the LOCAL date) assumes the
            # UTC calendar day equals the local one, which is FALSE for
            # evening games at negative UTC offsets (real instant is on
            # gameday+1). REAL BUG caught live while wiring up the serving
            # side of this same signal (mlb_markets.py::_game_kickoff_local):
            # the naive version was returning "no forecast" for a same-day
            # Coors Field game because the miscalculated instant landed in
            # the past. Fixed here too (not just at serving time) by trying
            # both candidate UTC days and keeping whichever one's local
            # conversion round-trips back to `gameday`.
            game_local = None
            for day_offset in (0, 1):
                candidate_date = dt.date.fromisoformat(g["gameday"]) + dt.timedelta(days=day_offset)
                candidate_utc = dt.datetime.fromisoformat(f"{candidate_date.isoformat()}T{g['gametime']}:00+00:00")
                candidate_local = candidate_utc.astimezone(tz)
                if candidate_local.date().isoformat() == g["gameday"]:
                    game_local = candidate_local.replace(minute=0, second=0, microsecond=0, tzinfo=None)
                    break
            if game_local is None:
                game_local = dt.datetime.fromisoformat(f"{g['gameday']}T{g['gametime']}:00+00:00").astimezone(tz).replace(minute=0, second=0, microsecond=0, tzinfo=None)
            key = game_local.strftime("%Y-%m-%dT%H:%M")
            idx = time_index.get(key)
            if idx is None:
                continue
            cache[g["id"]] = {
                "team": team,
                "gameday": g["gameday"],
                "temp_f": hourly["temperature_2m"][idx],
                "wind_mph": hourly["wind_speed_10m"][idx],
                "wind_dir": hourly["wind_direction_10m"][idx],
                "rh_pct": hourly["relative_humidity_2m"][idx],
                "pressure_hpa": hourly["surface_pressure"][idx],
            }
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=None))
        print(f"    done, {len(cache)} total cached ({time.monotonic() - t0:.0f}s elapsed)")

    print(f"Wrote {CACHE_PATH} ({CACHE_PATH.stat().st_size / 1e6:.2f} MB), {len(cache)} games")


if __name__ == "__main__":
    main()
