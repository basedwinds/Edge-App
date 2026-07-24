"""LoL polling/refresh entrypoints -- parallel to poller_valorant.py.

Covers map winner + total maps (both real, live Kalshi inventory, confirmed
2026-07-19) + tournament winner futures (real Kalshi series, current open-
market count not verified live at build time -- see kalshi_lol_client.py's
own docstring), plus the team Elo baseline. No Polymarket LoL client exists
-- checked live, real Polymarket LoL inventory is the LCK 2026 Season Winner
futures event only, no match-level market type at all.

REAL BUG fixed here (found live 2026-07-19, user report: "Matches tracked"
showing 0 despite real Kalshi market rows existing): same root cause and
fix as poller_valorant.py's own docstring -- market_catalog_lol.py::
find_or_create_upcoming_match existed but was never actually called from
refresh_kalshi_lol_markets(), which used a strict lookup-only helper
instead and gave up with a permanently-unmatched market whenever
Leaguepedia hadn't (yet) captured that match -- which, given how often
Leaguepedia's own rate limit makes refresh_lol_matches() fail outright (see
that function's own try/except below), meant LoL's "Matches tracked" could
realistically stay at 0 for very long stretches even with real market data
flowing in the whole time. Fixed by calling find_or_create_upcoming_match,
same "the live listing IS the schedule" fallback Tennis/Soccer already
rely on -- this makes LoL's live matching resilient to Leaguepedia's own
rate-limit outages instead of fully dependent on it succeeding.
"""
import datetime as dt
import logging

from app.clients import kalshi_lol_client, polymarket_lol_client
from app.db.database import SessionLocal
from app.ingestion import lol_data, market_catalog_lol
from app.ingestion.poller_lock import db_write_lock
from app.models.baseline import elo_service_lol

log = logging.getLogger("poller_lol")


def refresh_lol_ratings():
    elo_service_lol.refresh_ratings()


