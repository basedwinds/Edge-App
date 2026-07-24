"""NBA polling/refresh entrypoints -- parallel to poller.py (NFL). Not yet
wired into the main run_full_refresh() cycle or the FastAPI startup schedule
-- that's the next natural step (alongside exposing NBA through the API
routers), deliberately left for when the dashboard actually needs to show
this data (Phase 3), rather than scheduling background work nothing reads
yet.
"""
import datetime
import logging

from app.clients import espn_nba_client, kalshi_nba_client, polymarket_nba_client
from app.db.database import SessionLocal
from app.db.models import Market, NbaCoachSnapshot, NbaGame
from app.ingestion import market_catalog_nba, nba_data, nba_summer_league_data
from app.ingestion.market_matcher_nba import build_game_index, match_kalshi_event_ticker, match_polymarket_event
from app.ingestion.poller_lock import db_write_lock
from app.models.news_adjustment.situational_nba import compute_situational_adjustment

log = logging.getLogger("poller_nba")

# Regular-season pull window -- generous enough to cover pre/regular/
# play-in/post-season for the current + upcoming season without needing to
# track exact season boundaries (see nba_data.py's own season-classification
# logic, which reads it off each game's own ESPN data regardless of what
# window it was fetched in).
_CURRENT_SEASON_START = datetime.date(2026, 9, 1)
_CURRENT_SEASON_END = datetime.date(2027, 7, 1)

# NBA Summer League runs roughly the first three weeks of July -- generous
# window, not exact dates (mirrors this app's other "wide enough, cheap to
# over-cover" date-window choices).
_SUMMER_LEAGUE_START = "20260701"
_SUMMER_LEAGUE_END = "20260731"


def _load_game_index(session):
    all_games = [
        {"id": g.id, "season": g.season, "away_team": g.away_team, "home_team": g.home_team, "gameday": g.gameday}
        for g in session.query(NbaGame).all()
    ]
    return build_game_index(all_games)


def _load_game_index_readonly():
    """Short, read-only session (no write lock needed -- WAL-mode reads
    don't contend with writes) -- see poller_mlb.py's own version of this
    helper for the same reasoning."""
    session = SessionLocal()
    try:
        return _load_game_index(session)
    finally:
        session.close()


def refresh_nba_games():
    games = nba_data.fetch_games(_CURRENT_SEASON_START, _CURRENT_SEASON_END)
    with db_write_lock():
        session = SessionLocal()
        try:
            count = market_catalog_nba.upsert_nba_games(session, games)
            log.info("refreshed %d nba games", count)
        finally:
            session.close()


def refresh_summer_league_games():
    games = nba_summer_league_data.fetch_games(_SUMMER_LEAGUE_START, _SUMMER_LEAGUE_END)
    with db_write_lock():
        session = SessionLocal()
        try:
            count = market_catalog_nba.upsert_nba_games(session, games)
            log.info("refreshed %d nba summer league games", count)
        finally:
            session.close()


