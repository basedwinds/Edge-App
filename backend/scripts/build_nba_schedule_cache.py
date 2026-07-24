"""One-off local cache builder for NBA historical schedule/results.

Unlike NFL (one nflverse CSV covers 2012-2025 in a single request), NBA has
no equivalent free bulk file -- app/ingestion/nba_data.py pulls ESPN's
scoreboard endpoint in 7-day windows (confirmed ~18s/season, so a 12-season
pull is a few minutes, not instant). Cached to disk here so the Elo
backtest/training scripts don't re-hit ESPN on every run.

Run: backend/.venv/Scripts/python.exe scripts/build_nba_schedule_cache.py
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion import nba_data  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "nba_schedule_cache.json"
START = dt.date(2013, 10, 1)  # covers season label 2014 onward
# REAL GAP caught 2026-07-16: this was hardcoded to 2025-06-30, silently
# missing the entire 2025-26 season (1,391 games) every time this script was
# rerun -- Elo/scoring ratings built from the cache were a full season stale
# with no error or warning. Update this to "today" (or later) whenever
# rebuilding, not a fixed date -- there is no automatic reminder otherwise.
END = dt.date.today()


def main():
    print(f"Fetching NBA games {START} .. {END} (chunked, ~18s/season)...")
    games = nba_data.fetch_games(START, END)
    nba_data.compute_rest_days(games)
    games.sort(key=lambda g: (g["gameday"], g["id"]))

    by_type = {}
    for g in games:
        by_type[g["game_type"]] = by_type.get(g["game_type"], 0) + 1
    print(f"Total games: {len(games)}  |  by type: {by_type}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(games, indent=None))
    print(f"Wrote {CACHE_PATH} ({CACHE_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
