"""Soccer polling/refresh entrypoints -- parallel to poller_tennis.py, same
"no external schedule source, the live listing IS the schedule" pattern (see
market_catalog_soccer.py's docstring), but grouped by 3 rows per event
(home/draw/away), not 2 like every moneyline market elsewhere in this app."""
import datetime
import logging

from app.clients import espn_soccer_client, kalshi_soccer_client, polymarket_soccer_client, transfermarkt_client
from app.db.database import SessionLocal
from app.db.models import Market, SoccerMatch
from app.ingestion import market_catalog_soccer
from app.ingestion.poller_lock import db_write_lock
from app.ingestion.market_matcher_soccer import canonical_team_key, kalshi_match_suffix, team_names_match
from app.models.baseline import elo_service_soccer
from app.models.news_adjustment.injury_rules_soccer import compute_injury_adjustment
from app.models.news_adjustment.motivation_rules_soccer import compute_motivation_adjustment
from app.models.news_adjustment.schema import merge_adjustments

log = logging.getLogger("poller_soccer")


def refresh_soccer_ratings():
    elo_service_soccer.refresh_ratings()


def refresh_soccer_results():
    """Backfills REAL final scores onto live-tracked SoccerMatch rows once
    their real match has actually been played.

    REAL GAP this fixes: unlike NFL/NBA/MLB (whose own fetch_games() re-
    fetches and overwrites home_score/away_score for every game, including
    past ones, so a game naturally gets its final score written in by the
    next poll after it ends), nothing in this app's Soccer pipeline ever
    wrote a live-tracked match's real outcome back onto its SoccerMatch row
    -- find_or_create_upcoming_match only ever CREATES rows while
    result_ft IS NULL, never updates one after the fact. Caught here (not
    live-observed as a symptom) while wiring up CLV/auto-settlement for
    Soccer: both depend on result_ft actually getting set for a real match
    once it's over, which nothing did.

    Uses ESPN's real scoreboard endpoint (espn_soccer_client.py, confirmed
    live 2026-07-19 to generalize across all 6 leagues, not just MLS) --
    queries a real date window per league (from the oldest still-unresolved
    tracked match's match_date through today) and matches each real
    STATUS_FULL_TIME event onto a tracked row via team_names_match (same
    fuzzy cross-source matcher as every other Soccer join in this app)."""
    # REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    # connection across slow network I/O" anti-pattern this app's other
    # pollers all had -- see poller_lock.py's own docstring): the ESPN
    # scoreboard fetch per league used to happen INSIDE the same open
    # session as both the initial read AND the final write. This function
    # genuinely needs a DB READ before it knows what to fetch (which
    # leagues/date windows have real unresolved matches), so it can't
    # collapse to a single fetch-then-write shape like most other pollers
    # -- instead: a short read-only session first (no write lock needed,
    # WAL-mode reads don't contend with writes), then the real network
    # fetches with NO session open, then a final short session under
    # poller_lock.py::db_write_lock() for the actual writes.
    read_session = SessionLocal()
    try:
        unresolved = (
            read_session.query(SoccerMatch)
            .filter(SoccerMatch.result_ft.is_(None))
            .all()
        )
        today = datetime.date.today()
        by_league_ids: dict[str, list[int]] = {}
        for match in unresolved:
            try:
                match_date = datetime.date.fromisoformat(match.match_date)
            except (TypeError, ValueError):
                continue
            if match_date > today:
                continue  # real match hasn't happened yet, nothing to backfill
            by_league_ids.setdefault(match.league, []).append(match.id)
    finally:
        read_session.close()

    results_by_league: dict[str, list[dict]] = {}
    for league, match_ids in by_league_ids.items():
        # oldest date computed from the SAME read_session's rows above,
        # captured before that session closed -- recomputed here from
        # `unresolved` (still a live Python list, no DB access needed) to
        # avoid keeping the read_session's own ORM objects around across
        # the network fetch below.
        league_dates = [
            datetime.date.fromisoformat(m.match_date) for m in unresolved
            if m.id in match_ids
        ]
        oldest = min(league_dates)
        raw = espn_soccer_client.fetch_scoreboard(league, oldest, today)
        results_by_league[league] = espn_soccer_client.parse_final_results(raw)

    with db_write_lock():
        session = SessionLocal()
        try:
            updated = 0
            total = 0
            for league, match_ids in by_league_ids.items():
                results = results_by_league.get(league, [])
                total += len(match_ids)
                for match_id in match_ids:
                    match = session.get(SoccerMatch, match_id)
                    if match is None:
                        continue
                    found = next(
                        (
                            r for r in results
                            if r["match_date"] == match.match_date
                            and team_names_match(r["home_team"], match.home_team)
                            and team_names_match(r["away_team"], match.away_team)
                        ),
                        None,
                    )
                    if found is None:
                        continue
                    match.home_goals_ft = found["home_goals_ft"]
                    match.away_goals_ft = found["away_goals_ft"]
                    match.result_ft = found["result_ft"]
                    updated += 1
            session.commit()
            log.info("soccer results backfill: %d/%d unresolved-but-already-played matches updated", updated, total)
        finally:
            session.close()