def refresh_kalshi_nba_moneyline():
    game_index = _load_game_index_readonly()
    rows_by_series = {
        label: kalshi_nba_client.get_moneyline_markets(series)
        for series, label in (
            (kalshi_nba_client.MONEYLINE_SERIES, "regular"),
            (kalshi_nba_client.SUMMER_MONEYLINE_SERIES, "summer"),
        )
    }
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for rows in rows_by_series.values():
                for row in rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    if game_id is not None:
                        matched += 1
                    else:
                        unmatched += 1
                    market_catalog_nba.upsert_kalshi_nba_moneyline_market(session, row, game_id)
                session.commit()
            log.info("kalshi nba moneyline: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_polymarket_nba_moneyline():
    game_index = _load_game_index_readonly()
    rows = polymarket_nba_client.get_summer_league_moneyline_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in rows:
                game_id = match_polymarket_event(row["event_slug"], game_index)
                if game_id is not None:
                    matched += 1
                else:
                    unmatched += 1
                market_catalog_nba.upsert_polymarket_nba_moneyline_row(session, row, game_id)
            session.commit()
            log.info("polymarket nba moneyline (summer league): %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_kalshi_nba_spread_total():
    """Regular-season spread/total (0 open, season starts October) + Summer
    League spread/total (LIVE right now, real ladder data) -- Polymarket has
    NO spread/total equivalent to ingest yet: confirmed live 2026-07-16 that
    Summer League's Polymarket markets are single-binary-only (no bundled
    spread/total the way NFL's regular-season bundle works), and regular-
    season NBA isn't open on Polymarket at all yet. Not built pending real
    structure to verify against -- same "don't guess a platform's market
    shape without evidence" discipline this project applies elsewhere,
    rather than blindly porting NFL's Polymarket bundle-parsing assumption
    onto a platform/sport pairing with zero observable data."""
    game_index = _load_game_index_readonly()
    rows_by_series = [
        (kalshi_nba_client._get_team_ladder_markets(spread_series), kalshi_nba_client._get_game_ladder_markets(total_series))
        for spread_series, total_series in (
            (kalshi_nba_client.SPREAD_SERIES, kalshi_nba_client.TOTAL_SERIES),
            (kalshi_nba_client.SUMMER_SPREAD_SERIES, kalshi_nba_client.SUMMER_TOTAL_SERIES),
        )
    ]
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for spread_rows, total_rows in rows_by_series:
                for row in spread_rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    matched += game_id is not None
                    unmatched += game_id is None
                    market_catalog_nba.upsert_kalshi_nba_spread_market(session, row, game_id)
                for row in total_rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    matched += game_id is not None
                    unmatched += game_id is None
                    market_catalog_nba.upsert_kalshi_nba_total_market(session, row, game_id)
                session.commit()
            log.info("kalshi nba spread/total: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_kalshi_nba_team_total_and_half_lines():
    """KXNBATEAMTOTAL/KXNBA{1,2}H{SPREAD,TOTAL} -- confirmed to exist in the
    live catalog (2026-07-17) but 0 open events so far, same pattern as
    regular-season spread/total. No Summer League equivalent checked/built
    for these (Summer League only confirmed to have game/spread/total
    series, not team-total or half-line ladders)."""
    game_index = _load_game_index_readonly()
    team_total_rows = kalshi_nba_client.get_team_total_markets()
    half_rows = {
        half: (kalshi_nba_client.get_half_spread_markets(half), kalshi_nba_client.get_half_total_markets(half))
        for half in (1, 2)
    }
    with db_write_lock():
        session = SessionLocal()
        try:
            matched = unmatched = 0
            for row in team_total_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                matched += game_id is not None
                unmatched += game_id is None
                market_catalog_nba.upsert_kalshi_nba_team_total_market(session, row, game_id)
            for half, (spread_rows, total_rows) in half_rows.items():
                for row in spread_rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    matched += game_id is not None
                    unmatched += game_id is None
                    market_catalog_nba.upsert_kalshi_nba_half_spread_market(session, row, game_id, f"spread_{half}h")
                for row in total_rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    matched += game_id is not None
                    unmatched += game_id is None
                    market_catalog_nba.upsert_kalshi_nba_half_total_market(session, row, game_id, f"total_{half}h")
            session.commit()
            log.info("kalshi nba team-total/half-lines: %d matched, %d unmatched", matched, unmatched)
        finally:
            session.close()


def refresh_kalshi_nba_futures():
    rows = kalshi_nba_client.get_futures_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_nba.upsert_kalshi_nba_futures_market(session, row)
            session.commit()
            log.info("refreshed %d kalshi nba futures markets", len(rows))
        finally:
            session.close()


def refresh_polymarket_nba_futures():
    rows = polymarket_nba_client.get_futures_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            skipped = 0
            for row in rows:
                if market_catalog_nba.upsert_polymarket_nba_futures_market(session, row) is None:
                    skipped += 1
            session.commit()
            log.info("refreshed %d polymarket nba futures markets (%d unresolved team names skipped)", len(rows) - skipped, skipped)
        finally:
            session.close()


def refresh_kalshi_nba_records():
    """KXNBARECORD-27BEST/-27WORST -- found during a post-Phase-2 "check if
    anything was missed" audit pass (2026-07-16): 60 real open markets that
    the original build missed entirely because the ticker guessed by analogy
    to NFL ("KXRECORDNBABEST") doesn't exist. Same row shape as
    get_futures_markets(), reuses the same upsert function."""
    rows = kalshi_nba_client.get_record_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_nba.upsert_kalshi_nba_futures_market(session, row)
            session.commit()
            log.info("refreshed %d kalshi nba best/worst record markets", len(rows))
        finally:
            session.close()


def refresh_kalshi_nba_win_totals():
    """0 open markets right now (season starts October) -- built ready for
    when Kalshi lists KXNBAWINS-{team}, same pattern as refresh_kalshi_
    nba_futures for KXRECORDNBABEST (also 0 open, also wired in already)."""
    rows = kalshi_nba_client.get_win_total_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_nba.upsert_kalshi_nba_win_total_market(session, row)
            session.commit()
            log.info("refreshed %d kalshi nba win-total markets", len(rows))
        finally:
            session.close()


def refresh_nba_news_adjustments():
    """Free (ESPN injuries + standings, no paid API) -- runs every poll
    cycle since there's no cost concern. Only scores TRACKED, not-yet-played,
    non-excluded (SUMMER/PRE) games -- same pattern as poller.py's NFL
    refresh_news_adjustments. Standings are fetched lazily, only if at least
    one tracked game is in the final-stretch month (see playoff_motivation_
    nba.py's FINAL_STRETCH_MONTH), to avoid a pointless call the rest of the
    season when it can't fire anyway.

    REAL BUG this fixes (found live 2026-07-20, worst offender in this
    file -- ~30 fetch_current_coach calls PLUS a lazily-fetched standings
    call used to all happen INSIDE one open session, interleaved with real
    DB reads/writes): restructured into 3 phases -- (1) every network call
    that has NO real DB dependency (injuries, player_ppg, all 30 coach
    fetches) happens first with no session open; (2) a short READ-ONLY
    session (no write lock needed) loads the schedule index, tracked game
    rows, and each coach's own real season (the one DB-dependent input the
    coach fetch needs) all at once; (3) the standings-per-season fetch (the
    one remaining real network call, now easy to pre-compute exactly which
    seasons are needed from phase 2's plain data) runs with no session
    open; (4) a single write-locked session does every remaining DB write
    with zero network I/O left inside it -- see poller_lock.py's own
    docstring for the full real-bug story this class of fix addresses."""
    injuries_by_team = espn_nba_client.fetch_all_injuries()
    # Player-value proxy (2026-07-17): one stats call PER INJURED PLAYER,
    # scoped to only the (small, naturally-bounded) set who actually appear
    # on today's injury report -- too expensive to pre-fetch league-wide,
    # cheap enough for this. Built once per cycle, shared across every
    # tracked game below.
    player_ppg: dict[str, float] = {}
    for team_injuries in injuries_by_team.values():
        for inj in team_injuries:
            name, athlete_id = inj.get("player_name"), inj.get("athlete_id")
            if not name or not athlete_id or name in player_ppg:
                continue
            ppg = espn_nba_client.fetch_player_season_avg_points(athlete_id)
            if ppg is not None:
                player_ppg[name] = ppg

    # Coaching-change detection (2026-07-17): one roster call per team,
    # cheap (30 calls/cycle) -- pure network, no DB dependency at all.
    coach_name_by_team: dict[str, str] = {}
    for team in espn_nba_client.TEAM_ABBREVIATIONS:
        coach_name = espn_nba_client.fetch_current_coach(team)
        if coach_name is not None:
            coach_name_by_team[team] = coach_name

    # Phase 2: short read-only session (no write lock -- WAL-mode reads
    # don't contend with writes) for everything that needs the DB but not
    # a write: each coach's own real season (from that team's most recent
    # tracked/cached game), the REG schedule index, and the tracked-game
    # rows themselves (captured as plain dicts so nothing ORM-bound
    # outlives this session).
    read_session = SessionLocal()
    try:
        season_by_team: dict[str, int] = {}
        for team in coach_name_by_team:
            latest_game = (
                read_session.query(NbaGame)
                .filter((NbaGame.home_team == team) | (NbaGame.away_team == team))
                .order_by(NbaGame.gameday.desc())
                .first()
            )
            season_by_team[team] = latest_game.season if latest_game else datetime.date.today().year

        reg_rows = read_session.query(NbaGame).filter(NbaGame.game_type == "REG").all()
        schedule_index = nba_data.build_team_schedule_index(
            [
                {
                    "id": g.id, "game_type": g.game_type, "season": g.season, "gameday": g.gameday,
                    "home_team": g.home_team, "away_team": g.away_team,
                    "home_score": g.home_score, "away_score": g.away_score,
                }
                for g in reg_rows
            ]
        )

        tracked_game_ids = {
            row[0] for row in read_session.query(Market.nba_game_id).filter(Market.nba_game_id.isnot(None), Market.sport == "nba").distinct()
        }
        tracked_games = []
        for game_id in tracked_game_ids:
            game = read_session.get(NbaGame, game_id)
            if game is None or game.home_score is not None or game.game_type in ("SUMMER", "PRE"):
                continue
            tracked_games.append({
                "id": game.id, "home_team": game.home_team, "away_team": game.away_team,
                "gameday": game.gameday, "season": game.season,
            })
    finally:
        read_session.close()

    # Phase 3: standings fetch, now pre-computable from tracked_games'
    # plain data alone (no DB needed) -- same lazy "only if a tracked game
    # is in the final-stretch month" gate as before, just moved ahead of
    # any session.
    seasons_needing_standings = {
        g["season"] for g in tracked_games if int(g["gameday"].split("-")[1]) >= 4
    }
    standings_by_season = {season: espn_nba_client.fetch_standings(season) for season in seasons_needing_standings}

    # Phase 4: single write-locked session, zero network I/O left inside it.
    with db_write_lock():
        session = SessionLocal()
        try:
            coach_by_team: dict[str, NbaCoachSnapshot] = {}
            for team, coach_name in coach_name_by_team.items():
                snapshot, _ = market_catalog_nba.upsert_coach_snapshot(session, team, coach_name, season_by_team[team])
                coach_by_team[team] = snapshot

            updated = 0
            for g in tracked_games:
                standing_by_team = standings_by_season.get(g["season"]) if g["season"] in seasons_needing_standings else None

                home_last, home_next = nba_data.get_adjacent_games(schedule_index, g["season"], g["home_team"], g["id"])
                away_last, away_next = nba_data.get_adjacent_games(schedule_index, g["season"], g["away_team"], g["id"])

                home_coach = coach_by_team.get(g["home_team"])
                away_coach = coach_by_team.get(g["away_team"])

                adjustment = compute_situational_adjustment(
                    home_team=g["home_team"],
                    away_team=g["away_team"],
                    away_injuries=injuries_by_team.get(g["away_team"], []),
                    home_injuries=injuries_by_team.get(g["home_team"], []),
                    gameday=g["gameday"],
                    away_standing=(standing_by_team or {}).get(g["away_team"]),
                    home_standing=(standing_by_team or {}).get(g["home_team"]),
                    home_last_opp=(home_last or {}).get("opponent"),
                    home_last_won=(home_last or {}).get("won"),
                    home_next_opp=(home_next or {}).get("opponent"),
                    away_last_opp=(away_last or {}).get("opponent"),
                    away_last_won=(away_last or {}).get("won"),
                    away_next_opp=(away_next or {}).get("opponent"),
                    player_ppg=player_ppg,
                    away_coach_name=away_coach.coach_name if away_coach else None,
                    away_previous_coach_name=away_coach.previous_coach_name if away_coach else None,
                    away_coach_since=away_coach.since if away_coach else None,
                    home_coach_name=home_coach.coach_name if home_coach else None,
                    home_previous_coach_name=home_coach.previous_coach_name if home_coach else None,
                    home_coach_since=home_coach.since if home_coach else None,
                )
                if adjustment is not None:
                    market_catalog_nba.upsert_nba_news_adjustment(session, g["id"], adjustment)
                    updated += 1
            session.commit()
            log.info("nba news adjustments: %d/%d tracked games scored", updated, len(tracked_games))
        finally:
            session.close()


def refresh_nba_ratings():
    """Elo + scoring ratings + season sim, recomputed from the just-ingested
    NbaGame rows (DB read, not a live re-fetch -- see elo_service_nba.py's
    docstring for why). Feeds game_lines_nba.py's spread/total probability
    estimates and the futures markets' model_prob. Season sim degrades
    gracefully to a no-op until the target season's schedule is published
    (see season_sim_service_nba.py)."""
    from app.models.baseline import elo_service_nba
    from app.models import scoring_ratings_service_nba, season_sim_service_nba

    elo_service_nba.refresh_ratings()
    scoring_ratings_service_nba.refresh()
    season_sim_service_nba.refresh()


def settle_placed_bets_nba():
    """NBA weekly-pool bets never auto-settled before this (bet_settlement.py
    was hardcoded to NflGame) -- fixed 2026-07-17, Phase 10 polish pass. Same
    `settle_finished_games` NFL's poller.py calls, which now dispatches on
    bet.sport internally (see bet_settlement.py::_get_game) -- called here
    too (not just relying on NFL's own poll cycle to catch NBA bets, even
    though it technically would since the query isn't sport-scoped) so NBA
    settlement isn't silently dependent on NFL's poller staying enabled."""
    from app.models.bet_settlement import settle_finished_games

    with db_write_lock():
        session = SessionLocal()
        try:
            settle_finished_games(session)
        finally:
            session.close()


def run_full_refresh_nba():
    refresh_nba_games()
    refresh_summer_league_games()
    refresh_nba_ratings()
    refresh_kalshi_nba_moneyline()
    refresh_polymarket_nba_moneyline()
    refresh_kalshi_nba_spread_total()
    refresh_kalshi_nba_team_total_and_half_lines()
    refresh_kalshi_nba_futures()
    refresh_kalshi_nba_records()
    refresh_kalshi_nba_win_totals()
    refresh_polymarket_nba_futures()
    refresh_nba_news_adjustments()
    settle_placed_bets_nba()
