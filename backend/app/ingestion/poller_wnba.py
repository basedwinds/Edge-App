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
from app.clients import polymarket_wnba_client
from app.db.database import SessionLocal
from app.db.models import WnbaGame
from app.ingestion import market_catalog_wnba, wnba_data
from app.ingestion.market_matcher_wnba import build_game_index, match_kalshi_event_ticker, match_polymarket_slug
from app.ingestion.poller_lock import db_write_lock
from app.models import season_sim_wnba
from app.models.baseline import elo_service_wnba

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


def refresh_polymarket_wnba_moneyline():
    """Polymarket per-game moneyline (tag_slug="wnba").

    WNBA was the last sport the health check flagged as "Polymarket lists this
    sport but we ingest none of it" -- a whole platform's prices, cross-platform
    divergences and CLV missing. Polymarket carries only the moneyline per game
    (no bundled spread/total), so that is all this ingests.

    Matching goes slug-date + resolved full team names, never the slug's own
    team codes: "la" is the LA Sparks and "las" is the Las Vegas Aces.
    """
    game_index = _load_game_index_readonly()
    rows = polymarket_wnba_client.get_moneyline_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            # Both rows of a game share a slug; resolve the pairing once per slug
            # so each row is matched against the real (away, home), not itself.
            teams_by_slug: dict[str, list[str]] = {}
            for r in rows:
                teams_by_slug.setdefault(r["event_slug"], []).append(r["team_espn_abbr"])
            for row in rows:
                pair = teams_by_slug.get(row["event_slug"]) or []
                game_id = (
                    match_polymarket_slug(row["event_slug"], pair[0], pair[1], game_index)
                    if len(pair) == 2 else None
                )
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_wnba.upsert_polymarket_wnba_moneyline_row(session, row, game_id)
            session.commit()
            log.info("polymarket wnba moneyline: %d matched, %d unmatched", matched, unmatched)
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
    # refresh_wnba_season_sim is NOT in this chain -- it runs as its own
    # scheduler job (see scheduler.py). It fetches its own season schedule and
    # needs only Elo, and refresh_wnba_games ahead of it is ~124 sequential ESPN
    # calls, so keeping pricing off that critical path is worth it on its own.
    #
    # For the record, since the comment here previously claimed otherwise: that
    # queueing was NOT why the win totals went unpriced. The real cause was a
    # missing module import in this file -- both elo_service_wnba and
    # season_sim_wnba were referenced but never imported, so refresh_wnba_ratings
    # and refresh_wnba_season_sim both raised NameError on every run. In the
    # original straight-line chain that killed the whole refresh at step 2, which
    # is why no WNBA market ever refreshed either.
    #
    # Steps are also individually guarded, matching run_full_refresh_cfb: a
    # straight-line chain meant one raising step (a Kalshi 429, say) silently
    # skipped everything after it -- including settlement -- with no log line
    # naming which step died.
    for step in (refresh_wnba_ratings,
                 refresh_kalshi_wnba_moneyline,
                 refresh_polymarket_wnba_moneyline,
                 refresh_kalshi_wnba_spread_total,
                 refresh_kalshi_wnba_halves,
                 refresh_kalshi_wnba_win_totals,
                 settle_placed_bets_wnba):
        try:
            step()
        except Exception:
            log.exception("wnba refresh step %s failed; continuing", step.__name__)
