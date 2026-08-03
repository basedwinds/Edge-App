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


def refresh_kalshi_wnba_spread_total():
    """Spread + total ladders (KXWNBASPREAD / KXWNBATOTAL). WNBA was moneyline-
    only until 2026-08-02 even though Kalshi has run these series all along --
    both reuse the existing WNBA Elo via the same NBA-shaped models, so this is
    ingestion + wiring rather than a new model."""
    game_index = _load_game_index_readonly()
    spreads = kalshi_wnba_client.get_spread_markets()
    totals = kalshi_wnba_client.get_total_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in spreads:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_wnba.upsert_kalshi_wnba_spread_market(session, row, game_id)
            for row in totals:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_wnba.upsert_kalshi_wnba_total_market(session, row, game_id)
            session.commit()
            log.info("kalshi wnba spread/total: %d spread + %d total rows, %d matched, %d unmatched",
                     len(spreads), len(totals), matched, unmatched)
        finally:
            session.close()


def refresh_kalshi_wnba_halves():
    """1H/2H winner, spread and total (six live Kalshi series). Priced by
    game_lines_wnba's MEASURED half constants -- notably a second half that
    carries no home-court edge, which the data showed and the game model cannot
    express."""
    game_index = _load_game_index_readonly()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for half in (1, 2):
                for kind, fetch in (("winner", kalshi_wnba_client.get_half_winner_markets),
                                    ("spread", kalshi_wnba_client.get_half_spread_markets),
                                    ("total", kalshi_wnba_client.get_half_total_markets)):
                    for row in fetch(half):
                        gid = match_kalshi_event_ticker(row["event_ticker"], game_index)
                        matched += gid is not None
                        unmatched += gid is None
                        market_catalog_wnba.upsert_kalshi_wnba_half_market(session, row, gid, half, kind)
            session.commit()
            log.info("kalshi wnba halves: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_wnba_season_sim():
    """Season win-total Monte Carlo, warmed off the request path -- it fetches
    the season-wide schedule (one ESPN call per day), far too slow for a
    request."""
    season_sim_wnba.warm()


def refresh_kalshi_wnba_win_totals():
    """KXWNBAWINS ladders. Season-long, so no game match is attempted."""
    rows = kalshi_wnba_client.get_win_total_markets()
    if not rows:
        return
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_wnba.upsert_kalshi_wnba_win_total_market(session, row)
            session.commit()
            log.info("kalshi wnba win totals: %d rows", len(rows))
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
    refresh_kalshi_wnba_spread_total()
    refresh_kalshi_wnba_halves()
    refresh_wnba_season_sim()
    refresh_kalshi_wnba_win_totals()
    settle_placed_bets_wnba()