def refresh_soccer_news_adjustments():
    """Free (Transfermarkt whole-league injury lists + ESPN whole-league
    standings, no paid API) -- matches each tracked, not-yet-played match's
    real home/away club names against that league's real injury list via
    team_names_match (the same fuzzy matcher every other cross-source
    Soccer join in this app already uses -- Transfermarkt's own club-name
    spelling is a THIRD source, on top of football-data.co.uk/ESPN/Kalshi/
    Polymarket, so exact-string matching would silently miss real players
    the same way this app's own earlier spread-market bug did, see
    soccer_markets.py's docstring on that one) and looks each team up in
    that league's real standings table (canonical_team_key-matched, same as
    every other ESPN-vs-football-data.co.uk name join in this app) for the
    motivation signal (see motivation_rules_soccer.py). The two independent
    signals are combined via merge_adjustments (schema.py) before caching --
    same "sum independent rule modules, don't overwrite" pattern
    situational_nba.py already established for NBA's own injury+coach+
    schedule+motivation bundle."""
    injuries_by_league = transfermarkt_client.fetch_all_injuries()
    standings_by_league = {
        league: espn_soccer_client.fetch_standings(league) for league in espn_soccer_client.STANDINGS_LEAGUE_CODES
    }
    # canonical_team_key-keyed copy of each league's standings so a lookup
    # by match.home_team/away_team (football-data.co.uk spelling) reliably
    # finds ESPN's own spelling of the same real club.
    canonical_standings_by_league = {
        league: {canonical_team_key(name): standing for name, standing in table.items()}
        for league, table in standings_by_league.items()
    }
    with db_write_lock():
        session = SessionLocal()
        try:
            tracked_match_ids = {
                row[0] for row in session.query(Market.soccer_match_id)
                .filter(Market.soccer_match_id.isnot(None), Market.sport == "soccer").distinct()
            }
            updated = 0
            for match_id in tracked_match_ids:
                match = session.get(SoccerMatch, match_id)
                if match is None or match.result_ft is not None:
                    continue

                league_injuries = injuries_by_league.get(match.league, [])
                home_injuries = [inj for inj in league_injuries if team_names_match(inj["club"], match.home_team)]
                away_injuries = [inj for inj in league_injuries if team_names_match(inj["club"], match.away_team)]
                injury_adjustment = compute_injury_adjustment(home_injuries, away_injuries)

                league_table = canonical_standings_by_league.get(match.league, {})
                home_standing = league_table.get(canonical_team_key(match.home_team))
                away_standing = league_table.get(canonical_team_key(match.away_team))
                motivation_adjustment = compute_motivation_adjustment(
                    match.home_team, match.away_team, home_standing, away_standing, match.league, len(league_table),
                )

                adjustment = merge_adjustments([injury_adjustment, motivation_adjustment])
                if adjustment is not None:
                    market_catalog_soccer.upsert_soccer_news_adjustment(session, match_id, adjustment)
                    updated += 1
            log.info(
                "soccer news adjustments: %d/%d tracked matches updated, %d league injury lists fetched, %d league standings fetched",
                updated, len(tracked_match_ids), sum(1 for v in injuries_by_league.values() if v),
                sum(1 for v in standings_by_league.values() if v),
            )
        finally:
            session.close()


