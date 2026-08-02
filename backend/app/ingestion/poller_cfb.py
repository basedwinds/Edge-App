"""College-football poll cycle -- parallel to poller_wnba.py.

Ordering matters and is deliberate: schedule -> ratings -> markets. The Elo
service seeds from the historical cache plus CfbGame rows, so refreshing ratings
before the schedule would rate the season off stale data, and matching markets
before the schedule would leave freshly-listed games unlinked for a cycle.
"""
import datetime
import logging

from app.clients import espn_cfb_client, kalshi_cfb_client
from app.db.database import SessionLocal
from app.ingestion.poller_lock import db_write_lock
from app.db.models import CfbGame
from app.ingestion import market_catalog_cfb
from app.ingestion.market_matcher_cfb import (
    build_game_index,
    build_name_index,
    match_game,
    parse_kalshi_event_ticker,
    resolve_team,
)
from app.models import season_sim_cfb
from app.models.baseline import elo_service_cfb

log = logging.getLogger("poller_cfb")


def _load_schedule_readonly() -> list[dict]:
    """CfbGame rows shaped for the matcher's indexes. Read-only, no write lock."""
    session = SessionLocal()
    try:
        return [
            {
                "id": g.id, "season": g.season, "date": g.gameday,
                "home_abbr": g.home_team, "away_abbr": g.away_team,
                "home_team": g.home_team, "away_team": g.away_team,
            }
            for g in session.query(CfbGame).all()
        ]
    finally:
        session.close()


def refresh_cfb_games():
    events = espn_cfb_client.fetch_upcoming_and_recent()
    games = [g for g in (espn_cfb_client.parse_event(e) for e in events) if g]
    if not games:
        log.info("cfb schedule: no events returned (offseason or fetch failure)")
        return
    with db_write_lock():
        session = SessionLocal()
        try:
            n = market_catalog_cfb.upsert_cfb_games(session, games)
            log.info("cfb schedule: %d games upserted", n)
        finally:
            session.close()
    # Stash the live name index for the market pass -- it needs display names,
    # which are NOT persisted on CfbGame (see market_matcher_cfb.build_name_index
    # for why the historical cache can't supply them).
    _NAME_INDEX_CACHE["index"] = build_name_index([
        {
            "home_team": g["home_team"], "away_team": g["away_team"],
            "home_name": g.get("home_name"), "away_name": g.get("away_name"),
            "home_short": g.get("home_short"), "away_short": g.get("away_short"),
        }
        for g in games
    ])


_NAME_INDEX_CACHE: dict = {"index": {}}


def refresh_cfb_ratings():
    elo_service_cfb.refresh_ratings()


def refresh_kalshi_cfb_moneyline():
    schedule = _load_schedule_readonly()
    if not schedule:
        log.info("cfb markets: no schedule rows yet, skipping")
        return
    game_index = build_game_index(schedule)
    known = {g["home_abbr"] for g in schedule} | {g["away_abbr"] for g in schedule}
    name_index = _NAME_INDEX_CACHE.get("index") or {}

    rows = kalshi_cfb_client.get_moneyline_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = unresolved = 0
            for row in rows:
                team = resolve_team(row.get("team_abbr_kalshi"), row.get("display_name"), name_index, known)
                parsed = parse_kalshi_event_ticker(row["event_ticker"])
                if team is None or parsed is None:
                    # Never guess -- an unlinked market is recoverable, a market
                    # linked to the WRONG game misprices and missettles silently.
                    unresolved += 1
                    continue
                opponent = _opponent_for(row["event_ticker"], rows, name_index, known, team)
                game_id = (
                    match_game(team, opponent, parsed["date"], game_index)
                    or match_game(opponent, team, parsed["date"], game_index)
                ) if opponent else None
                matched += game_id is not None
                unmatched += game_id is None
                row_for_db = dict(row, team=team)
                market_catalog_cfb.upsert_kalshi_cfb_moneyline_market(session, row_for_db, game_id)
            session.commit()
            log.info("kalshi cfb moneyline: %d rows, %d matched, %d unmatched, %d unresolved",
                     len(rows), matched, unmatched, unresolved)
        finally:
            session.close()


def _opponent_for(event_ticker: str, rows: list[dict], name_index: dict,
                  known: set, team: str) -> str | None:
    """The OTHER team in this event. Both sides of a game are separate markets
    sharing one event_ticker, so the pair is read by grouping rather than by
    splitting the ticker's teams-blob (ambiguous across ~130 FBS teams)."""
    for other in rows:
        if other["event_ticker"] != event_ticker:
            continue
        cand = resolve_team(other.get("team_abbr_kalshi"), other.get("display_name"), name_index, known)
        if cand and cand != team:
            return cand
    return None


def refresh_cfb_season_sim():
    """Season win-total Monte Carlo. Runs AFTER ratings (it reads them) and off
    the request path -- its own season-wide schedule fetch is ~100 ESPN calls,
    far too slow to run inside a request. Self-caches on a 1h TTL."""
    season_sim_cfb.warm()


def refresh_kalshi_cfb_win_totals():
    """KXNCAAFWINS ladders. Team resolution is abbreviation-ONLY here: these
    markets label themselves "9+ wins" rather than by team, so the matcher's
    display-name fallback has nothing to work with."""
    dist, trials = season_sim_cfb.get()
    rows = kalshi_cfb_client.get_win_total_markets()
    if not rows:
        return
    known = set(dist)
    with db_write_lock():
        session = SessionLocal()
        try:
            resolved = unresolved = 0
            for row in rows:
                team = resolve_team(row["team_abbr_kalshi"], None, {}, known)
                if team is None:
                    unresolved += 1
                    continue
                resolved += 1
                market_catalog_cfb.upsert_kalshi_cfb_win_total_market(session, row, team)
            session.commit()
            log.info("kalshi cfb win totals: %d rows, %d resolved, %d unresolved",
                     len(rows), resolved, unresolved)
        finally:
            session.close()


def settle_placed_bets_cfb():
    """Placeholder to keep the cycle shape identical to the other sports.
    Settlement needs finished games with scores, which only exist once the season
    starts -- wiring a grader against zero data would be untestable."""
    return


def run_full_refresh_cfb():
    for step in (refresh_cfb_games, refresh_cfb_ratings, refresh_cfb_season_sim,
                 refresh_kalshi_cfb_moneyline, refresh_kalshi_cfb_win_totals):
        try:
            step()
        except Exception:
            log.exception("cfb refresh step %s failed; continuing", step.__name__)
