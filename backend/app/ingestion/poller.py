import datetime
import logging

from app.clients import depth_chart_client, espn_client, kalshi_client, polymarket_client
from app.db.database import SessionLocal
from app.db.models import Market, NflGame, Setting
from app.ingestion import market_catalog, nfl_data, preseason_data
from app.ingestion.poller_lock import db_write_lock
from app.ingestion.market_matcher import (
    build_game_index,
    match_kalshi_event_ticker,
    match_kalshi_moneyline_event,
    match_polymarket_event,
    parse_polymarket_slug,
)
from app.models.news_adjustment.playoff_motivation import MIN_WEEK_FOR_ONE_SEED
from app.models.news_adjustment.situational import compute_situational_adjustment
from app.models.epa_ratings import get_current_epa_ratings
from app.models.qb_ratings import get_qb_career_stats
from app.models.skill_position_ratings import get_receiving_career_stats, get_rushing_career_stats
from app.models.awards import (
    build_all_starters_full_name_to_team,
    build_coach_name_to_team,
    build_offensive_skill_full_name_to_team,
    build_qb_rb_full_name_to_team,
    resolve_coach_candidate_team,
    resolve_player_candidate_team_full_name,
)
from app.models.defensive_ratings import get_defensive_career_scores

log = logging.getLogger("poller")


def _load_game_index(session):
    all_games = [
        {"id": g.id, "season": g.season, "away_team": g.away_team, "home_team": g.home_team, "gameday": g.gameday}
        for g in session.query(NflGame).all()
    ]
    return build_game_index(all_games)


def refresh_nfl_games():
    games = nfl_data.fetch_games()
    with db_write_lock():
        session = SessionLocal()
        try:
            count = market_catalog.upsert_nfl_games(session, games)
            log.info("refreshed %d nfl games", count)
        finally:
            session.close()


