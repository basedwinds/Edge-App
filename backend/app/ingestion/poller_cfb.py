"""College-football poll cycle -- parallel to poller_wnba.py.

Ordering matters and is deliberate: schedule -> ratings -> markets. The Elo
service seeds from the historical cache plus CfbGame rows, so refreshing ratings
before the schedule would rate the season off stale data, and matching markets
before the schedule would leave freshly-listed games unlinked for a cycle.
"""
import datetime
import logging

from app.clients import espn_cfb_client, kalshi_cfb_client, polymarket_cfb_client
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
from app.models import playoff_sim_cfb, season_sim_cfb
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


def refresh_kalshi_cfb_spread():
    """KXNCAAFSPREAD -- priced from game_lines_cfb, whose slope and spread were
    fitted on 4,836 CFB games (NFL's constants are 3.3x off; see that module).

    Team resolution and game matching are the SAME path the moneyline uses, on
    purpose: a 130-team sport needs both the ticker suffix and the display name,
    and the opponent is recovered from the other rows sharing this event. The
    only addition is `line`.

    The opponent lookup deliberately reuses the spread rows themselves -- a
    spread event carries rungs for BOTH teams, so the opposing team appears in
    this same list, exactly as it does for moneyline.
    """
    schedule = _load_schedule_readonly()
    if not schedule:
        log.info("cfb spread: no schedule rows yet, skipping")
        return
    game_index = build_game_index(schedule)
    known = {g["home_abbr"] for g in schedule} | {g["away_abbr"] for g in schedule}
    name_index = _NAME_INDEX_CACHE.get("index") or {}

    rows = kalshi_cfb_client.get_spread_markets()
    if not rows:
        # Not an error: Kalshi lists CFB spreads near game week, and the series
        # had zero markets in every status when this was built (2026-08-06).
        log.info("cfb spread: no open markets listed")
        return
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = unresolved = 0
            for row in rows:
                team = resolve_team(row.get("team_abbr_kalshi"), row.get("display_name"), name_index, known)
                parsed = parse_kalshi_event_ticker(row["event_ticker"])
                if team is None or parsed is None:
                    unresolved += 1
                    continue
                opponent = _opponent_for(row["event_ticker"], rows, name_index, known, team)
                game_id = (
                    match_game(team, opponent, parsed["date"], game_index)
                    or match_game(opponent, team, parsed["date"], game_index)
                ) if opponent else None
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_cfb.upsert_kalshi_cfb_spread_market(session, dict(row, team=team), game_id)
            session.commit()
            log.info("kalshi cfb spread: %d rows, %d matched, %d unmatched, %d unresolved",
                     len(rows), matched, unmatched, unresolved)
        finally:
            session.close()


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


_CONF_SIM: dict = {"data": {}}


def refresh_cfb_conference_sim():
    """Conference standings/title Monte Carlo, off the request path like the win
    sim. Reuses the same season schedule fetch."""
    try:
        games = season_sim_cfb._fetch_season_games()
        _CONF_SIM["data"] = season_sim_cfb.simulate_conferences(games=games)
    except Exception:
        log.exception("cfb conference sim failed")


def refresh_kalshi_cfb_conference_futures():
    """Champion / championship-qualifier / regular-season-top-N ladders."""
    sim = _CONF_SIM.get("data") or {}
    known = set(sim.get("champion") or {})
    fetchers = (
        (kalshi_cfb_client.get_conference_champion_markets, "conference_champion"),
        (kalshi_cfb_client.get_conference_qualifier_markets, "conference_qualifier"),
        (kalshi_cfb_client.get_conference_regtop_markets, "conference_regtop"),
    )
    name_index = _NAME_INDEX_CACHE.get("index") or {}
    with db_write_lock():
        session = SessionLocal()
        try:
            total = resolved = 0
            for fetch, mtype in fetchers:
                for row in fetch():
                    total += 1
                    team = resolve_team(row.get("team_abbr_kalshi"), row.get("display_name"),
                                        name_index, known)
                    if team is None:
                        continue
                    resolved += 1
                    market_catalog_cfb.upsert_kalshi_cfb_conference_market(session, row, team, mtype)
            session.commit()
            log.info("kalshi cfb conference futures: %d rows, %d resolved", total, resolved)
        finally:
            session.close()


