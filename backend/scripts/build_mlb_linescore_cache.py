"""One-off local cache builder for per-inning linescores -- powers the
first-5-innings (F5) 3-way margin model and the RFI (run-in-1st-inning)
binary model, neither of which the schedule/byDateRange bulk endpoints
expose. Uses the dedicated `/game/{gamePk}/linescore` endpoint (confirmed
live: ~3.3KB, far smaller than the full boxscore or live-feed endpoints used
elsewhere in this app -- no bulk-across-games version exists either way, so
this still costs one call per game).

Scoped to 2021-2025 + partial 2026 (~13,600 completed REG games) rather than
the full 2016-2025 history the moneyline baseline uses -- these are NEW
markets being built for the first time (not re-validating an existing
signal), so a smaller-but-still-real sample is the right first cut; can
cheaply extend back further later given the endpoint's low cost if needed.

Stores, per gamePk: runs scored by each team in innings 1-5 (F5 margin
input) and whether a run scored in inning 1 for each team (RFI input) --
raw per-inning runs are kept too so any other inning-cut question can reuse
this cache without a re-fetch.

Resumable: writes incrementally every N games.

Run: backend/.venv/Scripts/python.exe scripts/build_mlb_linescore_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.base import get_json  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_linescore_cache.json"
SCHEDULE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
TARGET_SEASONS = {2021, 2022, 2023, 2024, 2025, 2026}
SAVE_EVERY = 200


def _fetch_linescore(game_pk) -> dict:
    return get_json(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore")


def main():
    games = json.loads(SCHEDULE_CACHE_PATH.read_text())
    games = [
        g for g in games
        if g["season"] in TARGET_SEASONS and g["game_type"] == "R" and g.get("home_score") is not None
    ]
    games.sort(key=lambda g: (g["gameday"], g["game_number"], g["id"]))
    print(f"{len(games)} completed REG games ({sorted(TARGET_SEASONS)}) to fetch linescores for")

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
            ls = _fetch_linescore(gid)
            innings = ls.get("innings", [])
            home_by_inning = [inn.get("home", {}).get("runs") for inn in innings]
            away_by_inning = [inn.get("away", {}).get("runs") for inn in innings]
            if not innings or home_by_inning[0] is None:
                n_errors += 1
                continue
            home_f5 = sum(r for r in home_by_inning[:5] if r is not None)
            away_f5 = sum(r for r in away_by_inning[:5] if r is not None)
            home_i1 = home_by_inning[0] or 0
            away_i1 = away_by_inning[0] or 0
            cache[gid] = {
                "gameday": g["gameday"],
                "home_team": g["home_team"],
                "away_team": g["away_team"],
                "n_innings": len(innings),
                "home_f5_runs": home_f5,
                "away_f5_runs": away_f5,
                "rfi": (home_i1 + away_i1) > 0,
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
