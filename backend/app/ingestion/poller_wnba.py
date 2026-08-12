"""WNBA polling/refresh entrypoints -- parallel to poller_nba.py, moneyline
scope. Wired into the FastAPI startup schedule (main.py) alongside the other
sports' pollers. The kalshi moneyline refresh writes a MarketSnapshot per team
per cycle, which is what accumulates real closing prices for CLV + a future
market-odds edge read (the standalone scripts/backtest_wnba_market.py measured
-0.008 vs market once; live capture keeps that current).
"""
import datetime
import logging

from app.clients import espn_wnba_client, kalshi_wnba_client
from app.clients import polymarket_wnba_client
from app.db.database import SessionLocal
from app.db.models import Market, WnbaGame
from app.ingestion import market_catalog_wnba, wnba_data
from app.ingestion.market_matcher_wnba import build_game_index, match_kalshi_event_ticker, match_polymarket_slug
from app.ingestion.poller_lock import db_write_lock
from app.models import season_sim_wnba
from app.models.baseline import elo_service_wnba
from app.models.news_adjustment.injury_rules_wnba import compute_injury_adjustment

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
    team_totals = kalshi_wnba_client.get_team_total_markets()
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
            for row in team_totals:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_wnba.upsert_kalshi_wnba_team_total_market(session, row, game_id)
            session.commit()
            log.info("kalshi wnba spread/total: %d spread + %d total + %d team_total rows, %d matched, %d unmatched",
                     len(spreads), len(totals), len(team_totals), matched, unmatched)
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


def refresh_kalshi_wnba_quarters():
    """1Q-4Q winner, spread and total (twelve live Kalshi series). Priced by
    game_lines_wnba's MEASURED quarter constants -- notably a home edge that is
    65% a FIRST-quarter effect and neutral in the third, which the game model
    cannot express and an even split would get badly wrong."""
    game_index = _load_game_index_readonly()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for quarter in (1, 2, 3, 4):
                for kind, fetch in (("winner", kalshi_wnba_client.get_quarter_winner_markets),
                                    ("spread", kalshi_wnba_client.get_quarter_spread_markets),
                                    ("total", kalshi_wnba_client.get_quarter_total_markets)):
                    for row in fetch(quarter):
                        gid = match_kalshi_event_ticker(row["event_ticker"], game_index)
                        matched += gid is not None
                        unmatched += gid is None
                        market_catalog_wnba.upsert_kalshi_wnba_quarter_market(
                            session, row, gid, quarter, kind)
            session.commit()
            log.info("kalshi wnba quarters: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_wnba_season_sim():
    """Season win-total Monte Carlo, warmed off the request path -- it fetches
    the season-wide schedule (one ESPN call per day), far too slow for a
    request."""
    season_sim_wnba.warm()


def refresh_kalshi_wnba_standings():
    """The five one-per-team season markets.

    KXWNBA1SEED / KXWNBAPLAYOFF resolve on the regular-season TABLE and are
    priced from the season sim's win matrix (standings_probs, no bracket).
    KXWNBA / KXWNBAFINAL / KXWNBASEMIFINAL resolve on the BRACKET and are priced
    from bracket_probs, whose reseeding rule was recovered from the 2024/25
    postseasons rather than assumed.

    One fetch and one upsert covers all five because the row shape is identical
    -- the upsert takes market_type straight from the row's own market_kind."""
    rows = kalshi_wnba_client.get_standings_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_wnba.upsert_kalshi_wnba_standings_market(session, row)
            session.commit()
            log.info("kalshi wnba standings: %d markets ingested", len(rows))
        finally:
            session.close()


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


def refresh_wnba_news_adjustments():
    """Availability adjustment for tracked, not-yet-played WNBA games.

    INJURIES ONLY. The rest/schedule-spot half that poller_nba has was measured
    for the WNBA and REJECTED -- flat and wrong-signed over 1,467 games, see
    scripts/backtest_wnba_rest.py -- so it is deliberately absent rather than
    ported across for symmetry.

    Network first, DB second, per poller_lock.py: the injuries feed and the
    per-player minutes lookups all happen with NO session open, then a single
    write-locked session does the writes with no network left inside it.
    """
    injuries_by_team = espn_wnba_client.fetch_all_injuries()
    # One stats call PER INJURED PLAYER, scoped to the small set actually on
    # today's report (39 league-wide when checked) -- far too expensive to
    # pre-fetch league-wide, cheap at this size. Built once, shared by every
    # game below.
    player_mpg: dict[str, float] = {}
    for team_injuries in injuries_by_team.values():
        for inj in team_injuries:
            name, athlete_id = inj.get("player_name"), inj.get("athlete_id")
            if not name or not athlete_id or name in player_mpg:
                continue
            mpg = espn_wnba_client.fetch_player_season_avg_minutes(athlete_id)
            if mpg is not None:
                player_mpg[name] = mpg

    read_session = SessionLocal()
    try:
        tracked_ids = {
            row[0] for row in read_session.query(Market.wnba_game_id)
            .filter(Market.wnba_game_id.isnot(None)).distinct().all()
        }
        games = [
            {"id": g.id, "home_team": g.home_team, "away_team": g.away_team}
            for g in read_session.query(WnbaGame).filter(WnbaGame.id.in_(tracked_ids)).all()
            if g.home_score is None and g.game_type != "PRE"
        ] if tracked_ids else []
    finally:
        read_session.close()

    if not games:
        return
    with db_write_lock():
        session = SessionLocal()
        try:
            written = 0
            for g in games:
                adj = compute_injury_adjustment(
                    injuries_by_team.get(g["home_team"]) or [],
                    injuries_by_team.get(g["away_team"]) or [],
                    player_mpg,
                )
                if adj is None:
                    continue
                market_catalog_wnba.upsert_wnba_news_adjustment(session, g["id"], adj)
                written += 1
            session.commit()
            log.info("wnba news adjustments: %d of %d tracked games scored", written, len(games))
        finally:
            session.close()


def run_full_refresh_wnba():
    refresh_wnba_games()
    refresh_wnba_news_adjustments()
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
                 refresh_kalshi_wnba_quarters,
                 refresh_kalshi_wnba_win_totals,
                 refresh_kalshi_wnba_standings,
                 settle_placed_bets_wnba):
        try:
            step()
        except Exception:
            log.exception("wnba refresh step %s failed; continuing", step.__name__)
