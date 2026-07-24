"""WNBA polling/refresh entrypoints -- parallel to poller_nba.py, moneyline
scope. Wired into the FastAPI startup schedule (main.py) alongside the other
sports' pollers. The kalshi moneyline refresh writes a MarketSnapshot per team
per cycle, which is what accumulates real closing prices for CLV + a future
market-odds edge read (the standalone scripts/backtest_wnba_market.py measured
-0.008 vs market once; live capture keeps that current).
"""
import datetime
import logging

from app.clients import kalshi_wnba_client
from app.db.database import SessionLocal
from app.db.models import WnbaGame
from app.ingestion import market_catalog_wnba, wnba_data
from app.ingestion.market_matcher_wnba import build_game_index, match_kalshi_event_ticker
from app.ingestion.poller_lock import db_write_lock

log = logging.getLogger("poller_wnba")

# Generous WNBA window (season runs ~mid-May to mid-Oct); covers the current +
# next calendar-year season without tracking exact boundaries -- wnba_data
# reads each game's own season off ESPN regardless of the fetch window.
def _season_window():
    year = datetime.date.today().year
    return datetime.date(year, 4, 15), datetime.date(year, 11, 15)


def _load_game_index_readonly():
    session = SessionLocal()
    try:
        rows = [
            {"id": g.id, "season": g.season, "away_team": g.away_team, "home_team": g.home_team, "gameday": g.gameday}
            for g in session.query(WnbaGame).all()
        ]
    finally:
        session.close()
    return build_game_index(rows)


def refresh_wnba_games():
    start, end = _season_window()
    games = wnba_data.fetch_games(start, end)
    with db_write_lock():
        session = SessionLocal()
        try:
            count = market_catalog_wnba.upsert_wnba_games(session, games)
            log.info("refreshed %d wnba games", count)
        finally:
            session.close()


def refresh_wnba_ratings():
    from app.models.baseline import elo_service_wnba
    elo_service_wnba.refresh_ratings()


def refresh_kalshi_wnba_moneyline():
    game_index = _load_game_index_readonly()
    rows = kalshi_wnba_client.get_moneyline_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_wnba.upsert_kalshi_wnba_moneyline_market(session, row, game_id)
            session.commit()
            log.info("kalshi wnba moneyline: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def settle_placed_bets_wnba():
    from app.models.bet_settlement import settle_finished_games
    with db_write_lock():
        session = SessionLocal()
        try:
            settle_finished_games(session)
        finally:
            session.close()


def run_full_refresh_wnba():
    refresh_wnba_games()
    refresh_wnba_ratings()
    refresh_kalshi_wnba_moneyline()
    settle_placed_bets_wnba()
