"""MLB polling/refresh entrypoints -- parallel to poller_nba.py.

Covers moneyline/spread/total/team_total/f5/rfi game markets, the
already-open futures/win-total families, and the situational/news layer
(position-player injuries only so far -- see situational_mlb.py).
Deliberately NOT built yet: F3/F7 (same 3-way shape as F5, different inning
cut, not priced by either platform's own UI the way F5 is), extra-innings,
half-lines.
"""
import datetime
import logging

from app.clients import espn_mlb_client, kalshi_mlb_client, polymarket_mlb_client
from app.db.database import SessionLocal
from app.db.models import Market, MlbGame
from app.ingestion import market_catalog_mlb, mlb_data
from app.ingestion.market_matcher_mlb import build_game_index, match_kalshi_event_ticker, match_polymarket_event
from app.ingestion.poller_lock import db_write_lock
from app.models.news_adjustment.injury_rules_mlb import BatterOpsCache
from app.models.news_adjustment.situational_mlb import compute_situational_adjustment

log = logging.getLogger("poller_mlb")

# Generous season window -- covers spring training through the World Series
# without needing to track exact boundaries (mirrors poller_nba.py's own
# hardcoded-window approach for its Summer League pull).
_CURRENT_SEASON_START = datetime.date(2026, 3, 1)
_CURRENT_SEASON_END = datetime.date(2026, 11, 15)

# Module-level so its TTL persists across poll cycles (same reasoning as
# elo_service_mlb.py's module-level PitcherRatingCache instance).
_batter_ops_cache = BatterOpsCache()


def _load_game_index(session):
    all_games = [
        {"id": g.id, "season": g.season, "away_team": g.away_team, "home_team": g.home_team,
         "gameday": g.gameday, "gametime": g.gametime}
        for g in session.query(MlbGame).all()
    ]
    return build_game_index(all_games)


def _load_game_index_readonly():
    """Short, read-only session (no write lock needed -- WAL-mode reads
    don't contend with writes) used by every refresh_* below that needs to
    resolve real game ids from already-tracked MlbGame rows BEFORE it can
    do its own network fetch -- see poller_lock.py's own docstring for why
    this two-phase (read, then fetch, then locked write) shape replaced
    holding one session open across the whole function."""
    session = SessionLocal()
    try:
        return _load_game_index(session)
    finally:
        session.close()


def refresh_mlb_games():
    games = mlb_data.fetch_games(_CURRENT_SEASON_START, _CURRENT_SEASON_END, game_type="R")
    mlb_data.compute_rest_days(games)
    with db_write_lock():
        session = SessionLocal()
        try:
            count = market_catalog_mlb.upsert_mlb_games(session, games)
            log.info("refreshed %d mlb games", count)
        finally:
            session.close()