def refresh_nfl_half_scores():
    """Backfills HALF-time scores onto played NFL games, so the 1H/2H winner
    markets can settle.

    nflverse -- refresh_nfl_games's source -- publishes only the FINAL score,
    so without this every winner_1h/winner_2h bet would sit pending forever.
    That is precisely the defect soccer shipped with (573 bets, 2 settled), so
    it is wired at the same time as the markets rather than after.

    Fetched a WEEK at a time, never a whole season: ESPN caps a scoreboard
    response and truncates silently from the start of the range, which is how
    the soccer pipeline lost every result after April.
    """
    read_session = SessionLocal()
    try:
        pending = (
            read_session.query(NflGame)
            .filter(NflGame.home_score.isnot(None), NflGame.home_score_1h.is_(None))
            .all()
        )
        days = sorted({g.gameday for g in pending if g.gameday})
        index = {(g.gameday, g.home_team, g.away_team): g.id for g in pending if g.gameday}
    finally:
        read_session.close()
    if not days:
        return

    found: dict[tuple, dict] = {}
    start = datetime.date.fromisoformat(days[0])
    end = datetime.date.fromisoformat(days[-1])
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + datetime.timedelta(days=6), end)
        for row in espn_client.fetch_half_scores(cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")):
            if row.get("gameday"):
                found[(row["gameday"], row["home_abbr"], row["away_abbr"])] = row
        cursor = chunk_end + datetime.timedelta(days=1)

    with db_write_lock():
        session = SessionLocal()
        try:
            updated = 0
            for key, game_id in index.items():
                row = found.get(key)
                if row is None:
                    continue
                game = session.get(NflGame, game_id)
                if game is None:
                    continue
                game.home_score_1h = row["home_score_1h"]
                game.away_score_1h = row["away_score_1h"]
                updated += 1
            session.commit()
            log.info("nfl half scores: %d/%d played games backfilled", updated, len(index))
        finally:
            session.close()


def refresh_preseason_games():
    """nflverse (refresh_nfl_games's source) never publishes preseason --
    pulled from ESPN's scoreboard instead (see ingestion/preseason_data.py).
    Tied to whatever season nflverse's own schedule currently considers
    "latest" so this doesn't need its own separate notion of "current
    season"."""
    reg_games = nfl_data.fetch_games()
    if not reg_games:
        return
    season = max(g["season"] for g in reg_games)
    games = preseason_data.fetch_preseason_games(season)
    with db_write_lock():
        session = SessionLocal()
        try:
            count = market_catalog.upsert_nfl_games(session, games)
            log.info("refreshed %d preseason games (season %d)", count, season)
        finally:
            session.close()


def refresh_kalshi_futures():
    """Division winner / conference champion / 1-seed / Super Bowl champion /
    playoff qualifier -- see kalshi_client.py::get_futures_markets for why
    these five and not the other 235+ NFL series Kalshi lists."""
    rows = kalshi_client.get_futures_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog.upsert_kalshi_futures_market(session, row)
            session.commit()
            log.info("kalshi futures: %d markets ingested", len(rows))
        finally:
            session.close()


def refresh_kalshi_playoff_seed_and_host():
    """KXNFLSEED + KXNFLPLAYOFFHOST -- both priced from season_sim outputs added
    2026-08-06 (seed_pct / playoff_host_pct). Fetched together because they are
    the same shape of season-long team market and share a refresh cadence.

    Each series is fetched in its own try: one Kalshi hiccup should not cost the
    other's ingestion, the same reason the racing championship warm loop is
    per-series."""
    rows: list[dict] = []
    pairs = (
        ("playoff seed", kalshi_client.get_playoff_seed_markets,
         market_catalog.upsert_kalshi_playoff_seed_market),
        ("playoff host", kalshi_client.get_playoff_host_markets,
         market_catalog.upsert_kalshi_playoff_host_market),
    )
    for label, fetch, upsert in pairs:
        try:
            fetched = fetch()
        except Exception:
            log.exception("kalshi %s fetch failed", label)
            continue
        with db_write_lock():
            session = SessionLocal()
            try:
                for row in fetched:
                    upsert(session, row)
                session.commit()
                log.info("kalshi %s: %d markets ingested", label, len(fetched))
            except Exception:
                log.exception("kalshi %s upsert failed", label)
            finally:
                session.close()
        rows.extend(fetched)
    return len(rows)


def refresh_kalshi_stage_of_elim():
    """KXNFLSTAGEOFELIM (stage of elimination) -- per (team, round) markets,
    priced from season_sim's stage_exit_pct. See
    kalshi_client.get_stage_of_elimination_markets."""
    rows = kalshi_client.get_stage_of_elimination_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog.upsert_kalshi_stage_of_elim_market(session, row)
            session.commit()
            log.info("kalshi stage-of-elimination: %d markets ingested", len(rows))
        finally:
            session.close()


def refresh_awards():
    """MVP / Coach of the Year -- see app/models/awards.py for the scoring
    methodology and the honest explanation of why DPOY/OPOY/OROY/DROY and
    player-movement/business/culture markets are explicitly NOT attempted.
    Ingestion-time team resolution (candidate name -> team, via depth-chart/
    coach reverse lookups) happens here rather than at request time in
    markets.py, since it only needs to run once per poll cycle, not once
    per API request.

    REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    connection across slow network I/O" anti-pattern this app's other
    pollers all had -- see poller_lock.py's own docstring): ~7 separate
    depth-chart/Kalshi/Polymarket calls used to happen INSIDE one open
    session. This one genuinely needs a quick DB read first (max_season,
    coach_by_team), which doesn't need the write lock (WAL-mode reads
    don't contend with writes) -- then every network fetch with no
    session open, then a final session under
    poller_lock.py::db_write_lock() for the real writes."""
    read_session = SessionLocal()
    try:
        max_season = read_session.query(NflGame.season).order_by(NflGame.season.desc()).limit(1).scalar()
        if max_season is None:
            return
        coach_by_team: dict[str, str] = {}
        for g in read_session.query(NflGame).filter(NflGame.season == max_season).all():
            if g.home_coach:
                coach_by_team.setdefault(g.home_team, g.home_coach)
            if g.away_coach:
                coach_by_team.setdefault(g.away_team, g.away_coach)
    finally:
        read_session.close()

    try:
        skill_starters = depth_chart_client.get_skill_position_starters(max_season)
    except Exception:
        skill_starters = {}
    qb_rb_name_to_team = build_qb_rb_full_name_to_team(skill_starters)
    offensive_skill_name_to_team = build_offensive_skill_full_name_to_team(skill_starters)
    coach_name_to_team = build_coach_name_to_team(coach_by_team)

    mvp_rows = kalshi_client.get_mvp_markets()
    coty_rows = kalshi_client.get_coach_of_year_markets()
    poly_mvp_rows = polymarket_client.get_mvp_markets()
    opoy_rows = kalshi_client.get_opoy_markets()
    try:
        pooled_starters = depth_chart_client.get_current_starters(max_season)
    except Exception:
        pooled_starters = {}
    all_starters_name_to_team = build_all_starters_full_name_to_team(pooled_starters)
    dpoy_rows = kalshi_client.get_dpoy_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            matched = 0
            for row in mvp_rows:
                team_abbr = resolve_player_candidate_team_full_name(row["candidate_name"], qb_rb_name_to_team)
                if team_abbr:
                    matched += 1
                market_catalog.upsert_kalshi_award_market(session, row, team_abbr, "mvp")
            session.commit()
            log.info("kalshi mvp: %d markets ingested (%d team-resolved)", len(mvp_rows), matched)

            matched = 0
            for row in coty_rows:
                team_abbr = resolve_coach_candidate_team(row["candidate_name"], coach_name_to_team)
                if team_abbr:
                    matched += 1
                market_catalog.upsert_kalshi_award_market(session, row, team_abbr, "coach_of_year")
            session.commit()
            log.info("kalshi coach-of-year: %d markets ingested (%d team-resolved)", len(coty_rows), matched)

            matched = 0
            for row in poly_mvp_rows:
                team_abbr = resolve_player_candidate_team_full_name(row["candidate_name"], qb_rb_name_to_team)
                if team_abbr:
                    matched += 1
                market_catalog.upsert_polymarket_award_market(session, row, team_abbr, "mvp")
            session.commit()
            log.info("polymarket mvp: %d markets ingested (%d team-resolved)", len(poly_mvp_rows), matched)

            matched = 0
            for row in opoy_rows:
                team_abbr = resolve_player_candidate_team_full_name(row["candidate_name"], offensive_skill_name_to_team)
                if team_abbr:
                    matched += 1
                market_catalog.upsert_kalshi_award_market(session, row, team_abbr, "opoy")
            session.commit()
            log.info("kalshi opoy: %d markets ingested (%d team-resolved)", len(opoy_rows), matched)

            matched = 0
            for row in dpoy_rows:
                team_abbr = resolve_player_candidate_team_full_name(row["candidate_name"], all_starters_name_to_team)
                if team_abbr:
                    matched += 1
                market_catalog.upsert_kalshi_award_market(session, row, team_abbr, "dpoy")
            session.commit()
            log.info("kalshi dpoy: %d markets ingested (%d team-resolved)", len(dpoy_rows), matched)
        finally:
            session.close()


def refresh_stat_leaders():
    """League-leader categorical markets (KXLEADERNFL* family, 9 stat
    categories) + team points-scored/allowed most/least -- see
    stat_leaders.py for the raw-counting-stat career totals these are
    scored against in markets.py. Offensive categories (pass/rush/rec)
    resolve team via the same QB/RB/WR/TE depth-chart lookup OPOY uses;
    defensive categories (sacks, def_int) via the same pooled-starters
    lookup DPOY uses.

    REAL BUG this fixes (found live 2026-07-20, same anti-pattern as
    refresh_awards above -- see poller_lock.py's own docstring): ~15
    separate depth-chart/Kalshi calls used to happen INSIDE one open
    session. Same real-DB-read-first, then-fetch, then-locked-write shape
    as refresh_awards."""
    read_session = SessionLocal()
    try:
        max_season = read_session.query(NflGame.season).order_by(NflGame.season.desc()).limit(1).scalar()
    finally:
        read_session.close()
    if max_season is None:
        return

    try:
        skill_starters = depth_chart_client.get_skill_position_starters(max_season)
    except Exception:
        skill_starters = {}
    offensive_skill_name_to_team = build_offensive_skill_full_name_to_team(skill_starters)

    try:
        pooled_starters = depth_chart_client.get_current_starters(max_season)
    except Exception:
        pooled_starters = {}
    all_starters_name_to_team = build_all_starters_full_name_to_team(pooled_starters)

    offensive_types = {"leader_pass_yds", "leader_pass_tds", "leader_pass_int", "leader_rush_yds", "leader_rush_tds", "leader_rec_yds", "leader_rec_tds"}
    defensive_types = {"leader_def_int", "leader_sacks"}
    leader_rows_by_type = {market_type: kalshi_client.get_leader_markets(market_type) for market_type in offensive_types | defensive_types}
    team_points_rows_by_type = {
        market_type: kalshi_client.get_team_points_markets(market_type)
        for market_type in ("team_pts_most", "team_pts_least", "team_dpts_most", "team_dpts_least")
    }

    with db_write_lock():
        session = SessionLocal()
        try:
            for market_type, rows in leader_rows_by_type.items():
                name_to_team = offensive_skill_name_to_team if market_type in offensive_types else all_starters_name_to_team
                matched = 0
                for row in rows:
                    team_abbr = resolve_player_candidate_team_full_name(row["candidate_name"], name_to_team)
                    if team_abbr:
                        matched += 1
                    market_catalog.upsert_kalshi_award_market(session, row, team_abbr, market_type)
                session.commit()
                log.info("kalshi %s: %d markets ingested (%d team-resolved)", market_type, len(rows), matched)

            for market_type, rows in team_points_rows_by_type.items():
                for row in rows:
                    row["market_kind"] = market_type
                    market_catalog.upsert_kalshi_futures_market(session, row)
                session.commit()
                log.info("kalshi %s: %d markets ingested", market_type, len(rows))
        finally:
            session.close()


def refresh_season_stat_ladders():
    """Season-total threshold ladders (KXNFLSEASON{PASSYDS,RSHYDS,RECYDS,
    REC,RECTD,RSHTD}) -- see kalshi_client.py::SEASON_STAT_SERIES and
    season_projections.py for the probability model these are scored
    against in markets.py. Team resolution reuses the same QB/RB/WR/TE
    depth-chart lookup OPOY/stat-leaders already use.

    REAL BUG this fixes (found live 2026-07-20): same anti-pattern as
    refresh_awards/refresh_stat_leaders above, same real fix shape."""
    read_session = SessionLocal()
    try:
        max_season = read_session.query(NflGame.season).order_by(NflGame.season.desc()).limit(1).scalar()
    finally:
        read_session.close()
    if max_season is None:
        return

    try:
        skill_starters = depth_chart_client.get_skill_position_starters(max_season)
    except Exception:
        skill_starters = {}
    offensive_skill_name_to_team = build_offensive_skill_full_name_to_team(skill_starters)

    from app.clients.kalshi_client import SEASON_STAT_SERIES

    rows_by_category = {category: kalshi_client.get_season_stat_markets(category) for category in SEASON_STAT_SERIES}

    with db_write_lock():
        session = SessionLocal()
        try:
            for category, rows in rows_by_category.items():
                matched = 0
                for row in rows:
                    team_abbr = resolve_player_candidate_team_full_name(row["candidate_name"], offensive_skill_name_to_team)
                    if team_abbr:
                        matched += 1
                    market_catalog.upsert_kalshi_season_stat_market(session, row, team_abbr, category)
                session.commit()
                log.info("kalshi season_%s: %d markets ingested (%d team-resolved)", category, len(rows), matched)
        finally:
            session.close()


def refresh_division_extras():
    """Division wins ladder / division exact order / division most-or-least
    wins / worst-to-first / head-to-head win totals -- all Kalshi-only
    (confirmed no Polymarket equivalents 2026-07-16), all built directly on
    top of season_sim's win-count/division-order tallies, no new simulation
    infrastructure needed beyond the Round-12 _DIVISIONS/worst_record
    extensions to season_sim.py itself.

    REAL BUG this fixes (found live 2026-07-20): all 6 Kalshi calls used
    to happen INSIDE an open session -- fixed by fetching all 6 up front,
    then writing under poller_lock.py::db_write_lock()."""
    division_wins_rows = kalshi_client.get_division_wins_markets()
    division_order_rows = kalshi_client.get_division_order_markets()
    div_least_wins_rows = kalshi_client.get_div_least_wins_markets()
    div_most_wins_rows = kalshi_client.get_div_most_wins_markets()
    worst_to_first_rows = kalshi_client.get_worst_to_first_markets()
    h2h_rows = kalshi_client.get_h2h_wins_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            for row in division_wins_rows:
                market_catalog.upsert_kalshi_division_wins_market(session, row)
            session.commit()
            log.info("kalshi division wins: %d markets ingested", len(division_wins_rows))

            for row in division_order_rows:
                market_catalog.upsert_kalshi_division_order_market(session, row)
            session.commit()
            log.info("kalshi division order: %d markets ingested", len(division_order_rows))

            for row in div_least_wins_rows:
                market_catalog.upsert_kalshi_div_extreme_market(session, row, "div_least_wins")
            session.commit()
            log.info("kalshi div least wins: %d markets ingested", len(div_least_wins_rows))

            for row in div_most_wins_rows:
                market_catalog.upsert_kalshi_div_extreme_market(session, row, "div_most_wins")
            session.commit()
            log.info("kalshi div most wins: %d markets ingested", len(div_most_wins_rows))

            for row in worst_to_first_rows:
                market_catalog.upsert_kalshi_worst_to_first_market(session, row)
            session.commit()
            log.info("kalshi worst-to-first: %d markets ingested", len(worst_to_first_rows))

            for row in h2h_rows:
                market_catalog.upsert_kalshi_h2h_market(session, row)
            session.commit()
            log.info("kalshi h2h wins: %d markets ingested", len(h2h_rows))
        finally:
            session.close()


LAST_REFRESH_KEY = "last_full_refresh_at"


def mark_refresh_complete():
    """Records when run_full_refresh last finished -- surfaced in the
    frontend TopBar (2026-07-16 UI polish) so it's visible at a glance
    whether the data is fresh, rather than the user having to guess."""
    with db_write_lock():
        session = SessionLocal()
        try:
            now = datetime.datetime.utcnow().isoformat()
            row = session.get(Setting, LAST_REFRESH_KEY)
            if row is None:
                session.add(Setting(key=LAST_REFRESH_KEY, value=now))
            else:
                row.value = now
            session.commit()
        finally:
            session.close()


def _resolve_duplicate_fixtures(session):
    """Copy a result onto an esports match whose duplicate twin already has one.

    Only logs on failure: this is an opportunistic top-up, and settlement must
    still run even if it cannot.
    """
    from app.db.models import Cs2Match, LolMatch, ValorantMatch
    from app.models.duplicate_fixtures import apply_twin_results

    for model in (LolMatch, Cs2Match, ValorantMatch):
        try:
            apply_twin_results(session, model)
        except Exception:
            log.exception("duplicate-fixture resolve failed for %s", model.__name__)
            session.rollback()


def settle_placed_bets():
    """Auto-grades pending placed bets. TWO paths, both run every cycle:
    (1) settle_finished_games -- reconstructs win/loss from each sport's own
    modelled result (works for Polymarket bets too, no Kalshi ticker needed);
    (2) settle_from_kalshi_resolution -- grades straight from the Kalshi market's
    OWN finalized result, the authoritative 100%-coverage path that also catches
    what (1) can't (lower-tier matches missing from result feeds, map_winner,
    futures). (2) does its own network fetch + locking, so it runs OUTSIDE the
    lock (1) takes."""
    from app.models.bet_settlement import settle_finished_games

    with db_write_lock():
        session = SessionLocal()
        try:
            # Before grading: an esports fixture stored TWICE under two platform
            # spellings gets the result on one row and keeps the bets on the
            # other, so (1) below sees an ungraded match and (2) has to carry it.
            # Copying the twin's result across first lets the normal path work.
            # See models/duplicate_fixtures.py -- non-destructive, nothing merged.
            _resolve_duplicate_fixtures(session)
            settle_finished_games(session)
        finally:
            session.close()

    try:
        from app.ingestion.market_resolution_settlement import (
            backfill_esports_winners_from_kalshi, reconcile_kalshi_market_status,
            settle_from_kalshi_resolution,
        )
        # Status FIRST, then settle. A market that resolved is otherwise frozen
        # at "active" forever -- the per-sport refreshes only fetch OPEN markets,
        # so nothing ever walks back over it -- and the routers filter on
        # status == "active", so it stays priceable and recommendable at a 0/1
        # price. Reconciling first also means the settle pass below sees an
        # accurate board.
        reconcile_kalshi_market_status()
        # Write the result back onto the esports MATCH row too, not just the
        # bet. CS2's own results scraper is Cloudflare-gated, so without this
        # the Elo model never learns from a live match even though the answer
        # is already in the Kalshi resolution we just fetched.
        backfill_esports_winners_from_kalshi()
        settle_from_kalshi_resolution()
    except Exception:
        log.exception("kalshi-resolution settlement failed")

    # The SAME freeze on the Polymarket side, and worse: the per-sport
    # Polymarket refreshes fetch closed=false, so a resolved market drops out of
    # the feed and stays "active" forever. Measured 2026-08-06 before this ran:
    # 32,963 of 41,560 markets we called active (79%) were already closed on
    # Polymarket -- against 21% on Kalshi. Separate try block so a Gamma outage
    # cannot take the Kalshi path down with it, or vice versa.
    try:
        from app.ingestion.polymarket_resolution import reconcile_polymarket_market_status

        reconcile_polymarket_market_status()
        # Status first, then settle -- same ordering and same reason as the
        # Kalshi block above. This is the authoritative settlement path for
        # Polymarket bets, which until now had none: the per-sport graders need
        # a scraped result and a working name join, and where they disagree with
        # Polymarket about a POLYMARKET bet, Polymarket is the venue that would
        # actually pay, so it wins by definition.
        from app.ingestion.polymarket_settlement import settle_from_polymarket_resolution

        settle_from_polymarket_resolution()
    except Exception:
        log.exception("polymarket status reconciliation failed")


def refresh_kalshi_win_totals():
    """Season win-total markets (per-team over/under ladder, per-team exact
    win count, league-wide 'any team hits N wins') -- see kalshi_client.py's
    WIN_TOTAL_SERIES_PREFIX comment. Season-long, no nfl_game_id, same
    pattern as refresh_kalshi_futures above but kept separate since these
    hit 64 series (32 teams x 2 series families) instead of a handful.

    REAL BUG this fixes (found live 2026-07-20): all 3 calls used to
    happen INSIDE an open session -- fixed by fetching all 3 up front."""
    win_total_rows = kalshi_client.get_win_total_markets()
    exact_win_rows = kalshi_client.get_exact_win_total_markets()
    wins_any_rows = kalshi_client.get_wins_any_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            for row in win_total_rows:
                market_catalog.upsert_kalshi_win_total_market(session, row)
            session.commit()
            log.info("kalshi win total: %d markets ingested", len(win_total_rows))

            for row in exact_win_rows:
                market_catalog.upsert_kalshi_exact_win_total_market(session, row)
            session.commit()
            log.info("kalshi exact win total: %d markets ingested", len(exact_win_rows))

            for row in wins_any_rows:
                market_catalog.upsert_kalshi_wins_any_market(session, row)
            session.commit()
            log.info("kalshi wins-any: %d markets ingested", len(wins_any_rows))
        finally:
            session.close()


def refresh_polymarket_futures():
    """REAL BUG this fixes (found live 2026-07-20, partial before -- `rows`
    was already fetched pre-session, but `undefeated_row`/`qb_rows` weren't):
    all 3 calls now fetched up front."""
    rows = polymarket_client.get_futures_markets()
    undefeated_row = polymarket_client.get_undefeated_market()
    qb_rows = polymarket_client.get_week1_qb_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            matched = 0
            for row in rows:
                if market_catalog.upsert_polymarket_futures_market(session, row) is not None:
                    matched += 1
            session.commit()
            log.info("polymarket futures: %d/%d markets ingested (name match)", matched, len(rows))

            if undefeated_row is not None:
                market_catalog.upsert_polymarket_undefeated_market(session, undefeated_row)
                session.commit()
                log.info("polymarket undefeated-season market ingested")

            qb_matched = 0
            for row in qb_rows:
                if market_catalog.upsert_polymarket_week1_qb_market(session, row) is not None:
                    qb_matched += 1
            session.commit()
            log.info("polymarket week1-qb: %d/%d markets ingested (name match)", qb_matched, len(qb_rows))
        finally:
            session.close()


def refresh_kalshi_moneyline():
    rows = kalshi_client.get_moneyline_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            game_index = _load_game_index(session)

            matched = 0
            unmatched = 0
            for row in rows:
                game_id = match_kalshi_moneyline_event(row["event_ticker"], game_index)
                if game_id:
                    matched += 1
                else:
                    unmatched += 1
                market_catalog.upsert_kalshi_moneyline_market(session, row, game_id)
            session.commit()
            log.info("kalshi moneyline: %d markets ingested (%d matched, %d unmatched)", len(rows), matched, unmatched)
        finally:
            session.close()


def refresh_polymarket_moneyline():
    events = polymarket_client.get_open_nfl_events()
    game_like = [e for e in events if polymarket_client.is_game_market(e)]
    with db_write_lock():
        session = SessionLocal()
        try:
            game_index = _load_game_index(session)

            matched = 0
            unmatched = 0
            markets_written = 0
            for event in game_like:
                slug = event.get("slug", "")
                parsed = parse_polymarket_slug(slug)
                if not parsed:
                    unmatched += 1
                    continue
                game_id = match_polymarket_event(slug, game_index)
                if game_id:
                    matched += 1
                else:
                    unmatched += 1
                created = market_catalog.upsert_polymarket_moneyline_event(
                    session, event, game_id, parsed["away"], parsed["home"]
                )
                markets_written += len(created)
            session.commit()
            log.info(
                "polymarket moneyline: %d game-like events (%d matched, %d unmatched), %d markets written",
                len(game_like),
                matched,
                unmatched,
                markets_written,
            )
        finally:
            session.close()


def refresh_kalshi_spread_total():
    """Per-game spread/total -- see kalshi_client.py::get_spread_markets/
    get_total_markets. Still 0 open events this far before the season
    (confirmed 2026-07-15); ingestion is built and ready for the moment
    Kalshi lists them closer to game week.

    REAL BUG this fixes (found live 2026-07-20): 7 separate Kalshi calls
    used to happen INSIDE an open session -- fixed by fetching all 7 up
    front. game_index itself is a DB READ (not network), so it's fine to
    keep it inside the locked write session."""
    spread_rows = kalshi_client.get_spread_markets()
    total_rows = kalshi_client.get_total_markets()
    team_total_rows = kalshi_client.get_team_total_markets()
    half_rows = {half: (kalshi_client.get_half_spread_markets(half), kalshi_client.get_half_total_markets(half)) for half in (1, 2)}
    half_winner_rows = {half: kalshi_client.get_half_winner_markets(half) for half in (1, 2)}

    with db_write_lock():
        session = SessionLocal()
        try:
            game_index = _load_game_index(session)

            matched = 0
            for row in spread_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                if game_id:
                    matched += 1
                market_catalog.upsert_kalshi_spread_market(session, row, game_id)
            session.commit()
            log.info("kalshi spread: %d markets ingested (%d matched)", len(spread_rows), matched)

            matched = 0
            for row in total_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                if game_id:
                    matched += 1
                market_catalog.upsert_kalshi_total_market(session, row, game_id)
            session.commit()
            log.info("kalshi total: %d markets ingested (%d matched)", len(total_rows), matched)

            matched = 0
            for row in team_total_rows:
                game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                if game_id:
                    matched += 1
                market_catalog.upsert_kalshi_team_total_market(session, row, game_id)
            session.commit()
            log.info("kalshi team total: %d markets ingested (%d matched)", len(team_total_rows), matched)

            for half, (half_spread_rows, half_total_rows) in half_rows.items():
                matched = 0
                for row in half_spread_rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    if game_id:
                        matched += 1
                    market_catalog.upsert_kalshi_half_spread_market(session, row, game_id, f"spread_{half}h")
                session.commit()
                log.info("kalshi spread %dH: %d markets ingested (%d matched)", half, len(half_spread_rows), matched)

                matched = 0
                for row in half_total_rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    if game_id:
                        matched += 1
                    market_catalog.upsert_kalshi_half_total_market(session, row, game_id, f"total_{half}h")
                session.commit()
                log.info("kalshi total %dH: %d markets ingested (%d matched)", half, len(half_total_rows), matched)

            for half, winner_rows in half_winner_rows.items():
                matched = 0
                for row in winner_rows:
                    game_id = match_kalshi_event_ticker(row["event_ticker"], game_index)
                    if game_id:
                        matched += 1
                    market_catalog.upsert_kalshi_half_winner_market(session, row, game_id, f"winner_{half}h")
                session.commit()
                log.info("kalshi winner %dH: %d markets ingested (%d matched)", half, len(winner_rows), matched)
        finally:
            session.close()


def refresh_polymarket_spread_total():
    """Per-game spread/total -- see
    polymarket_client.py::get_spread_total_markets. Polymarket bundles
    these into the SAME per-game event as moneyline, so no NFL games are
    open yet here either (moneyline's own preseason games ARE open, but
    Polymarket hasn't bundled spread/total onto those events yet as of
    2026-07-15 -- confirmed no "Spread"/"O/U" groupItemTitle markets found
    on any currently-tracked preseason event)."""
    events = polymarket_client.get_open_nfl_events()
    game_like = [e for e in events if polymarket_client.is_game_market(e)]
    spread_rows, total_rows = polymarket_client.get_spread_total_markets(game_like_events=game_like)

    with db_write_lock():
        session = SessionLocal()
        try:
            game_index = _load_game_index(session)
            slug_to_game_id = {}
            for event in game_like:
                slug = event.get("slug", "")
                game_id = match_polymarket_event(slug, game_index)
                if game_id:
                    slug_to_game_id[slug] = game_id

            matched = 0
            for row in spread_rows:
                game_id = slug_to_game_id.get(row["event_slug"])
                if game_id:
                    matched += 1
                market_catalog.upsert_polymarket_spread_market(session, row, game_id)
            session.commit()
            log.info("polymarket spread: %d rows ingested (%d matched)", len(spread_rows), matched)

            matched = 0
            for row in total_rows:
                game_id = slug_to_game_id.get(row["event_slug"])
                if game_id:
                    matched += 1
                market_catalog.upsert_polymarket_total_market(session, row, game_id)
            session.commit()
            log.info("polymarket total: %d rows ingested (%d matched)", len(total_rows), matched)
        finally:
            session.close()


def refresh_news_adjustments():
    """Free (ESPN injuries + nflverse rest/QB/roof/coach data + nflverse depth
    charts + Open-Meteo weather, no paid API) -- runs on every poll cycle
    since there's no cost concern, unlike an LLM call.

    REAL BUG this fixes (found live 2026-07-20, worst offender in this app --
    per-tracked-game depth-chart calls, PLUS a lazily-fetched per-season
    standings call, all used to happen INSIDE one open session): restructured
    into 4 phases, same shape as poller_nba.py::refresh_nba_news_adjustments's
    own fix -- (1) every network call with no real DB dependency runs first;
    (2) a short READ-ONLY session (no write lock -- WAL-mode reads don't
    contend with writes) loads the schedule index and every tracked game's
    own real data (including get_previous_coach, the one DB-dependent input
    the adjustment calc needs) into plain dicts; (3) the season-scoped
    depth-chart/standings fetches -- now genuinely once-per-season instead of
    the ORIGINAL's per-game starters_by_team/qb_backup_by_team re-fetch, a
    real efficiency win alongside the lock fix -- run with no session open;
    (4) a single write-locked session does every remaining DB write with
    zero network I/O left inside it. See poller_lock.py's own docstring for
    the full real-bug story this class of fix addresses across every sport's
    poller."""
    injuries_by_team = espn_client.fetch_all_injuries()
    qb_career_stats = get_qb_career_stats()  # cached 24h; rebuilt from local PBP parquet, no network call
    epa_ratings = get_current_epa_ratings()  # cached 24h; rebuilt from local PBP parquet, no network call
    rush_career_stats = get_rushing_career_stats()  # cached 24h; rebuilt from local PBP parquet, no network call
    recv_career_stats = get_receiving_career_stats()  # cached 24h; rebuilt from local PBP parquet, no network call

    # Phase 2: short read-only session for the schedule index + every
    # tracked, not-yet-played, non-PRE game's own real data (captured as
    # plain dicts so nothing ORM-bound outlives this session), including
    # get_previous_coach (the one DB-dependent input the adjustment calc
    # needs, computed here rather than in the write session).
    read_session = SessionLocal()
    try:
        reg_rows = read_session.query(NflGame).filter(NflGame.game_type == "REG").all()
        opponent_index = nfl_data.build_opponent_index(
            [
                {
                    "game_type": g.game_type,
                    "season": g.season,
                    "week": g.week,
                    "home_team": g.home_team,
                    "away_team": g.away_team,
                    "home_score": g.home_score,
                    "away_score": g.away_score,
                }
                for g in reg_rows
            ]
        )

        tracked_game_ids = {
            row[0] for row in read_session.query(Market.nfl_game_id).filter(Market.nfl_game_id.isnot(None)).distinct()
        }
        tracked_games = []
        for game_id in tracked_game_ids:
            game = read_session.get(NflGame, game_id)
            if game is None or game.home_score is not None or game.game_type == "PRE":
                continue
            tracked_games.append({
                "id": game.id, "away_team": game.away_team, "home_team": game.home_team,
                "away_qb_name": game.away_qb_name, "home_qb_name": game.home_qb_name,
                "away_rest": game.away_rest, "home_rest": game.home_rest, "roof": game.roof,
                "gameday": game.gameday, "gametime": game.gametime,
                "away_coach": game.away_coach, "home_coach": game.home_coach,
                "season": game.season, "week": game.week,
                "away_previous_coach": market_catalog.get_previous_coach(read_session, game.away_team, game.season, game.week),
                "home_previous_coach": market_catalog.get_previous_coach(read_session, game.home_team, game.season, game.week),
            })
    finally:
        read_session.close()

    # Phase 3: season-scoped network fetches, now genuinely once per real
    # season represented among tracked_games (not once per game, unlike
    # starters_by_team/qb_backup_by_team's ORIGINAL per-game re-fetch --
    # current_positions_by_season/previous_positions_by_season were already
    # this shape before, kept as-is here).
    seasons = {g["season"] for g in tracked_games}
    starters_by_team_by_season: dict[int, dict] = {}
    qb_backup_by_team_by_season: dict[int, dict] = {}
    current_positions_by_season: dict[int, dict] = {}
    previous_positions_by_season: dict[int, dict] = {}
    standings_by_season: dict[int, dict] = {}
    for season in seasons:
        try:
            starters_by_team_by_season[season] = depth_chart_client.get_current_starters(season)
            qb_backup_by_team_by_season[season] = depth_chart_client.get_qb_backup(season)
        except Exception:
            starters_by_team_by_season[season] = {}
            qb_backup_by_team_by_season[season] = {}
        try:
            current_positions_by_season[season] = depth_chart_client.get_skill_position_starters(season)
            previous_positions_by_season[season] = depth_chart_client.get_skill_position_starters(
                season - 1, before_date=f"{season}-02-15"
            )
        except Exception:
            current_positions_by_season[season] = {}
            previous_positions_by_season[season] = {}
        # Standings only fetched lazily, for seasons with at least one
        # tracked game in the final-stretch week -- same lazy gate as before.
        if any(g["season"] == season and g["week"] >= MIN_WEEK_FOR_ONE_SEED for g in tracked_games):
            try:
                standings_by_season[season] = espn_client.fetch_standings(season)
            except Exception:
                standings_by_season[season] = {}

    # Phase 4: single write-locked session, zero network I/O left inside it.
    with db_write_lock():
        session = SessionLocal()
        try:
            updated = 0
            for g in tracked_games:
                starters_by_team = starters_by_team_by_season.get(g["season"], {})
                qb_backup_by_team = qb_backup_by_team_by_season.get(g["season"], {})
                current_positions = current_positions_by_season.get(g["season"], {})
                previous_positions = previous_positions_by_season.get(g["season"], {})
                standings = standings_by_season.get(g["season"], {})

                home_last = opponent_index.get((g["season"], g["home_team"], g["week"] - 1)) or {}
                home_next = opponent_index.get((g["season"], g["home_team"], g["week"] + 1)) or {}
                away_last = opponent_index.get((g["season"], g["away_team"], g["week"] - 1)) or {}
                away_next = opponent_index.get((g["season"], g["away_team"], g["week"] + 1)) or {}
                away_two_back = opponent_index.get((g["season"], g["away_team"], g["week"] - 2))

                adjustment, home_scoring_penalty_pp, away_scoring_penalty_pp = compute_situational_adjustment(
                    away_team=g["away_team"],
                    home_team=g["home_team"],
                    away_qb_name=g["away_qb_name"],
                    home_qb_name=g["home_qb_name"],
                    away_injuries=injuries_by_team.get(g["away_team"], []),
                    home_injuries=injuries_by_team.get(g["home_team"], []),
                    away_rest=g["away_rest"],
                    home_rest=g["home_rest"],
                    roof=g["roof"],
                    game_date_iso=g["gameday"],
                    gametime=g["gametime"],
                    away_coach_current=g["away_coach"],
                    away_coach_previous=g["away_previous_coach"],
                    home_coach_current=g["home_coach"],
                    home_coach_previous=g["home_previous_coach"],
                    away_starters=starters_by_team.get(g["away_team"]),
                    home_starters=starters_by_team.get(g["home_team"]),
                    week=g["week"],
                    away_standing=standings.get(g["away_team"]),
                    home_standing=standings.get(g["home_team"]),
                    away_backup_qb=qb_backup_by_team.get(g["away_team"]),
                    home_backup_qb=qb_backup_by_team.get(g["home_team"]),
                    qb_career_stats=qb_career_stats,
                    home_last_opp=home_last.get("opponent"),
                    home_last_won=home_last.get("won"),
                    home_next_opp=home_next.get("opponent"),
                    away_last_opp=away_last.get("opponent"),
                    away_last_won=away_last.get("won"),
                    away_next_opp=away_next.get("opponent"),
                    away_last_week=away_last or None,
                    away_two_weeks_back=away_two_back,
                    epa_ratings=epa_ratings,
                    home_current_positions=current_positions.get(g["home_team"]),
                    away_current_positions=current_positions.get(g["away_team"]),
                    home_previous_positions=previous_positions.get(g["home_team"]),
                    away_previous_positions=previous_positions.get(g["away_team"]),
                    rush_career_stats=rush_career_stats,
                    recv_career_stats=recv_career_stats,
                )
                if adjustment is not None:
                    market_catalog.upsert_news_adjustment(
                        session, g["id"], adjustment, research_text="",
                        home_scoring_penalty_pp=home_scoring_penalty_pp,
                        away_scoring_penalty_pp=away_scoring_penalty_pp,
                    )
                updated += 1
            session.commit()
            log.info("news adjustments refreshed for %d tracked games", updated)
        finally:
            session.close()


def run_full_refresh():
    from app.models.baseline import elo_service
    from app.models import season_sim_service, scoring_ratings_service

    refresh_nfl_games()
    refresh_nfl_half_scores()
    refresh_preseason_games()
    elo_service.refresh_ratings()
    season_sim_service.refresh()
    scoring_ratings_service.refresh()
    refresh_kalshi_moneyline()
    refresh_polymarket_moneyline()
    refresh_kalshi_spread_total()
    refresh_polymarket_spread_total()
    refresh_kalshi_futures()
    refresh_kalshi_stage_of_elim()
    refresh_kalshi_playoff_seed_and_host()
    refresh_polymarket_futures()
    refresh_kalshi_win_totals()
    refresh_awards()
    refresh_stat_leaders()
    refresh_season_stat_ladders()
    refresh_division_extras()
    refresh_news_adjustments()
    settle_placed_bets()
    mark_refresh_complete()
