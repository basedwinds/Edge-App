"""One-time seed: loads the cached 12-season historical dataset (data/
nba_schedule_cache.json, built by build_nba_schedule_cache.py for the Phase
1 backtest) into the live NbaGame table.

REAL GAP caught live 2026-07-16: elo_service_nba.py/scoring_ratings_service_
nba.py read live ratings from the DB (not a fresh fetch every cycle, unlike
NFL's nflverse-CSV-based elo_service.py -- see that file's docstring for
why), but poller_nba.py's regular refresh_nba_games() only pulls the CURRENT
season's window. Without this one-time seed, the DB never gets the historical
games Elo needs to train on, and refresh_ratings() silently produces "0 teams
rated" -- every team stuck at the base 1500 rating with no real
differentiation. nflverse's CSV always has full history built in; NBA's
local cache needs an explicit one-time load into the DB to play the same
role. Not part of the 5-minute poll cycle -- run once (or after a cache
rebuild).

Run: backend/.venv/Scripts/python.exe scripts/seed_nba_historical_games.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal, init_db  # noqa: E402
from app.ingestion import market_catalog_nba  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "nba_schedule_cache.json"


def main():
    init_db()
    games = json.loads(CACHE_PATH.read_text())
    session = SessionLocal()
    try:
        count = market_catalog_nba.upsert_nba_games(session, games)
    finally:
        session.close()
    print(f"Seeded {count} historical NBA games into the DB from {CACHE_PATH}")


if __name__ == "__main__":
    main()
