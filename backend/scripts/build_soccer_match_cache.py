"""One-off cache builder for Soccer match history.

Two independent sub-jobs, parallel to build_tennis_match_cache.py:
1. football-data.co.uk (EPL/La Liga/Serie A/Bundesliga/Ligue 1) -- fast, a
   few hundred small CSV downloads (5 leagues x ~30 seasons), no
   checkpointing needed.
2. ESPN's free scoreboard API (MLS) -- chunked date-range requests, default
   start year 2018 (a pragmatic, non-guessed-but-unverified-deeper choice --
   MLS ships without a backtest anyway since ESPN has no odds, so going back
   further than a handful of seasons has diminishing value for a live-only
   rating pool; extend --mls-start-year if deeper history is ever wanted).

Run: backend/.venv/Scripts/python.exe scripts/build_soccer_match_cache.py [--football-data-only] [--espn-only] [--mls-start-year YYYY]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion import soccer_data  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--football-data-only", action="store_true")
    parser.add_argument("--espn-only", action="store_true")
    parser.add_argument("--mls-start-year", type=int, default=2018)
    args = parser.parse_args()

    if not args.espn_only:
        print("Fetching football-data.co.uk history (EPL/La Liga/Serie A/Bundesliga/Ligue 1)...")
        fd_matches = soccer_data.build_football_data_cache()
        print(f"  {len(fd_matches)} matches cached -> {soccer_data.FOOTBALL_DATA_CACHE_PATH}")
        by_league: dict[str, int] = {}
        for m in fd_matches:
            by_league[m["league"]] = by_league.get(m["league"], 0) + 1
        for league, count in sorted(by_league.items()):
            print(f"    {league}: {count}")

    if not args.football_data_only:
        print(f"Fetching ESPN MLS history (from {args.mls_start_year})...")
        mls_matches = soccer_data.build_espn_mls_cache(start_year=args.mls_start_year)
        print(f"  {len(mls_matches)} matches cached -> {soccer_data.ESPN_MLS_CACHE_PATH}")


if __name__ == "__main__":
    main()