def _match_date_from_iso(occurrence_datetime: str | None) -> str | None:
    if not occurrence_datetime:
        return None
    try:
        return dt.datetime.fromisoformat(occurrence_datetime.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def refresh_lol_matches():
    """Leaguepedia's MatchSchedule Cargo table, queried fresh each cycle --
    see lol_data.py's own docstring on the real rate-limiting this endpoint
    applies (exactly ONE cargoquery call per refresh, on purpose).

    REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    connection across slow network I/O" anti-pattern this app's other
    pollers all had -- see poller_lock.py's own docstring for the full
    story): the fetch used to happen INSIDE an open SessionLocal(). For
    LoL specifically this was the worst version of it, since a rate-
    limited Leaguepedia retry sequence can hold that idle-but-checked-out
    pooled connection for 100+ real seconds -- fixed by fetching first,
    then opening a session only for the quick write, now taking
    poller_lock.py::db_write_lock() around just that write instead of the
    OLD whole-function serialized() wrapping at the call site (removed
    2026-07-20 -- see that module's own docstring)."""
    try:
        rows = lol_data.fetch_matches()
    except Exception:
        log.exception("leaguepedia match refresh failed (rate-limited or transient) -- skipping this cycle")
        return
    with db_write_lock():
        session = SessionLocal()
        try:
            count = 0
            for row in rows:
                market_catalog_lol.upsert_leaguepedia_match(session, row)
                count += 1
            session.commit()
            log.info("refreshed %d leaguepedia matches", count)
        except Exception:
            log.exception("leaguepedia match write failed (transient) -- skipping this cycle")
        finally:
            session.close()


def refresh_kalshi_lol_markets():
    """REAL BUG this fixes (found live 2026-07-20): all 3 of this
    function's own Kalshi calls used to happen INSIDE an open
    SessionLocal(), interleaved with real DB reads/writes
    (find_or_create_upcoming_match) -- fixed by fetching all 3 up front
    (map/total-maps/tournament-winner rows), then doing every DB-dependent
    step afterward under poller_lock.py::db_write_lock()."""
    map_rows = kalshi_lol_client.get_map_winner_markets()
    # REAL COVERAGE GAP this closes (found live 2026-07-20, via
    # catalog_scan.py's newly-added esports coverage -- user-reported:
    # "keep pushing esports... covering everything all markets") -- see
    # kalshi_lol_client.py's own real-bug note: KXLOLGAME is a real
    # whole-match/series winner ticker this app never queried at all.
    series_winner_rows = kalshi_lol_client.get_series_winner_markets()
    total_maps_rows = kalshi_lol_client.get_total_maps_markets()
    tournament_winner_rows = kalshi_lol_client.get_tournament_winner_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            # find_or_create_upcoming_match matches onto a real Leaguepedia-
            # sourced row when one exists, or creates a live fallback row
            # from Kalshi's OWN team names when it doesn't (see module
            # docstring's real-bug note) -- the real safety net given how
            # often refresh_lol_matches() itself fails outright to
            # Leaguepedia's rate limit.
            teams_by_code: dict[str, set[str]] = {}
            max_map_by_code: dict[str, int] = {}
            date_by_code: dict[str, str | None] = {}
            occurrence_by_code: dict[str, str | None] = {}
            for row in map_rows:
                teams_by_code.setdefault(row["match_code"], set()).add(row["team_name"])
                max_map_by_code[row["match_code"]] = max(max_map_by_code.get(row["match_code"], 0), row["map_number"])
                date_by_code.setdefault(row["match_code"], _match_date_from_iso(row.get("occurrence_datetime")))
                occurrence_by_code.setdefault(row["match_code"], row.get("occurrence_datetime"))

            match_id_by_code: dict[str, int | None] = {}
            for code, teams in teams_by_code.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_lol.find_or_create_upcoming_match(
                        session, team_a, team_b, match_date=date_by_code.get(code)
                    )
                    match_id_by_code[code] = match.id if match else None
                    # REAL BUG this fixes (user-reported 2026-07-20: esports
                    # recommended bets missing a real match start time) --
                    # see poller_valorant.py's own version of this comment.
                    # occurrence_datetime was already being fetched above
                    # (for date_by_code) and then thrown away -- only the
                    # date survived onto the match record.
                    occurrence = occurrence_by_code.get(code)
                    if match is not None and match.winner is None and occurrence:
                        match.estimated_start_time = occurrence
                else:
                    match_id_by_code[code] = None

            matched = sum(1 for v in match_id_by_code.values() if v is not None)
            for row in map_rows:
                market_catalog_lol.upsert_kalshi_lol_map_winner_market(
                    session, row, match_id_by_code.get(row["match_code"])
                )
            # REAL BUG fixed here (found live 2026-07-20): max_map_by_code
            # was already computed above but never actually passed to a
            # backfill_best_of call -- see market_catalog_lol.py::
            # backfill_best_of's own docstring for the full real-bug story.
            for code, match_id in match_id_by_code.items():
                if match_id is not None:
                    market_catalog_lol.backfill_best_of(session, match_id, max_map_by_code[code])

            # KXLOLGAME resolves independently by its own event_ticker (one
            # event per match, no map_code to join on) -- same pattern as
            # poller_valorant.py's own new series_winner resolution block.
            teams_by_series_event: dict[str, set[str]] = {}
            occurrence_by_series_event: dict[str, str | None] = {}
            for row in series_winner_rows:
                teams_by_series_event.setdefault(row["event_ticker"], set()).add(row["team_name"])
                occurrence_by_series_event.setdefault(row["event_ticker"], row.get("occurrence_datetime"))

            match_id_by_series_event: dict[str, int | None] = {}
            for event_ticker, teams in teams_by_series_event.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_lol.find_or_create_upcoming_match(session, team_a, team_b)
                    match_id_by_series_event[event_ticker] = match.id if match else None
                    occurrence = occurrence_by_series_event.get(event_ticker)
                    if match is not None and match.winner is None and occurrence:
                        match.estimated_start_time = occurrence
                else:
                    match_id_by_series_event[event_ticker] = None

            series_matched = sum(1 for v in match_id_by_series_event.values() if v is not None)
            for row in series_winner_rows:
                market_catalog_lol.upsert_kalshi_lol_series_winner_market(
                    session, row, match_id_by_series_event.get(row["event_ticker"])
                )

            for row in total_maps_rows:
                match = market_catalog_lol.find_or_create_upcoming_match(session, row["team_a"], row["team_b"])
                occurrence = row.get("occurrence_datetime")
                if match is not None and match.winner is None and occurrence:
                    match.estimated_start_time = occurrence
                market_catalog_lol.upsert_kalshi_lol_total_maps_market(
                    session, row, match.id if match else None
                )

            for row in tournament_winner_rows:
                market_catalog_lol.upsert_kalshi_lol_tournament_winner_market(session, row)

            session.commit()
            log.info(
                "kalshi lol: %d/%d map matches matched, %d/%d series matches matched",
                matched, len(match_id_by_code), series_matched, len(match_id_by_series_event),
            )
        finally:
            session.close()


def refresh_polymarket_lol_markets():
    """LoL's Polymarket side (2026-07-24): series/game winner + games total +
    handicap + season-winner futures. Every market type for a real match is
    bundled under ONE Polymarket event (event_slug) -- resolve the Match Winner's
    two team names to a LolMatch once per slug, reuse for every sibling market,
    same pattern as poller_valorant.refresh_polymarket_valorant_markets."""
    winner_rows = polymarket_lol_client.get_series_winner_markets()
    map_rows = polymarket_lol_client.get_map_winner_markets()
    total_rows = polymarket_lol_client.get_total_maps_markets()
    handicap_rows = polymarket_lol_client.get_map_handicap_markets()
    futures_rows = polymarket_lol_client.get_futures_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            teams_by_slug: dict[str, set[str]] = {}
            event_by_slug: dict[str, str] = {}
            start_by_slug: dict[str, str | None] = {}
            for row in winner_rows:
                teams_by_slug.setdefault(row["event_slug"], set()).add(row["team_name"])
                event_by_slug.setdefault(row["event_slug"], row.get("event_title", ""))
                start_by_slug.setdefault(row["event_slug"], row.get("estimated_start_time"))

            match_id_by_slug: dict[str, int | None] = {}
            for slug, teams in teams_by_slug.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_lol.find_or_create_upcoming_match(session, team_a, team_b, event_name=event_by_slug.get(slug))
                    match_id_by_slug[slug] = match.id if match else None
                    start = start_by_slug.get(slug)
                    if match is not None and match.winner is None and start:
                        match.estimated_start_time = start
                else:
                    match_id_by_slug[slug] = None

            matched = sum(1 for v in match_id_by_slug.values() if v is not None)
            for row in winner_rows:
                market_catalog_lol.upsert_polymarket_lol_series_winner_row(session, row, match_id_by_slug.get(row["event_slug"]))
            for row in map_rows:
                market_catalog_lol.upsert_polymarket_lol_map_winner_row(session, row, match_id_by_slug.get(row["event_slug"]))
            for row in total_rows:
                market_catalog_lol.upsert_polymarket_lol_total_row(session, row, match_id_by_slug.get(row["event_slug"]))
            for row in handicap_rows:
                market_catalog_lol.upsert_polymarket_lol_handicap_row(session, row, match_id_by_slug.get(row["event_slug"]))
            for row in futures_rows:
                market_catalog_lol.upsert_polymarket_lol_futures_row(session, row)

            session.commit()
            log.info("polymarket lol: %d/%d matches matched", matched, len(match_id_by_slug))
        finally:
            session.close()


def run_full_refresh_lol():
    # REAL BUG this ordering fixes (found live 2026-07-20, user report:
    # "not seeing any model %" for LoL): refresh_lol_matches() is the one
    # step that can burn 100+ real seconds retrying against Leaguepedia's
    # rate limit (4 attempts, 20/30/45/68s backoff) -- while this whole
    # function runs, it holds this app's shared cross-sport poller lock
    # (see poller_lock.py), so putting the fragile network call FIRST meant
    # a single bad cycle could leave refresh_lol_ratings() never reached at
    # all and hold the shared lock long enough to starve other sports'
    # refreshes. refresh_lol_ratings() is pure local computation, so it runs
    # first and LoL's model % stays fresh even when the live crawl fails.
    # (Roster-change scrape removed 2026-07-23 -- badge retired for esports,
    # no accuracy penalty found, so no reason to hit rate-limited Leaguepedia
    # for it. See poller_cs2.py's note + scripts/calibrate_cs2_roster_window.py.)
    refresh_lol_ratings()
    refresh_lol_matches()
    refresh_kalshi_lol_markets()
    refresh_polymarket_lol_markets()
