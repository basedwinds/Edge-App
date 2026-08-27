"""Soccer polling/refresh entrypoints -- parallel to poller_tennis.py, same
"no external schedule source, the live listing IS the schedule" pattern (see
market_catalog_soccer.py's docstring), but grouped by 3 rows per event
(home/draw/away), not 2 like every moneyline market elsewhere in this app."""
import datetime
import logging
import time

from app.clients import espn_soccer_client, kalshi_soccer_client, polymarket_soccer_client, transfermarkt_client
from app.db.database import SessionLocal
from app.db.models import Market, SoccerMatch
from app.ingestion import market_catalog_soccer
from app.ingestion.poller_lock import db_write_lock
from app.ingestion.market_matcher_soccer import canonical_team_key, kalshi_match_suffix, team_names_match
from app.ingestion.start_times import should_update_start
from app.models import playoff_sim_service_mls
from app.models.baseline import elo_service_soccer
from app.models.news_adjustment.injury_rules_soccer import compute_injury_adjustment
from app.models.news_adjustment.motivation_rules_soccer import compute_motivation_adjustment
from app.models.news_adjustment.schema import merge_adjustments

log = logging.getLogger("poller_soccer")

# How far AHEAD a fixture may be and still have its kickoff corrected from ESPN.
# Not a settlement horizon -- a display/staking one: a wrong kickoff is wrong on
# the screen the moment the market is priced, which is days before it settles.
# 14 days covers 410 of the 414 upcoming fixtures that currently carry live
# markets; the handful beyond that are season-long futures with no kickoff.
KICKOFF_HORIZON = datetime.timedelta(days=14)


def refresh_soccer_ratings():
    elo_service_soccer.refresh_ratings()