def refresh_kalshi_soccer_markets():
    """REAL BUG this fixes (found live 2026-07-20, worst offender in this
    file -- ~17 separate Kalshi calls all used to happen INSIDE one open
    session): every fetch below now happens up front, with no session
    open, before any DB read/write -- see poller_lock.py's own docstring
    for the full real-bug story this class of fix addresses across every
    sport's poller."""
    rows = kalshi_soccer_client.get_moneyline_markets()
    spread_rows = kalshi_soccer_client.get_spread_markets()
    total_rows = kalshi_soccer_client.get_total_markets()
    btts_rows = kalshi_soccer_client.get_btts_markets()
    # Second batch (added 2026-07-19) -- fetched up front too, same as
    # everything else here; each entry is (rows, upsert_fn, label).
    second_batch = [
        (kalshi_soccer_client.get_first_half_markets(), market_catalog_soccer.upsert_kalshi_soccer_first_half_market, "1h"),
        (kalshi_soccer_client.get_first_half_spread_markets(), market_catalog_soccer.upsert_kalshi_soccer_first_half_spread_market, "1h_spread"),
        (kalshi_soccer_client.get_first_half_total_markets(), market_catalog_soccer.upsert_kalshi_soccer_first_half_total_market, "1h_total"),
        (kalshi_soccer_client.get_first_half_btts_markets(), market_catalog_soccer.upsert_kalshi_soccer_first_half_btts_market, "1h_btts"),
        (kalshi_soccer_client.get_second_half_markets(), market_catalog_soccer.upsert_kalshi_soccer_second_half_market, "2h"),
        (kalshi_soccer_client.get_second_half_spread_markets(), market_catalog_soccer.upsert_kalshi_soccer_second_half_spread_market, "2h_spread"),
        (kalshi_soccer_client.get_second_half_total_markets(), market_catalog_soccer.upsert_kalshi_soccer_second_half_total_market, "2h_total"),
        (kalshi_soccer_client.get_second_half_btts_markets(), market_catalog_soccer.upsert_kalshi_soccer_second_half_btts_market, "2h_btts"),
        (kalshi_soccer_client.get_ftts_markets(), market_catalog_soccer.upsert_kalshi_soccer_ftts_market, "ftts"),
        (kalshi_soccer_client.get_correct_score_markets(), market_catalog_soccer.upsert_kalshi_soccer_correct_score_market, "score"),
        (kalshi_soccer_client.get_team_total_markets(), market_catalog_soccer.upsert_kalshi_soccer_team_total_market, "teamtotal"),
    ]

    with db_write_lock():
        session = SessionLocal()
        try:
            by_event: dict[str, list[dict]] = {}
            for row in rows:
                by_event.setdefault(row["event_ticker"], []).append(row)

            match_id_by_event: dict[str, int | None] = {}
            # Keyed by kalshi_match_suffix's own (division, date+team-code)
            # return value -- an opaque cross-series join key, same role
            # as market_matcher_tennis.py's kalshi_match_suffix.
            match_id_by_suffix_key: dict[tuple[str, str], int | None] = {}
            for event_ticker, event_rows in by_event.items():
                if len(event_rows) != 3:
                    match_id_by_event[event_ticker] = None
                    continue
                first = event_rows[0]
                match = market_catalog_soccer.find_or_create_upcoming_match(
                    session, first["division"], first["home_team"], first["away_team"],
                )
                market_catalog_soccer.update_match_estimated_start_time(match, first.get("estimated_start_time"))
                match_id = match.id if match else None
                match_id_by_event[event_ticker] = match_id
                suffix_key = kalshi_match_suffix(event_ticker)
                if suffix_key:
                    match_id_by_suffix_key[suffix_key] = match_id

            matched = sum(1 for v in match_id_by_event.values() if v is not None)
            for row in rows:
                market_catalog_soccer.upsert_kalshi_soccer_moneyline_market(
                    session, row, match_id_by_event.get(row["event_ticker"])
                )

            # Spread/total live on their OWN series/event_ticker per match
            # (see kalshi_soccer_client.py's docstring), resolved via the
            # SAME cross-series join key moneyline's event_ticker maps to,
            # reused directly here rather than re-matching by team name a
            # second time.
            for row in spread_rows:
                suffix_key = kalshi_match_suffix(row["event_ticker"])
                market_catalog_soccer.upsert_kalshi_soccer_spread_market(
                    session, row, match_id_by_suffix_key.get(suffix_key) if suffix_key else None
                )
            for row in total_rows:
                suffix_key = kalshi_match_suffix(row["event_ticker"])
                market_catalog_soccer.upsert_kalshi_soccer_total_market(
                    session, row, match_id_by_suffix_key.get(suffix_key) if suffix_key else None
                )
            for row in btts_rows:
                suffix_key = kalshi_match_suffix(row["event_ticker"])
                market_catalog_soccer.upsert_kalshi_soccer_btts_market(
                    session, row, match_id_by_suffix_key.get(suffix_key) if suffix_key else None
                )

            counts: dict[str, int] = {}
            for fetched_rows, upsert_fn, label in second_batch:
                for row in fetched_rows:
                    suffix_key = kalshi_match_suffix(row["event_ticker"])
                    upsert_fn(session, row, match_id_by_suffix_key.get(suffix_key) if suffix_key else None)
                counts[label] = len(fetched_rows)

            session.commit()
            log.info(
                "kalshi soccer: %d/%d matches resolved, %d moneyline rows, %d spread rows, %d total rows, %d btts rows, "
                "%d 1H rows, %d 1H-spread, %d 1H-total, %d 1H-btts, %d 2H rows, %d 2H-spread, %d 2H-total, %d 2H-btts, "
                "%d ftts, %d correct-score, %d team-total",
                matched, len(by_event), len(rows), len(spread_rows), len(total_rows), len(btts_rows),
                counts["1h"], counts["1h_spread"], counts["1h_total"], counts["1h_btts"],
                counts["2h"], counts["2h_spread"], counts["2h_total"], counts["2h_btts"],
                counts["ftts"], counts["score"], counts["teamtotal"],
            )
        finally:
            session.close()