def refresh_kalshi_mlb_moneyline():
    game_index = _load_game_index_readonly()
    rows = kalshi_mlb_client.get_moneyline_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_kalshi_mlb_moneyline_market(session, row, game_id)
            session.commit()
            log.info("kalshi mlb moneyline: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_polymarket_mlb_moneyline():
    game_index = _load_game_index_readonly()
    rows = polymarket_mlb_client.get_moneyline_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in rows:
                game_id = match_polymarket_event(row["event_slug"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_polymarket_mlb_moneyline_row(session, row, game_id)
            session.commit()
            log.info("polymarket mlb moneyline: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_kalshi_mlb_spread_total():
    game_index = _load_game_index_readonly()
    spread_rows = kalshi_mlb_client.get_spread_markets()
    total_rows = kalshi_mlb_client.get_total_markets()
    team_total_rows = kalshi_mlb_client.get_team_total_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in spread_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_kalshi_mlb_spread_market(session, row, game_id)
            for row in total_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_kalshi_mlb_total_market(session, row, game_id)
            for row in team_total_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_kalshi_mlb_team_total_market(session, row, game_id)
            session.commit()
            log.info("kalshi mlb spread/total/team_total: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_polymarket_mlb_spread_total():
    game_index = _load_game_index_readonly()
    spread_rows = polymarket_mlb_client.get_spread_markets()
    total_rows = polymarket_mlb_client.get_total_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in spread_rows:
                game_id = match_polymarket_event(row["event_slug"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_polymarket_mlb_spread_row(session, row, game_id)
            for row in total_rows:
                game_id = match_polymarket_event(row["event_slug"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_polymarket_mlb_total_row(session, row, game_id)
            session.commit()
            log.info("polymarket mlb spread/total: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_kalshi_mlb_f5_rfi():
    game_index = _load_game_index_readonly()
    f5_rows = kalshi_mlb_client.get_f5_markets()
    rfi_rows = kalshi_mlb_client.get_rfi_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in f5_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_kalshi_mlb_f5_market(session, row, game_id)
            for row in rfi_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_kalshi_mlb_rfi_market(session, row, game_id)
            session.commit()
            log.info("kalshi mlb f5/rfi: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_polymarket_mlb_f5_rfi():
    game_index = _load_game_index_readonly()
    f5_rows = polymarket_mlb_client.get_f5_markets()
    rfi_rows = polymarket_mlb_client.get_rfi_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in f5_rows:
                game_id = match_polymarket_event(row["game_slug"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_polymarket_mlb_f5_row(session, row, game_id)
            for row in rfi_rows:
                game_id = match_polymarket_event(row["event_slug"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_mlb.upsert_polymarket_mlb_rfi_row(session, row, game_id)
            session.commit()
            log.info("polymarket mlb f5/rfi: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_kalshi_mlb_futures():
    futures_rows = kalshi_mlb_client.get_futures_markets()
    win_total_rows = kalshi_mlb_client.get_win_total_markets()
    matchup_rows = kalshi_mlb_client.get_world_series_matchup_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in futures_rows:
                market_catalog_mlb.upsert_kalshi_mlb_futures_market(session, row)
            for row in win_total_rows:
                market_catalog_mlb.upsert_kalshi_mlb_win_total_market(session, row)
            for row in matchup_rows:
                market_catalog_mlb.upsert_kalshi_mlb_ws_matchup_market(session, row)
            session.commit()
            log.info(
                "refreshed kalshi mlb futures + win totals + %d ws matchups",
                len(matchup_rows),
            )
        finally:
            session.close()


def refresh_polymarket_mlb_futures():
    futures_rows = polymarket_mlb_client.get_futures_markets()
    win_total_rows = polymarket_mlb_client.get_win_total_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            skipped = 0
            for row in futures_rows:
                if market_catalog_mlb.upsert_polymarket_mlb_futures_market(session, row) is None:
                    skipped += 1
            for row in win_total_rows:
                if market_catalog_mlb.upsert_polymarket_mlb_win_total_market(session, row) is None:
                    skipped += 1
            session.commit()
            log.info("refreshed polymarket mlb futures + win totals (%d unresolved team names skipped)", skipped)
        finally:
            session.close()


def refresh_mlb_ratings():
    """Elo, recomputed from the just-ingested MlbGame rows, plus the season
    sim (feeds the futures markets' model_prob -- see season_sim_service_mlb
    .py, added 2026-07-17, Phase 10 polish pass)."""
    from app.models.baseline import elo_service_mlb
    from app.models import season_sim_service_mlb

    elo_service_mlb.refresh_ratings()
    season_sim_service_mlb.refresh()


def refresh_mlb_news_adjustments():
    """Free (ESPN injuries + a bulk hitting-stats call, no paid API) --
    runs every poll cycle since there's no cost concern. Only scores
    TRACKED, not-yet-played games -- same pattern as poller.py's NFL
    refresh_news_adjustments/poller_nba.py's NBA equivalent."""
    injuries_by_team = espn_mlb_client.fetch_all_injuries()
    with db_write_lock():
        session = SessionLocal()
        try:
            tracked_game_ids = {
                row[0] for row in session.query(Market.mlb_game_id)
                .filter(Market.mlb_game_id.isnot(None), Market.sport == "mlb").distinct()
            }
            updated = 0
            for game_id in tracked_game_ids:
                game = session.get(MlbGame, game_id)
                if game is None or game.home_score is not None:
                    continue
                adjustment = compute_situational_adjustment(
                    home_injuries=injuries_by_team.get(game.home_team, []),
                    away_injuries=injuries_by_team.get(game.away_team, []),
                    season=game.season,
                    ops_cache=_batter_ops_cache,
                )
                if adjustment is not None:
                    market_catalog_mlb.upsert_mlb_news_adjustment(session, game.id, adjustment)
                    updated += 1
            log.info("mlb news adjustments: %d/%d tracked games scored", updated, len(tracked_game_ids))
        finally:
            session.close()


def settle_placed_bets_mlb():
    """MLB weekly-pool bets never auto-settled before this (bet_settlement.py
    was hardcoded to NflGame) -- fixed 2026-07-17, Phase 10 polish pass. Same
    `settle_finished_games` NFL's poller.py calls, which now dispatches on
    bet.sport internally (see bet_settlement.py::_get_game) -- called here
    too so MLB settlement isn't silently dependent on NFL's own poller
    staying enabled. Only moneyline/spread/total/team_total are gradeable --
    F5/RFI aren't (this app's live DB only stores each game's FINAL score,
    not per-inning linescores, see bet_settlement.py's own docstring)."""
    from app.models.bet_settlement import settle_finished_games

    with db_write_lock():
        session = SessionLocal()
        try:
            settle_finished_games(session)
        finally:
            session.close()


def run_full_refresh_mlb():
    refresh_mlb_games()
    refresh_mlb_ratings()
    refresh_kalshi_mlb_moneyline()
    refresh_polymarket_mlb_moneyline()
    refresh_kalshi_mlb_spread_total()
    refresh_polymarket_mlb_spread_total()
    refresh_kalshi_mlb_f5_rfi()
    refresh_polymarket_mlb_f5_rfi()
    refresh_kalshi_mlb_futures()
    refresh_polymarket_mlb_futures()
    refresh_mlb_news_adjustments()
    settle_placed_bets_mlb()
