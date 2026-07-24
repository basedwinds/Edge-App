"""One-off local cache builder for per-game bullpen workload -- feasibility
data for a bullpen-fatigue signal (flagged in pitcher_ratings_mlb.py-adjacent
project notes as "the most promising remaining MLB-native candidate" but
never checked against real data because it needs box-score-level pitching
lines, which neither the schedule nor byDateRange bulk endpoints expose).

Unlike build_mlb_pitcher_snapshot_cache.py's ONE-call-per-14-days bulk
approach, there's no bulk-across-games boxscore endpoint -- this costs one
call per game (~0.35s each, confirmed live). Scoped to ONE full season
(2024, the most recent COMPLETE season) rather than the full 2016-2025
history, matching this project's "validate cheaply before committing to
expensive infrastructure" discipline -- if the signal isn't real on 2,430
games, more history won't change that conclusion; if it IS real, backfilling
more seasons is a cheap follow-up.

Stores, per (gamePk): each team's starter pitch count and TOTAL RELIEF pitch
count (relievers = every pitcher after the first in the boxscore's own
`teams.{home,away}.pitchers` appearance-order list) -- pitch count, not IP,
is the workload measure (a team can throw 40 relief pitches over 1.0 taxing
inning or 15 over an easy 2.0, pitches better reflects "how much arm was
used" than innings does).

Resumable: writes incrementally to a partial cache file every N games so an
interrupted run doesn't lose progress and can pick back up.

Run: backend/.venv/Scripts/python.exe scripts/build_mlb_boxscore_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients import statsapi_mlb_client  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_boxscore_cache.json"
SCHEDULE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
TARGET_SEASON = 2024
SAVE_EVERY = 100


def _team_pitching_line(team_box: dict) -> dict:
    order = team_box.get("pitchers", [])
    starter_pitches = 0.0
    relief_pitches = 0.0
    relief_ip = 0.0
    for i, pid in enumerate(order):
        p = team_box["players"].get(f"ID{pid}")
        if not p:
            continue
        stats = p.get("stats", {}).get("pitching", {})
        pitches = stats.get("numberOfPitches") or 0
        ip_raw = stats.get("inningsPitched")
        try:
            ip = float(ip_raw) if ip_raw is not None else 0.0
        except ValueError:
            ip = 0.0
        if i == 0:
            starter_pitches = float(pitches)
        else:
            relief_pitches += float(pitches)
            relief_ip += ip
    return {"starter_pitches": starter_pitches, "relief_pitches": relief_pitches, "relief_ip": round(relief_ip, 1)}


def main():
    games = json.loads(SCHEDULE_CACHE_PATH.read_text())
    games = [g for g in games if g["season"] == TARGET_SEASON and g["game_type"] == "R"]
    games.sort(key=lambda g: (g["gameday"], g["game_number"], g["id"]))
    print(f"{len(games)} {TARGET_SEASON} REG games to fetch box scores for")

    cache: dict[str, dict] = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
        print(f"Resuming: {len(cache)} games already cached")

    t0 = time.monotonic()
    n_fetched = 0
    n_errors = 0
    for g in games:
        gid = g["id"]
        if gid in cache:
            continue
        try:
            box = statsapi_mlb_client.get_boxscore(gid)
            home = box["teams"]["home"]
            away = box["teams"]["away"]
            cache[gid] = {
                "gameday": g["gameday"],
                "home_team": home["team"]["abbreviation"],
                "away_team": away["team"]["abbreviation"],
                "home": _team_pitching_line(home),
                "away": _team_pitching_line(away),
            }
        except Exception as e:
            n_errors += 1
            print(f"  error on game {gid}: {e}")
            continue
        n_fetched += 1
        if n_fetched % SAVE_EVERY == 0:
            CACHE_PATH.write_text(json.dumps(cache, indent=None))
            elapsed = time.monotonic() - t0
            print(f"  {len(cache)}/{len(games)} cached ({elapsed:.0f}s elapsed, {n_errors} errors)")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=None))
    print(f"Done: {len(cache)}/{len(games)} games cached, {n_errors} errors, "
          f"{time.monotonic() - t0:.0f}s this run. Wrote {CACHE_PATH} "
          f"({CACHE_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