def _real_match_date(match) -> datetime.date | None:
    """The date the match is actually PLAYED.

    `match_date` on a live-tracked row is the date the listing was scraped,
    which can sit days away from kickoff (measured 2026-08-06: up to 7).
    `estimated_start_time` carries the real kickoff, so it wins whenever it
    parses; match_date is only the fallback for rows that predate it."""
    est = getattr(match, "estimated_start_time", None)
    if est:
        try:
            return datetime.datetime.fromisoformat(str(est).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            pass
    try:
        return datetime.date.fromisoformat(match.match_date)
    except (TypeError, ValueError):
        return None


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
            # Gate on the REAL kickoff, not the scrape date -- a row scraped
            # today for a match a week out would otherwise look "already
            # played" and be re-fetched pointlessly every cycle.
            match_date = _real_match_date(match)
            if match_date is None or match_date > today + KICKOFF_HORIZON:
                continue  # real match hasn't happened yet, nothing to backfill
            # TOMORROW is included deliberately, and this is not just slack.
            # The gate decides using the STORED start, which is the very thing
            # that may be wrong -- so a match stored a few hours late crosses
            # midnight UTC, reads as "tomorrow", and is skipped by the only
            # code that would have corrected it. Measured 2026-08-09:
            # Bragantino vs Corinthians kicked off 21:30Z but was stored
            # 00:30Z the next day, so it stayed uncorrected and recommendable
            # all evening while Bahia and Palmeiras -- same error, same
            # league, just not over the midnight boundary -- were fixed.
            # Widening by a day costs nothing: the ESPN window below already
            # runs to today + 1, and a not-yet-played match simply finds no
            # result to backfill.
            #
            # ONE DAY WAS NOT ENOUGH (user-reported 2026-08-10). Tigres vs
            # Vancouver kicks off 02:00Z on the 12th; Kalshi said 05:00Z, which
            # on the 10th is TWO days out, so the day of slack could not reach
            # it and the row displayed 1am instead of 10pm. The correction ran
            # only on matches about to be played, which is far too late to be
            # the thing a user reads when deciding a bet.
            #
            # So the horizon is now the BETTING horizon, not the settlement
            # one. It costs no extra requests -- the fetch below is one
            # scoreboard call per league over a date RANGE, so a wider range is
            # the same call. 399 of 414 upcoming fixtures carrying live markets
            # sat outside the old window.
            by_league_ids.setdefault(match.league, []).append(match.id)
    finally:
        read_session.close()

    # LOUD when a league has matches waiting on results but no ESPN slug to
    # fetch them with. THIS IS NOT HYPOTHETICAL -- it is the exact failure this
    # warning was added for (2026-08-08). espn_soccer_client.LEAGUE_CODES held
    # only the original six leagues, so every league added since (N1, E1, P1,
    # B1, T1, G1, the domestic cups, all three UEFA competitions) had its
    # results silently never REQUESTED. 134 live rows across 8 leagues could
    # never resolve, and a user's Liga Portugal bets sat pending on a match
    # that had finished hours earlier.
    #
    # What made it survive from the day Eredivisie was added is that it looked
    # like nothing: the backfill logged "0/14 updated", which reads as a
    # matching failure rather than a fetch that never happened. Same shape as
    # observation_logger's "returned markets but priced NONE" warning, and
    # added for the same reason -- a component that is merely UNWIRED produces
    # no error, so it has to announce itself.
    missing = {lg: len(ids) for lg, ids in by_league_ids.items()
               if lg not in espn_soccer_client.LEAGUE_CODES}
    if missing:
        log.error(
            "soccer results: %d league(s) have matches awaiting results but NO ESPN slug "
            "in espn_soccer_client.LEAGUE_CODES -- these can NEVER resolve and their bets "
            "will sit pending forever: %s",
            len(missing), ", ".join(f"{lg} ({n} matches)" for lg, n in sorted(missing.items())),
        )

    def _differs_materially(current: str | None, incoming: str) -> bool:
        """True when the two stamps are a genuinely different moment.

        A missing current value counts (there is nothing to compare, and having
        a real kickoff beats having none). One minute of tolerance absorbs
        formatting and rounding without hiding anything: the errors this exists
        for are hours wide, not seconds.
        """
        if not current:
            return True
        try:
            a = datetime.datetime.fromisoformat(current.replace("Z", "+00:00"))
            b = datetime.datetime.fromisoformat(incoming.replace("Z", "+00:00"))
        except ValueError:
            return current != incoming
        return abs((a - b).total_seconds()) > 60

    results_by_league: dict[str, list[dict]] = {}
    kickoffs_by_league: dict[str, list[dict]] = {}
    for league, match_ids in by_league_ids.items():
        # oldest date computed from the SAME read_session's rows above,
        # captured before that session closed -- recomputed here from
        # `unresolved` (still a live Python list, no DB access needed) to
        # avoid keeping the read_session's own ORM objects around across
        # the network fetch below.
        # Real kickoff dates, same reason as the gate above -- a window built
        # from scrape dates can start after a match actually happened.
        league_dates = [
            d for m in unresolved if m.id in match_ids
            and (d := _real_match_date(m)) is not None
        ]
        if not league_dates:
            continue
        # One day of slack each side: ESPN dates a late kickoff on the next
        # UTC day, which is the off-by-one the matcher below tolerates too.
        oldest = min(league_dates) - datetime.timedelta(days=1)
        # End of the window must track the gate above. If the fetch stopped at
        # today+1 while the gate admitted matches 14 days out, those matches
        # would be selected and then find no ESPN event to match -- a silent
        # no-op that looks exactly like "ESPN has no fixture for this".
        newest = max(max(league_dates), today) + datetime.timedelta(days=1)
        raw = espn_soccer_client.fetch_scoreboard(league, oldest, newest)
        results_by_league[league] = espn_soccer_client.parse_final_results(raw)
        # Same payload, no extra request -- see parse_kickoffs' docstring for the
        # live-match bug this closes.
        kickoffs_by_league[league] = espn_soccer_client.parse_kickoffs(raw)

    # Match rows to results, and fetch half-time goals, with NO session open --
    # `unresolved` is still a live Python list and both steps are pure functions
    # over it, so neither needs the DB. Keeping the per-event half-time requests
    # out here matters: they are one HTTP call each, and this file's own
    # docstring is about not holding a connection across slow network I/O.
    by_match_id: dict[int, dict] = {}
    kickoff_fixes: dict[int, str] = {}
    for league, match_ids in by_league_ids.items():
        results = results_by_league.get(league, [])
        kickoffs = kickoffs_by_league.get(league, [])
        wanted = set(match_ids)
        for match in unresolved:
            if match.id not in wanted:
                continue

            # CORRECT THE KICKOFF FROM ESPN before anything else. The platform's
            # own occurrence_datetime is not reliable: for Nuremberg vs Dresden
            # (2026-08-09) Kalshi said 14:30Z against a real 11:30Z kickoff, and
            # a live 1-0 match was offered as a bet because the start-time guard
            # was handed a time three hours in the future. See
            # espn_soccer_client.parse_kickoffs.
            #
            # Joined on TEAMS with the same one-day date tolerance the results
            # matcher uses, because ESPN dates a late kickoff on the next UTC day.
            kmatch = [
                k for k in kickoffs
                if team_names_match(k["home_team"], match.home_team)
                and team_names_match(k["away_team"], match.away_team)
            ]
            real_day = _real_match_date(match)
            if real_day is not None:
                kmatch = [
                    k for k in kmatch
                    if abs((datetime.date.fromisoformat(k["match_date"]) - real_day).days) <= 1
                ]
            if kmatch:
                espn_kick = kmatch[0]["kickoff"]
                # Compare INSTANTS, not strings. ESPN writes "2026-08-09T17:00Z"
                # where the platform wrote "2026-08-09T17:00:00Z" -- the same
                # moment in two formats. A string comparison treats every such
                # row as a correction, which would rewrite it and log a warning
                # on every single poll cycle, forever, burying the handful of
                # real three-hour errors this exists to surface.
                if espn_kick and _differs_materially(match.estimated_start_time, espn_kick):
                    # should_update_start still applies: it refuses only the move
                    # that orphans a played match (past -> future). Pulling a
                    # start time EARLIER, this fix's whole purpose, always passes.
                    if should_update_start(match.estimated_start_time, espn_kick,
                                           match.match_date):
                        kickoff_fixes[match.id] = espn_kick
            # Match on TEAMS first, then take the nearest date within a day --
            # do NOT require match_date to be equal.
            #
            # REAL BUG this fixes (2026-08-06): a live-tracked row's
            # `match_date` is the SCRAPE date, not the kickoff date -- e.g. San
            # Jose vs Los Angeles G stored match_date 2026-07-19 with
            # estimated_start_time 2026-07-26T02:30Z, a full week out. Measured
            # against real ESPN results for 54 team-matched MLS rows, ESPN-date
            # minus match_date was spread across 0,1,3,4,6,7 days with only 9
            # exact, while ESPN-date minus estimated_start_time was exact on 51
            # and off by one on 3 (a late kickoff crossing midnight UTC). So
            # estimated_start_time is the real date and the old equality test on
            # match_date was never going to fire.
            real = _real_match_date(match)
            cands = [
                r for r in results
                if team_names_match(r["home_team"], match.home_team)
                and team_names_match(r["away_team"], match.away_team)
            ]
            if real is not None:
                cands = [
                    r for r in cands
                    if abs((datetime.date.fromisoformat(r["match_date"]) - real).days) <= 1
                ]
                cands.sort(key=lambda r: abs((datetime.date.fromisoformat(r["match_date"]) - real).days))
            if not cands:
                continue
            found = dict(cands[0])
            # Half-time goals are not on the scoreboard (linescores is null on
            # every competitor there), so they cost one request per match. Only
            # matched rows are asked for, and a settled row is never revisited.
            found["halves"] = espn_soccer_client.fetch_half_time_goals(league, found.get("event_id"))
            by_match_id[match.id] = found

    with db_write_lock():
        session = SessionLocal()
        try:
            updated = ht = 0
            total = sum(len(v) for v in by_league_ids.values())

            # Kickoff corrections first, and separately from results: a LIVE
            # match has no result to write, and it is precisely the live ones
            # this needs to reach.
            corrected = 0
            for match_id, kick in kickoff_fixes.items():
                match = session.get(SoccerMatch, match_id)
                if match is None:
                    continue
                # Re-check against the row as it is NOW. The decision above was
                # made from a snapshot taken before the ESPN fetch, and the
                # market poller can write in that gap -- so without this, a row
                # already carrying the right time gets "corrected" to the value
                # it already has and logs a warning about it every cycle,
                # burying the real ones.
                if not _differs_materially(match.estimated_start_time, kick):
                    match.start_time_source = market_catalog_soccer.ESPN_START_SOURCE
                    continue
                log.warning(
                    "soccer kickoff corrected from ESPN: match %d %s vs %s -- platform said %s, "
                    "ESPN says %s. A start time later than reality is what lets a LIVE match be "
                    "recommended.",
                    match.id, match.home_team, match.away_team,
                    match.estimated_start_time, kick,
                )
                match.estimated_start_time = kick
                # Tag the provenance, or the next market poll simply writes the
                # platform's time straight back over this and the correction
                # never survives a cycle -- which is exactly what was happening.
                match.start_time_source = market_catalog_soccer.ESPN_START_SOURCE
                corrected += 1
            if corrected:
                log.info("soccer: %d kickoff time(s) corrected from ESPN", corrected)
            for match_id, found in by_match_id.items():
                match = session.get(SoccerMatch, match_id)
                if match is None:
                    continue
                match.home_goals_ft = found["home_goals_ft"]
                match.away_goals_ft = found["away_goals_ft"]
                match.result_ft = found["result_ft"]
                # Who scored first -- ftts cannot be graded from a final score.
                # May be None when ESPN carried no play-by-play; the grader
                # leaves those bets pending rather than guessing.
                if found.get("first_scorer") is not None:
                    match.first_scorer = found["first_scorer"]
                if found.get("halves") is not None:
                    match.home_goals_ht, match.away_goals_ht = found["halves"]
                    ht += 1
                updated += 1
            session.commit()
            log.info(
                "soccer results backfill: %d/%d unresolved-but-already-played matches updated (%d with half-time goals)",
                updated, total, ht,
            )
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


# Upserts per lock acquisition in refresh_kalshi_soccer_markets's second
# stage. ~3,600 rows a cycle, so 400 is roughly nine acquisitions -- short
# enough that another poller waits under ten seconds, long enough that lock
# churn is not itself the cost.
_KALSHI_UPSERT_BATCH = 400


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

    # SPLIT INTO TWO LOCKED STAGES, 2026-08-26. This whole write used to run
    # under ONE db_write_lock() -- and that lock is the app-wide, non-reentrant
    # one every poller and `serialized()` share. The soccer pass runs 467-517s
    # against a 300s interval, so it never stops, and while it held that lock
    # nothing else could write. `/soccer/markets` swinging 52s -> 150s between
    # cache-warm passes is that contention, not route cost.
    #
    # Stage 1 (match resolution) KEEPS one lock for the whole loop: it is a
    # read-then-create critical section, and interleaving another writer could
    # double-create a SoccerMatch.
    #
    # Stage 2 (market upserts) takes the lock PER BATCH and commits per batch.
    # Each upsert is an independent row keyed by its own ticker, and only this
    # poller writes Kalshi soccer markets, so releasing between batches cannot
    # corrupt anything. Same shape as the snapshot-prune fix, which deleted 3.9M
    # rows in ONE transaction under this lock and starved all nine pollers until
    # APScheduler skipped it.
    #
    # The tradeoff is atomicity: a crash mid-write now leaves some markets
    # updated and some not. That is fine here -- these are idempotent upserts and
    # the next cycle re-runs them 300s later -- and it is the same tradeoff the
    # prune made.
    #
    # PER-STAGE TIMING IS LOGGED because the estimate that motivated this was a
    # SUBTRACTION (315s step - 125s fetch measured separately), and Kalshi
    # rate-limit backoff makes the fetch half swing 174s -> 415s on its own. The
    # next pass says outright where the time actually goes.
    t_stage0 = time.time()
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
                # PASS THE KICKOFF DAY. Omitting it made find_or_create_upcoming_match
                # fall back to stamping the SCRAPE date, and that scrape date is then
                # read downstream as if it were the fixture date. /soccer/markets
                # treats "match_date < today" as proof a match has already been played
                # and drops the row -- so every Kalshi-sourced fixture went invisible
                # as soon as the date rolled over, no matter how far in the future its
                # actual kickoff was.
                #
                # It rolled over EARLIER than a local clock suggests, which is why this
                # looked intermittent rather than broken: the stamp is local
                # date.today() but the route compares against UTC, so from early
                # evening in a US timezone onward the two disagree by a day and the
                # whole Kalshi-only half of soccer blanks out. Measured live at 20:23
                # local on 2026-08-08 -- local said 08-08, UTC said 08-09, and all 174
                # Brazil / 88 Argentina / 49 Mexico / 36 Japan markets were dropped
                # with 0 of them actually decided. E0/F1/I1/N1 were hit too; they are
                # simply between seasons so nobody noticed.
                #
                # The cup path below already did this (date = start[:10]) and cups
                # priced fine all along, which is the clearest evidence this was the
                # missing argument and not a modelling problem.
                start_day = (first.get("estimated_start_time") or "")[:10] or None
                match = market_catalog_soccer.find_or_create_upcoming_match(
                    session, first["division"], first["home_team"], first["away_team"],
                    start_day,
                )
                market_catalog_soccer.update_match_estimated_start_time(
                    match, first.get("estimated_start_time"), source="kalshi")
                match_id = match.id if match else None
                match_id_by_event[event_ticker] = match_id
                suffix_key = kalshi_match_suffix(event_ticker)
                if suffix_key:
                    match_id_by_suffix_key[suffix_key] = match_id

            session.commit()
        finally:
            session.close()
    t_stage1 = time.time()

    matched = sum(1 for v in match_id_by_event.values() if v is not None)

    # One flat work list, so batching is over the TOTAL upsert count rather than
    # per market type -- the small types (2h_btts has 5 rows) would otherwise
    # each take and release the lock for almost nothing.
    work: list = []
    for row in rows:
        work.append((market_catalog_soccer.upsert_kalshi_soccer_moneyline_market,
                     row, match_id_by_event.get(row["event_ticker"])))

    # Spread/total live on their OWN series/event_ticker per match (see
    # kalshi_soccer_client.py's docstring), resolved via the SAME cross-series
    # join key moneyline's event_ticker maps to, reused directly here rather
    # than re-matching by team name a second time.
    def _by_suffix(row):
        sk = kalshi_match_suffix(row["event_ticker"])
        return match_id_by_suffix_key.get(sk) if sk else None

    for row in spread_rows:
        work.append((market_catalog_soccer.upsert_kalshi_soccer_spread_market, row, _by_suffix(row)))
    for row in total_rows:
        work.append((market_catalog_soccer.upsert_kalshi_soccer_total_market, row, _by_suffix(row)))
    for row in btts_rows:
        work.append((market_catalog_soccer.upsert_kalshi_soccer_btts_market, row, _by_suffix(row)))
    counts: dict[str, int] = {}
    for fetched_rows, upsert_fn, label in second_batch:
        for row in fetched_rows:
            work.append((upsert_fn, row, _by_suffix(row)))
        counts[label] = len(fetched_rows)

    # SPLIT WAIT FROM WORK. After the per-row flush was removed this stage was
    # still 663.6s for 3,752 rows -- 177ms each, which is not a plausible cost
    # for an indexed upsert plus one insert. Batching means this now WAITS for
    # the shared lock ten times while nine other pollers use it, so the wall
    # clock includes queueing that is not this poller's work at all. Reporting
    # one number for both is what made the previous estimate wrong twice; these
    # are measured separately so the next change targets the right thing.
    t_lock_wait = 0.0
    t_lock_work = 0.0
    for i in range(0, len(work), _KALSHI_UPSERT_BATCH):
        _w0 = time.time()
        with db_write_lock():
            _w1 = time.time()
            t_lock_wait += _w1 - _w0
            session = SessionLocal()
            try:
                for fn, row, mid in work[i:i + _KALSHI_UPSERT_BATCH]:
                    fn(session, row, mid)
                session.commit()
            finally:
                session.close()
            t_lock_work += time.time() - _w1
    t_stage2 = time.time()

    log.info(
        "kalshi soccer: %d/%d matches resolved, %d moneyline rows, %d spread rows, %d total rows, %d btts rows, "
        "%d 1H rows, %d 1H-spread, %d 1H-total, %d 1H-btts, %d 2H rows, %d 2H-spread, %d 2H-total, %d 2H-btts, "
        "%d ftts, %d correct-score, %d team-total "
        "[match-resolve %.1fs, %d upserts in %d batches %.1fs "
        "= %.1fs waiting for the lock + %.1fs holding it]",
        matched, len(by_event), len(rows), len(spread_rows), len(total_rows), len(btts_rows),
        counts["1h"], counts["1h_spread"], counts["1h_total"], counts["1h_btts"],
        counts["2h"], counts["2h_spread"], counts["2h_total"], counts["2h_btts"],
        counts["ftts"], counts["score"], counts["teamtotal"],
        t_stage1 - t_stage0, len(work),
        (len(work) + _KALSHI_UPSERT_BATCH - 1) // _KALSHI_UPSERT_BATCH,
        t_stage2 - t_stage1, t_lock_wait, t_lock_work,
    )


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
                market_catalog_soccer.update_match_estimated_start_time(
                    match, first.get("estimated_start_time"), source="polymarket")
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
    team_points_rows = kalshi_soccer_client.get_team_points_markets()
    mls_playoff_rows = kalshi_soccer_client.get_mls_playoff_markets()
    ligamx_rows = kalshi_soccer_client.get_ligamx_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_soccer.upsert_kalshi_soccer_league_winner_market(session, row)
            for row in relegation_rows:
                market_catalog_soccer.upsert_kalshi_soccer_relegation_market(session, row)
            for row in top_n_rows:
                market_catalog_soccer.upsert_kalshi_soccer_top_n_market(session, row)
            for row in team_points_rows:
                market_catalog_soccer.upsert_kalshi_soccer_team_points_market(session, row)
            for row in mls_playoff_rows:
                market_catalog_soccer.upsert_kalshi_mls_playoff_market(session, row)
            # Liga MX reuses the MLS-playoff upsert unchanged: identical row
            # shape (market_type on the row, torneo in group_label, no
            # soccer_match_id). A separate near-identical function would be two
            # places to fix.
            for row in ligamx_rows:
                market_catalog_soccer.upsert_kalshi_mls_playoff_market(session, row)
            session.commit()
            log.info(
                "kalshi soccer futures: %d league_winner rows across %d leagues, %d relegation rows across %d leagues, "
                "%d top-N rows (top_half/top4/top2, EPL only), %d team-points rows across %d leagues, "
                "%d MLS playoff rows, %d Liga MX rows",
                len(rows), len({r["division"] for r in rows}),
                len(relegation_rows), len({r["division"] for r in relegation_rows}),
                len(top_n_rows),
                len(team_points_rows), len({r["division"] for r in team_points_rows}),
                len(mls_playoff_rows), len(ligamx_rows),
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


def refresh_polymarket_soccer_futures():
    """Polymarket's side of league_winner (2026-08-12).

    Fetched by exact EVENT SLUG rather than by tag, so it cannot silently pull
    the wrong league -- see the client's LEAGUE_WINNER_EVENT_SLUGS note. Only
    leagues this app already rates are listed, because simulate_season is
    league-agnostic and prices a league the moment its rows and its Elo pool
    both exist; a league we cannot rate would just add permanent blanks.

    Kept as its OWN step rather than folded into refresh_kalshi_soccer_futures
    so a Polymarket outage cannot take Kalshi's futures down with it, matching
    the per-step isolation the step list below exists for.
    """
    rows = polymarket_soccer_client.get_league_winner_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_soccer.upsert_polymarket_soccer_league_winner_row(session, row)
            session.commit()
        finally:
            session.close()
    leagues = {r["division"] for r in rows}
    log.info("polymarket soccer futures: %d league_winner rows across %d leagues",
             len(rows), len(leagues))


def refresh_mls_playoff_sim():
    """Re-runs the MLS Cup bracket Monte Carlo. Ordered AFTER
    refresh_soccer_ratings in the full refresh below on purpose: it reads the
    MLS rating state, and on a cold process that state is empty until the
    ratings job has run -- a sim built on an unwarmed pool would see every team
    as unrated and refuse to price (or worse, price off nothing)."""
    playoff_sim_service_mls.refresh()


def run_full_refresh_soccer():
    # refresh_mls_playoff_sim is deliberately NOT here. It runs 10,000
    # simulations and ~10 live ESPN calls, and this function fires every 5
    # minutes -- see MIN_REFRESH_INTERVAL in playoff_sim_service_mls for the
    # incident that caused. It has its own slow scheduler job.
    #
    # EVERY STEP IS WRAPPED, and this is not defensive padding -- it is fixing a
    # real, observed, pre-existing failure. On 2026-08-08 the whole pass was
    # dying at refresh_kalshi_soccer_futures on a Kalshi 429 (rate limit) raised
    # out of get_open_events. One rate-limited futures fetch therefore silently
    # skipped EVERY later step in the pass, including refresh_soccer_results and
    # settle_soccer_placed_bets -- so settled bets were not being settled, for a
    # reason that had nothing to do with settlement and left no error anyone was
    # looking at. This predates the cup/UEFA work; those additions only made it
    # visible by being at the end of the queue (and, being ~90 more Kalshi
    # events per cycle, by adding to the rate pressure that triggers it).
    #
    # A transient upstream 429 in ONE market family must never be able to stop
    # the others, and above all must never stop settlement.
    steps = (
        ("ratings", refresh_soccer_ratings),
        ("kalshi markets", refresh_kalshi_soccer_markets),
        ("polymarket markets", refresh_polymarket_soccer_markets),
        ("kalshi futures", refresh_kalshi_soccer_futures),
        ("polymarket futures", refresh_polymarket_soccer_futures),
        # MLS playoff sim. It has its OWN 6-hourly scheduler job, and this does
        # not replace it -- this is a belt-and-braces warm so an empty cache
        # cannot silently unprice all 60 MLS futures rows.
        #
        # REAL BUG this fixes (2026-08-08): mls_cup_winner and
        # mls_conference_winner were serving 60/60 rows with model_prob=None.
        # The model was fine -- run by hand it completes 10,000 sims over 242
        # remaining fixtures in ~30s -- but its job had not fired in the live
        # worker, so the in-process cache was empty and the router had nothing
        # to read. Same shape as the cup rows earlier today: a working model,
        # starved by scheduling.
        #
        # SAFE AT THIS CADENCE BY CONSTRUCTION, which is the whole point of
        # playoff_sim_service_mls.MIN_REFRESH_INTERVAL: calling refresh() more
        # often than every 6 hours returns the cached result immediately without
        # simulating. That TTL was added precisely so a mistaken call site costs
        # nothing -- see its own comment about the incident where this ran every
        # 5 minutes and pinned a core.
        ("mls playoff sim", refresh_mls_playoff_sim),
    )
    timings: list[tuple[str, float]] = []
    for name, fn in steps:
        _t0 = time.monotonic()
        try:
            fn()
        except Exception:
            log.exception("soccer %s refresh failed -- continuing the rest of the pass", name)
        timings.append((name, time.monotonic() - _t0))
    # REAL BUG this fixes (2026-08-08, found within hours of building them).
    # Both of these were written, verified by hand, and then left out of this
    # function -- so their markets were ingested ONCE and never polled again.
    # The failure is not "no data": the rows sit in the DB, status active,
    # priced correctly by the router. But this router drops any market whose
    # newest MarketSnapshot is stale relative to the feed's own poll cadence
    # (that is how it tells a live market from a dead one), and nothing was
    # writing fresh snapshots. So all 191 cup rows silently disappeared from
    # /soccer/markets about 40 minutes after being verified working, with
    # every intermediate check -- market_type filter, active status, future
    # match dates, a direct ORM query -- passing. A poller that is never
    # scheduled looks exactly like a poller that is broken, one cycle later.
    #
    # WRAPPED, and the wrapping is the point. On the first scheduled run after
    # these were added, the log showed "kalshi cup markets refreshed" twice and
    # the UEFA line zero times -- meaning the pass reached cups and then never
    # got past UEFA, which also silently skipped refresh_soccer_results,
    # refresh_soccer_news_adjustments and settle_soccer_placed_bets for that
    # cycle. UEFA is by far the biggest fetch here (441 markets over ~90 events,
    # and Kalshi 429s are already visible in this app's logs), so it is the most
    # likely thing in this function to throw. A new, optional market family must
    # never be able to take settlement down with it.
    for name, fn in (("cup", refresh_kalshi_cup_markets), ("uefa", refresh_kalshi_uefa_markets),
                     ("conmebol", refresh_kalshi_conmebol_markets),
                     ("leagues cup", refresh_kalshi_leagues_cup_markets),
                     ("national", refresh_kalshi_national_markets)):
        _t0 = time.monotonic()
        try:
            fn()
        except Exception:
            log.exception("soccer %s market refresh failed -- continuing the rest of the pass", name)
        timings.append((name, time.monotonic() - _t0))
    for name, fn in (("results", refresh_soccer_results),
                     ("news adjustments", refresh_soccer_news_adjustments),
                     ("bet settlement", settle_soccer_placed_bets)):
        _t0 = time.monotonic()
        try:
            fn()
        except Exception:
            log.exception("soccer %s failed -- continuing the rest of the pass", name)
        timings.append((name, time.monotonic() - _t0))

    # PER-STEP TIMING, because this pass is currently slower than its own
    # 5-minute interval and apscheduler is reporting overlapping instances.
    # Without this line the only way to find out which step is eating the
    # budget is to guess. Logged as one sorted line per pass, worst first.
    total = sum(d for _n, d in timings)
    breakdown = "  ".join(f"{n}={d:.1f}s" for n, d in sorted(timings, key=lambda x: -x[1]))
    log.warning("soccer pass took %.1fs (interval is 300s) -- %s", total, breakdown)


def refresh_kalshi_cup_markets():
    """Domestic cup ties (Coppa Italia, DFB Pokal).

    Kept as its own entrypoint rather than folded into
    refresh_kalshi_soccer_markets, for a structural reason and not just tidiness:
    that function groups rows by event and requires exactly 3 per event to
    identify a match (home/draw/away), then joins the other market types onto it
    by an opaque per-division ticker suffix. Cups break both assumptions -- the
    ADVANCE series has 2 rows per event, TOTAL has 6, and the suffix scheme is
    per-competition rather than per-division. Threading that through would put
    new failure modes into the busiest poller in the app.

    Every fetch happens before the session opens, same discipline as every other
    poller here (see refresh_kalshi_soccer_markets' own docstring).
    """
    batches = [
        (kalshi_soccer_client.get_cup_moneyline_markets(),
         market_catalog_soccer.upsert_kalshi_cup_moneyline_market, "cup_moneyline"),
        (kalshi_soccer_client.get_cup_advance_markets(),
         market_catalog_soccer.upsert_kalshi_cup_advance_market, "cup_advance"),
        (kalshi_soccer_client.get_cup_total_markets(),
         market_catalog_soccer.upsert_kalshi_cup_total_market, "cup_total"),
        (kalshi_soccer_client.get_cup_spread_markets(),
         market_catalog_soccer.upsert_kalshi_cup_spread_market, "cup_spread"),
    ]

    counts: dict[str, int] = {}
    with db_write_lock():
        session = SessionLocal()
        try:
            # One SoccerMatch per real tie, shared by all three market types --
            # keyed on the fixture itself, not the event ticker, because the
            # three series use DIFFERENT event tickers for the same tie.
            match_id_by_tie: dict[tuple[str, str, str, str], int | None] = {}

            for rows, upsert, label in batches:
                n = 0
                for row in rows:
                    league = market_catalog_soccer.cup_league_code(row["competition"])
                    start = row.get("estimated_start_time") or ""
                    date = start[:10] or None
                    key = (league, canonical_team_key(row["home_team"]),
                           canonical_team_key(row["away_team"]), date or "")
                    if key not in match_id_by_tie:
                        match = market_catalog_soccer.find_or_create_upcoming_match(
                            session, league, row["home_team"], row["away_team"], date,
                            start_time=row.get("estimated_start_time"))
                        session.flush()
                        match_id_by_tie[key] = match.id if match is not None else None
                    upsert(session, row, match_id_by_tie[key])
                    n += 1
                counts[label] = n
            session.commit()
        finally:
            session.close()
    log.info("kalshi cup markets refreshed: %s", counts)
    return counts


def refresh_kalshi_uefa_markets():
    """UEFA club competitions. Same separate-entrypoint reasoning as
    refresh_kalshi_cup_markets, and the same fetch-before-session discipline.

    ADVANCE is not fetched at all -- UEFA knockout ties run over two legs, so
    that market cannot be priced from a single-match distribution (see
    models/uefa_match.py). Ingesting it would only produce rows this app must
    then refuse to price.
    """
    batches = [
        (kalshi_soccer_client.get_uefa_moneyline_markets(),
         market_catalog_soccer.upsert_kalshi_uefa_moneyline_market, "uefa_moneyline"),
        (kalshi_soccer_client.get_uefa_total_markets(),
         market_catalog_soccer.upsert_kalshi_uefa_total_market, "uefa_total"),
        (kalshi_soccer_client.get_uefa_spread_markets(),
         market_catalog_soccer.upsert_kalshi_uefa_spread_market, "uefa_spread"),
    ]
    counts: dict[str, int] = {}
    with db_write_lock():
        session = SessionLocal()
        try:
            match_id_by_tie: dict[tuple, int | None] = {}
            for rows, upsert, label in batches:
                n = 0
                for row in rows:
                    league = market_catalog_soccer.uefa_league_code(row["competition"])
                    date = (row.get("estimated_start_time") or "")[:10] or None
                    key = (league, canonical_team_key(row["home_team"]),
                           canonical_team_key(row["away_team"]), date or "")
                    if key not in match_id_by_tie:
                        match = market_catalog_soccer.find_or_create_upcoming_match(
                            session, league, row["home_team"], row["away_team"], date,
                            start_time=row.get("estimated_start_time"))
                        session.flush()
                        match_id_by_tie[key] = match.id if match is not None else None
                    upsert(session, row, match_id_by_tie[key])
                    n += 1
                counts[label] = n
            session.commit()
        finally:
            session.close()
    log.info("kalshi uefa markets refreshed: %s", counts)
    return counts


def refresh_kalshi_conmebol_markets():
    """Copa Libertadores + Copa Sudamericana.

    A separate entrypoint from the UEFA one on purpose, matching how the models
    are separated: conmebol_match.py has its own fitted offsets and its own
    baseline mu (BRA1-pinned), so these rows must never route through the UEFA
    handler.

    ADVANCE is not fetched, same rule and same reason as UEFA: CONMEBOL knockout
    rounds are two legs plus penalties, so KXCONMEBOLLIBADVANCE depends on an
    aggregate this app cannot compute from one match. 14 open advance markets
    stay uningested until the two-legged model lands.
    """
    batches = [
        (kalshi_soccer_client.get_conmebol_moneyline_markets(),
         market_catalog_soccer.upsert_kalshi_conmebol_moneyline_market, "conmebol_moneyline"),
        (kalshi_soccer_client.get_conmebol_total_markets(),
         market_catalog_soccer.upsert_kalshi_conmebol_total_market, "conmebol_total"),
        (kalshi_soccer_client.get_conmebol_spread_markets(),
         market_catalog_soccer.upsert_kalshi_conmebol_spread_market, "conmebol_spread"),
    ]
    counts: dict[str, int] = {}
    with db_write_lock():
        session = SessionLocal()
        try:
            match_id_by_tie: dict[tuple, int | None] = {}
            for rows, upsert, label in batches:
                n = 0
                for row in rows:
                    league = market_catalog_soccer.conmebol_league_code(row["competition"])
                    date = (row.get("estimated_start_time") or "")[:10] or None
                    key = (league, canonical_team_key(row["home_team"]),
                           canonical_team_key(row["away_team"]), date or "")
                    if key not in match_id_by_tie:
                        match = market_catalog_soccer.find_or_create_upcoming_match(
                            session, league, row["home_team"], row["away_team"], date,
                            start_time=row.get("estimated_start_time"))
                        session.flush()
                        match_id_by_tie[key] = match.id if match is not None else None
                    upsert(session, row, match_id_by_tie[key])
                    n += 1
                counts[label] = n
            session.commit()
        finally:
            session.close()
    log.info("kalshi conmebol markets refreshed: %s", counts)
    return counts


def refresh_kalshi_national_markets():
    """National-team match markets (currently the ASEAN Championship).

    Stored under league "INTL" -- the same code the ratings use -- so
    resolve_league and pricing line up without a second mapping. ADVANCE is not
    fetched: it is decided after extra time and penalties, which a single-match
    goal distribution cannot answer.
    """
    batches = [
        (kalshi_soccer_client.get_national_moneyline_markets(),
         market_catalog_soccer.upsert_kalshi_national_moneyline_market, "national_moneyline"),
        (kalshi_soccer_client.get_national_total_markets(),
         market_catalog_soccer.upsert_kalshi_national_total_market, "national_total"),
        (kalshi_soccer_client.get_national_spread_markets(),
         market_catalog_soccer.upsert_kalshi_national_spread_market, "national_spread"),
        (kalshi_soccer_client.get_national_btts_markets(),
         market_catalog_soccer.upsert_kalshi_national_btts_market, "national_btts"),
    ]
    counts: dict[str, int] = {}
    league = market_catalog_soccer.NATIONAL_LEAGUE_CODE
    with db_write_lock():
        session = SessionLocal()
        try:
            match_id_by_tie: dict[tuple, int | None] = {}
            for rows, upsert, label in batches:
                n = 0
                for row in rows:
                    date = (row.get("estimated_start_time") or "")[:10] or None
                    key = (league, canonical_team_key(row["home_team"]),
                           canonical_team_key(row["away_team"]), date or "")
                    if key not in match_id_by_tie:
                        match = market_catalog_soccer.find_or_create_upcoming_match(
                            session, league, row["home_team"], row["away_team"], date,
                            start_time=row.get("estimated_start_time"))
                        session.flush()
                        match_id_by_tie[key] = match.id if match is not None else None
                    upsert(session, row, match_id_by_tie[key])
                    n += 1
                counts[label] = n
            session.commit()
        finally:
            session.close()
    log.info("kalshi national markets refreshed: %s", counts)
    return counts


def refresh_kalshi_leagues_cup_markets():
    """Leagues Cup (MLS vs Liga MX). Separate entrypoint and separate league
    code from UEFA, because the two use DIFFERENT fitted offsets and different
    venue terms -- see models/leagues_cup_match.py.

    All four listed series are ingested (moneyline / total / spread / BTTS);
    every one of them is a single-match question the goal distribution can
    answer. There is no ADVANCE series to worry about, unlike the domestic cups.
    """
    batches = [
        (kalshi_soccer_client.get_leagues_cup_moneyline_markets(),
         market_catalog_soccer.upsert_kalshi_leagues_cup_moneyline_market, "leagues_cup_moneyline"),
        (kalshi_soccer_client.get_leagues_cup_total_markets(),
         market_catalog_soccer.upsert_kalshi_leagues_cup_total_market, "leagues_cup_total"),
        (kalshi_soccer_client.get_leagues_cup_spread_markets(),
         market_catalog_soccer.upsert_kalshi_leagues_cup_spread_market, "leagues_cup_spread"),
        (kalshi_soccer_client.get_leagues_cup_advance_markets(),
         market_catalog_soccer.upsert_kalshi_leagues_cup_advance_market, "leagues_cup_advance"),
        (kalshi_soccer_client.get_leagues_cup_btts_markets(),
         market_catalog_soccer.upsert_kalshi_leagues_cup_btts_market, "leagues_cup_btts"),
    ]
    counts: dict[str, int] = {}
    league = market_catalog_soccer.LEAGUES_CUP_LEAGUE_CODE
    with db_write_lock():
        session = SessionLocal()
        try:
            match_id_by_tie: dict[tuple, int | None] = {}
            for rows, upsert, label in batches:
                n = 0
                for row in rows:
                    # Kickoff day, NOT a scrape stamp -- see the comment on the
                    # league path in refresh_kalshi_soccer_markets.
                    date = (row.get("estimated_start_time") or "")[:10] or None
                    key = (league, canonical_team_key(row["home_team"]),
                           canonical_team_key(row["away_team"]), date or "")
                    if key not in match_id_by_tie:
                        match = market_catalog_soccer.find_or_create_upcoming_match(
                            session, league, row["home_team"], row["away_team"], date,
                            start_time=row.get("estimated_start_time"))
                        session.flush()
                        match_id_by_tie[key] = match.id if match is not None else None
                    upsert(session, row, match_id_by_tie[key])
                    n += 1
                counts[label] = n
            session.commit()
        finally:
            session.close()
    log.info("kalshi leagues cup markets refreshed: %s", counts)
    return counts