def refresh_polymarket_soccer_markets():
    """See refresh_kalshi_soccer_markets's own docstring -- same real fix,
    Polymarket's own version (~15 separate calls, same "fetch everything
    up front, no session held across any of it" restructuring)."""
    rows = polymarket_soccer_client.get_moneyline_markets()
    spread_rows = polymarket_soccer_client.get_spread_markets()
    total_rows = polymarket_soccer_client.get_total_markets()
    # Second batch (added 2026-07-19) -- fetched up front too; each entry
    # is (rows, upsert_fn, label).
    second_batch = [
        (polymarket_soccer_client.get_btts_markets(), market_catalog_soccer.upsert_polymarket_soccer_btts_row, "btts"),
        (polymarket_soccer_client.get_team_total_markets(), market_catalog_soccer.upsert_polymarket_soccer_team_total_row, "teamtotal"),
        (polymarket_soccer_client.get_first_half_markets(), market_catalog_soccer.upsert_polymarket_soccer_first_half_row, "1h"),
        (polymarket_soccer_client.get_second_half_markets(), market_catalog_soccer.upsert_polymarket_soccer_second_half_row, "2h"),
        (polymarket_soccer_client.get_first_half_total_markets(), market_catalog_soccer.upsert_polymarket_soccer_first_half_total_row, "1h_total"),
        (polymarket_soccer_client.get_second_half_total_markets(), market_catalog_soccer.upsert_polymarket_soccer_second_half_total_row, "2h_total"),
        (polymarket_soccer_client.get_first_half_team_total_markets(), market_catalog_soccer.upsert_polymarket_soccer_first_half_team_total_row, "1h_teamtotal"),
        (polymarket_soccer_client.get_second_half_team_total_markets(), market_catalog_soccer.upsert_polymarket_soccer_second_half_team_total_row, "2h_teamtotal"),
        (polymarket_soccer_client.get_first_half_btts_markets(), market_catalog_soccer.upsert_polymarket_soccer_first_half_btts_row, "1h_btts"),
        (polymarket_soccer_client.get_second_half_btts_markets(), market_catalog_soccer.upsert_polymarket_soccer_second_half_btts_row, "2h_btts"),
        (polymarket_soccer_client.get_ftts_markets(), market_catalog_soccer.upsert_polymarket_soccer_ftts_row, "ftts"),
        (polymarket_soccer_client.get_correct_score_markets(), market_catalog_soccer.upsert_polymarket_soccer_correct_score_row, "score"),
    ]

    with db_write_lock():
        session = SessionLocal()
        try:
            by_event: dict[str, list[dict]] = {}
            for row in rows:
                by_event.setdefault(row["event_slug"], []).append(row)

            match_id_by_event: dict[str, int | None] = {}
            for event_slug, event_rows in by_event.items():
                if len(event_rows) != 3:
                    match_id_by_event[event_slug] = None
                    continue
                first = event_rows[0]
                match = market_catalog_soccer.find_or_create_upcoming_match(
                    session, first["division"], first["home_team"], first["away_team"], first.get("match_date"),
                )
                market_catalog_soccer.update_match_estimated_start_time(match, first.get("estimated_start_time"))
                match_id_by_event[event_slug] = match.id if match else None

            matched = sum(1 for v in match_id_by_event.values() if v is not None)
            for row in rows:
                market_catalog_soccer.upsert_polymarket_soccer_moneyline_row(
                    session, row, match_id_by_event.get(row["event_slug"])
                )

            # Spread/total live on a SEPARATE sibling event ("-more-markets",
            # see polymarket_soccer_client.py's docstring) -- no shared
            # event_slug with moneyline to join on, so each row is resolved
            # the same way find_or_create_upcoming_match resolves any live
            # listing: by team name. Idempotent against the SAME match
            # moneyline already created/found above (team names match),
            # never creates a duplicate.
            for row in spread_rows:
                match = market_catalog_soccer.find_or_create_upcoming_match(
                    session, row["division"], row["home_team"], row["away_team"],
                )
                market_catalog_soccer.upsert_polymarket_soccer_spread_row(session, row, match.id if match else None)
            for row in total_rows:
                match = market_catalog_soccer.find_or_create_upcoming_match(
                    session, row["division"], row["home_team"], row["away_team"],
                )
                market_catalog_soccer.upsert_polymarket_soccer_total_row(session, row, match.id if match else None)

            # Second batch -- same team-name-matching resolution as
            # spread/total above (BTTS/team-total/half-variants live in
            # the same "-more-markets" bundle; First Half/Second Half
            # Winner/FTTS/Correct Score each live on their OWN dedicated
            # sibling event, but resolve onto the SAME real match the
            # same way).
            counts: dict[str, int] = {}
            for fetched_rows, upsert_fn, label in second_batch:
                for row in fetched_rows:
                    match = market_catalog_soccer.find_or_create_upcoming_match(
                        session, row["division"], row["home_team"], row["away_team"],
                    )
                    upsert_fn(session, row, match.id if match else None)
                counts[label] = len(fetched_rows)

            session.commit()
            log.info(
                "polymarket soccer: %d/%d matches resolved, %d moneyline rows, %d spread rows, %d total rows, "
                "%d btts, %d team-total, %d 1H, %d 2H, %d 1H-total, %d 2H-total, %d 1H-teamtotal, %d 2H-teamtotal, "
                "%d 1H-btts, %d 2H-btts, %d ftts, %d correct-score",
                matched, len(by_event), len(rows), len(spread_rows), len(total_rows),
                counts["btts"], counts["teamtotal"], counts["1h"], counts["2h"], counts["1h_total"], counts["2h_total"],
                counts["1h_teamtotal"], counts["2h_teamtotal"], counts["1h_btts"], counts["2h_btts"],
                counts["ftts"], counts["score"],
            )
        finally:
            session.close()