def refresh_cfb_playoff_sim():
    """12-team CFP bracket Monte Carlo. Off the request path; reuses the season
    schedule. See playoff_sim_cfb -- its seeding is a committee PROXY."""
    playoff_sim_cfb.warm()


def refresh_kalshi_cfb_playoff_futures():
    """Playoff qualification, quarterfinal qualification, and title-by-
    conference."""
    sim = playoff_sim_cfb.get() or {}
    known = set(sim.get("playoff") or {})
    name_index = _NAME_INDEX_CACHE.get("index") or {}
    fetchers = (
        (kalshi_cfb_client.get_playoff_markets, "cfb_playoff", True),
        (kalshi_cfb_client.get_quarterfinal_markets, "cfb_quarterfinal", True),
        # Conference-labelled, not team-labelled -- store the Kalshi code as-is
        # and let the router map it to a conference name.
        (kalshi_cfb_client.get_title_conference_markets, "cfb_title_conference", False),
    )
    with db_write_lock():
        session = SessionLocal()
        try:
            total = resolved = 0
            for fetch, mtype, by_team in fetchers:
                for row in fetch():
                    total += 1
                    if by_team:
                        team = resolve_team(row.get("team_abbr_kalshi"), row.get("display_name"),
                                            name_index, known)
                        if team is None:
                            continue
                    else:
                        team = row.get("team_abbr_kalshi")
                    resolved += 1
                    market_catalog_cfb.upsert_kalshi_cfb_conference_market(session, row, team, mtype)
            session.commit()
            log.info("kalshi cfb playoff futures: %d rows, %d resolved", total, resolved)
        finally:
            session.close()


def settle_placed_bets_cfb():
    """Placeholder to keep the cycle shape identical to the other sports.
    Settlement needs finished games with scores, which only exist once the season
    starts -- wiring a grader against zero data would be untestable."""
    return


def refresh_polymarket_cfb_futures():
    """Polymarket CFB season futures. CFB was the only sport in this app with
    zero Polymarket coverage (audited 2026-08-07), so before this every CFB
    market was single-source: no cross-platform divergence could be found on it
    and there was no second book to compare a Kalshi price against.

    Uses the SAME name index the Kalshi path uses -- Polymarket writes full
    display names with mascots ("Boston College Eagles"), which is the shape
    resolve_team already handles. Measured at ingestion time: 402/402 rows
    resolved.
    """
    rows = polymarket_cfb_client.get_cfb_futures_markets()
    if not rows:
        log.info("polymarket cfb futures: no rows returned")
        return
    name_index = _NAME_INDEX_CACHE.get("index") or {}
    if not name_index:
        # Ordered after refresh_cfb_games in run_full_refresh_cfb, which is what
        # populates this. Bail rather than drop all 402 rows as unresolved --
        # that would look like Polymarket had no supply.
        log.warning("polymarket cfb futures: name index empty, skipping this cycle")
        return
    known = set(name_index.values())
    stored = unresolved = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                team = resolve_team(None, row["team_name"], name_index, known)
                if market_catalog_cfb.upsert_polymarket_cfb_futures_market(session, row, team) is None:
                    unresolved += 1
                else:
                    stored += 1
            session.commit()
        finally:
            session.close()
    log.info("polymarket cfb futures: %d stored, %d unresolved, across %d market types",
             stored, unresolved, len({r["market_type"] for r in rows}))


def run_full_refresh_cfb():
    for step in (refresh_cfb_games, refresh_cfb_ratings, refresh_cfb_season_sim,
                 refresh_cfb_conference_sim, refresh_kalshi_cfb_moneyline,
                 refresh_kalshi_cfb_spread,
                 refresh_kalshi_cfb_win_totals, refresh_kalshi_cfb_conference_futures,
                 refresh_cfb_playoff_sim, refresh_kalshi_cfb_playoff_futures,
                 refresh_polymarket_cfb_futures):
        try:
            step()
        except Exception:
            log.exception("cfb refresh step %s failed; continuing", step.__name__)
