"""One-off local cache builder for MLB historical schedule/results.

MLB Stats API returns a full season in one request (confirmed live, no
chunking needed -- see mlb_data.py's docstring), so this is much faster than
NBA's equivalent script. Cached to disk so Elo/pitcher-rating backtest
scripts don't re-hit the API on every run.

Run: backend/.venv/Scripts/python.exe scripts/build_mlb_schedule_cache.py
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion import mlb_data  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "mlb_schedule_cache.json"
START_SEASON = 2016
END = dt.date.today()  # update whenever rebuilding -- no automatic reminder, see build_nba_schedule_cache.py's own note


def main():
    all_games = []
    for season in range(START_SEASON, END.year + 1):
        season_start = dt.date(season, 3, 1)
        season_end = min(dt.date(season, 11, 15), END)
        if season_start > END:
            break
        t0 = time.monotonic()
        games = mlb_data.fetch_games(season_start, season_end, game_type="R")
        print(f"{season}: {len(games)} REG games ({time.monotonic() - t0:.1f}s)")
        all_games.extend(games)

    mlb_data.compute_rest_days(all_games)
    all_games.sort(key=lambda g: (g["gameday"], g["game_number"], g["id"]))

    print(f"Total games: {len(all_games)}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(all_games, indent=None))
    print(f"Wrote {CACHE_PATH} ({CACHE_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