def refresh_kalshi_soccer_futures():
    """League-winner futures -- just ingests real live prices, no match
    resolution needed (not tied to a single SoccerMatch). The season Monte
    Carlo itself runs at request time in the router (soccer_markets.py),
    same "compute the model number fresh per request, don't cache a stale
    simulation" pattern as Tennis's own bracket sim."""
    rows = kalshi_soccer_client.get_league_winner_markets()
    relegation_rows = kalshi_soccer_client.get_relegation_markets()
    top_n_rows = kalshi_soccer_client.get_top_n_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_soccer.upsert_kalshi_soccer_league_winner_market(session, row)
            for row in relegation_rows:
                market_catalog_soccer.upsert_kalshi_soccer_relegation_market(session, row)
            for row in top_n_rows:
                market_catalog_soccer.upsert_kalshi_soccer_top_n_market(session, row)
            session.commit()
            log.info(
                "kalshi soccer futures: %d league_winner rows across %d leagues, %d relegation rows across %d leagues, "
                "%d top-N rows (top_half/top4/top2, EPL only)",
                len(rows), len({r["division"] for r in rows}),
                len(relegation_rows), len({r["division"] for r in relegation_rows}),
                len(top_n_rows),
            )
        finally:
            session.close()


def settle_soccer_placed_bets():
    """Auto-grades placed bets tied to a real Soccer match once its final
    score lands -- same pattern as poller.py::settle_placed_bets (NFL).
    settle_finished_games itself scans ALL sports' pending bets (filtered by
    market_type), so calling it here (right after refresh_soccer_results
    above has a chance to backfill this cycle's real results) is what lets
    a Soccer bet settle the same cycle its match ends, not the next one."""
    from app.models.bet_settlement import settle_finished_games

    with db_write_lock():
        session = SessionLocal()
        try:
            settle_finished_games(session)
        finally:
            session.close()


def run_full_refresh_soccer():
    refresh_soccer_ratings()
    refresh_kalshi_soccer_markets()
    refresh_polymarket_soccer_markets()
    refresh_kalshi_soccer_futures()
    refresh_soccer_results()
    refresh_soccer_news_adjustments()
    settle_soccer_placed_bets()
