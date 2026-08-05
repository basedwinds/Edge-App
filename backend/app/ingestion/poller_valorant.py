"""Valorant polling/refresh entrypoints -- parallel to poller_mma.py.

Covers map winner (Kalshi + Polymarket), series/match winner + map handicap +
series total (Polymarket only -- see kalshi_valorant_client.py's own
docstring on why Kalshi coverage here is map-level only) + tournament winner
futures (both platforms), plus the team Elo baseline (elo_valorant.py/
elo_service_valorant.py).

REAL BUG fixed here (found live 2026-07-19, user report: "Matches tracked"
showing 0 despite hundreds of real Kalshi/Polymarket market rows existing):
market_catalog_valorant.py::find_or_create_upcoming_match was written to
mirror Tennis/Soccer's own "the live listing IS the schedule" fallback --
create a live ValorantMatch row from a platform's OWN team names when
vlr.gg's scrape hasn't captured that match yet (or its own refresh cycle
simply hasn't run/succeeded recently) -- but it was never actually CALLED
from either refresh function below. Both used a strict "look up an existing
match, give up with None if not found" helper instead, so a market with no
matching vlr.gg row just stayed permanently unmatched (no match_label, not
counted in "Matches tracked") even though the market data itself was real
and present the whole time. Fixed by calling find_or_create_upcoming_match
instead of the old lookup-only helper.
"""
import datetime as dt
import logging

from app.clients import kalshi_valorant_client, polymarket_valorant_client
from app.db.database import SessionLocal
from app.ingestion import market_catalog_valorant, valorant_data
from app.ingestion.start_times import apply_start
from app.ingestion.poller_lock import db_write_lock
from app.models.baseline import elo_service_valorant

log = logging.getLogger("poller_valorant")


def refresh_valorant_ratings():
    elo_service_valorant.refresh_ratings()


def _match_date_from_iso(occurrence_datetime: str | None) -> str | None:
    if not occurrence_datetime:
        return None
    try:
        return dt.datetime.fromisoformat(occurrence_datetime.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def refresh_valorant_matches():
    """vlr.gg's own live schedule (upcoming) + first page of recent results
    -- NOT a full historical crawl (see elo_service_valorant.py's docstring
    on why a real historical vlr.gg cache doesn't exist yet).

    REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    connection across slow network I/O" anti-pattern this app's other
    pollers all had -- see poller_lock.py's own docstring): the fetch used
    to happen INSIDE an open SessionLocal()."""
    rows = valorant_data.fetch_upcoming_matches() + valorant_data.fetch_recent_results()
    with db_write_lock():
        session = SessionLocal()
        try:
            count = 0
            for row in rows:
                market_catalog_valorant.upsert_vlr_match(session, row)
                count += 1
            session.commit()
            log.info("refreshed %d vlr.gg matches", count)
        finally:
            session.close()


def refresh_kalshi_valorant_markets():
    """REAL BUG this fixes (found live 2026-07-20): both Kalshi calls used
    to happen INSIDE an open session, interleaved with real DB reads/
    writes -- fixed by fetching both up front, then doing every
    DB-dependent step under poller_lock.py::db_write_lock()."""
    map_rows = kalshi_valorant_client.get_map_winner_markets()
    # REAL COVERAGE GAP this closes (found live 2026-07-20, via
    # catalog_scan.py's newly-added esports coverage -- user-reported:
    # "keep pushing esports... covering everything all markets") -- see
    # kalshi_valorant_client.py's own real-bug note: KXVALORANTGAME is a
    # real whole-match/series winner ticker this app never queried at all.
    series_winner_rows = kalshi_valorant_client.get_series_winner_markets()
    tournament_winner_rows = kalshi_valorant_client.get_tournament_winner_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            # Every KXVALORANTMAP map event for the same real match shares
            # the same match_code prefix (see kalshi_valorant_client.py) --
            # resolve each match_code to a valorant_match_id ONCE via the
            # team names seen across its own map events, then reuse for
            # every row + the max-map-number best_of backfill, same
            # "resolve once, reuse" pattern as poller_mma.py's fight_suffix
            # resolution.
            teams_by_code: dict[str, set[str]] = {}
            max_map_by_code: dict[str, int] = {}
            date_by_code: dict[str, str | None] = {}
            event_by_code: dict[str, str] = {}
            occurrence_by_code: dict[str, str | None] = {}
            for row in map_rows:
                teams_by_code.setdefault(row["match_code"], set()).add(row["team_name"])
                max_map_by_code[row["match_code"]] = max(max_map_by_code.get(row["match_code"], 0), row["map_number"])
                date_by_code.setdefault(row["match_code"], _match_date_from_iso(row.get("occurrence_datetime")))
                event_by_code.setdefault(row["match_code"], row.get("event_title", ""))
                occurrence_by_code.setdefault(row["match_code"], row.get("occurrence_datetime"))

            # find_or_create_upcoming_match matches onto a real vlr.gg-
            # sourced row when one exists, or creates a live fallback row
            # from Kalshi's OWN team names when it doesn't (see module
            # docstring's real-bug note) -- same "the live listing IS the
            # schedule" fallback Tennis/Soccer already rely on, just
            # actually wired in here now.
            match_id_by_code: dict[str, int | None] = {}
            for code, teams in teams_by_code.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_valorant.find_or_create_upcoming_match(
                        session, team_a, team_b, match_date=date_by_code.get(code), event_name=event_by_code.get(code)
                    )
                    match_id_by_code[code] = match.id if match else None
                    # REAL BUG this fixes (user-reported 2026-07-20: esports
                    # recommended bets missing a real match start time):
                    # Kalshi's own real per-match occurrence_datetime was
                    # already being fetched above (for date_by_code) and then
                    # thrown away -- only the date survived onto the match
                    # record, never the full timestamp. Kalshi's own value is
                    # authoritative, so it always overwrites vlr.gg's own
                    # rough date+AM/PM-guess while the match is still
                    # upcoming, same "always overwrite while upcoming"
                    # convention documented in valorant_data.py's own module
                    # docstring (which was never actually wired up until now)
                    # and already implemented for MMA (see poller_mma.py::
                    # _infer_start_time_from_kalshi).
                    occurrence = occurrence_by_code.get(code)
                    if match is not None and match.winner is None:
                        apply_start(match, occurrence)
                else:
                    match_id_by_code[code] = None

            matched = sum(1 for v in match_id_by_code.values() if v is not None)
            for row in map_rows:
                market_catalog_valorant.upsert_kalshi_valorant_map_winner_market(
                    session, row, match_id_by_code.get(row["match_code"])
                )
            for code, match_id in match_id_by_code.items():
                if match_id is not None:
                    market_catalog_valorant.backfill_best_of(session, match_id, max_map_by_code[code])

            # KXVALORANTGAME resolves independently by its own event_ticker
            # (one event per match, no map_code to join on) -- same "resolve
            # once per event, reuse for every market in it" pattern as
            # market_id_by_code above, just keyed differently since this
            # ticker's own event IS the match, not a per-map sub-event.
            teams_by_series_event: dict[str, set[str]] = {}
            occurrence_by_series_event: dict[str, str | None] = {}
            event_title_by_series_event: dict[str, str] = {}
            for row in series_winner_rows:
                teams_by_series_event.setdefault(row["event_ticker"], set()).add(row["team_name"])
                occurrence_by_series_event.setdefault(row["event_ticker"], row.get("occurrence_datetime"))
                event_title_by_series_event.setdefault(row["event_ticker"], row.get("event_title", ""))

            match_id_by_series_event: dict[str, int | None] = {}
            for event_ticker, teams in teams_by_series_event.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_valorant.find_or_create_upcoming_match(
                        session, team_a, team_b, event_name=event_title_by_series_event.get(event_ticker)
                    )
                    match_id_by_series_event[event_ticker] = match.id if match else None
                    occurrence = occurrence_by_series_event.get(event_ticker)
                    if match is not None and match.winner is None:
                        apply_start(match, occurrence)
                else:
                    match_id_by_series_event[event_ticker] = None

            series_matched = sum(1 for v in match_id_by_series_event.values() if v is not None)
            for row in series_winner_rows:
                market_catalog_valorant.upsert_kalshi_valorant_series_winner_market(
                    session, row, match_id_by_series_event.get(row["event_ticker"])
                )

            for row in tournament_winner_rows:
                market_catalog_valorant.upsert_kalshi_valorant_tournament_winner_market(session, row)

            session.commit()
            log.info(
                "kalshi valorant: %d/%d map matches matched, %d/%d series matches matched",
                matched, len(match_id_by_code), series_matched, len(match_id_by_series_event),
            )
        finally:
            session.close()


def refresh_polymarket_valorant_markets():
    """REAL BUG this fixes (found live 2026-07-20): all 5 of this
    function's own Polymarket calls used to happen INSIDE an open
    session, interleaved with real DB reads/writes -- fixed by fetching
    all 5 up front, then doing every DB-dependent step under
    poller_lock.py::db_write_lock()."""
    winner_rows = polymarket_valorant_client.get_match_winner_markets()
    map_rows = polymarket_valorant_client.get_map_winner_markets()
    total_maps_rows = polymarket_valorant_client.get_total_maps_markets()
    map_handicap_rows = polymarket_valorant_client.get_map_handicap_markets()
    futures_rows = polymarket_valorant_client.get_futures_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            # Every market type for a given real match is bundled under
            # ONE Polymarket event (event_slug), unlike Kalshi's per-map
            # events -- resolve match_winner's own two real team names to
            # a valorant_match_id once per event_slug, reuse for every
            # other market type from that same event, same pattern as
            # poller_mma.py's Polymarket refresh.
            teams_by_slug: dict[str, set[str]] = {}
            event_by_slug: dict[str, str] = {}
            start_time_by_slug: dict[str, str | None] = {}
            for row in winner_rows:
                teams_by_slug.setdefault(row["event_slug"], set()).add(row["team_name"])
                event_by_slug.setdefault(row["event_slug"], row.get("event_title", ""))
                start_time_by_slug.setdefault(row["event_slug"], row.get("estimated_start_time"))

            # find_or_create_upcoming_match matches onto a real vlr.gg-
            # sourced row when one exists, or creates a live fallback row
            # from Polymarket's OWN team names when it doesn't (see module
            # docstring's real-bug note).
            match_id_by_slug: dict[str, int | None] = {}
            for slug, teams in teams_by_slug.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    # Pass Polymarket's OWN gameStartTime date as match_date.
                    # Without it find_or_create_upcoming_match falls back to
                    # datetime.date.today(), so the row records the day it was
                    # SCRAPED -- the Kalshi path above has always passed a real
                    # date, this one never did.
                    #
                    # REAL BUG (user-reported 2026-08-05): Leviatan vs MIBR,
                    # a match on the 8th, showed as an August 3rd match because
                    # the 3rd is when Polymarket first listed it. 22 Valorant
                    # rows carried the same defect, every one a "live:" row.
                    #
                    # It also caused a DUPLICATE. _within_rematch_window only
                    # matches fixtures within +/-2 days, so a row stamped 5 days
                    # early is unrecognisable as the same fixture, and the
                    # vlr.gg scrape then created a second row (id 230) for the
                    # match this poller had already created (id 275).
                    start_time = start_time_by_slug.get(slug)
                    start_date = str(start_time)[:10] if start_time else None
                    match = market_catalog_valorant.find_or_create_upcoming_match(
                        session, team_a, team_b,
                        match_date=start_date, event_name=event_by_slug.get(slug),
                    )
                    match_id_by_slug[slug] = match.id if match else None
                    # REAL BUG this fixes (user-reported 2026-07-20: esports
                    # recommended bets missing a real match start time) --
                    # polymarket_valorant_client.py now extracts Polymarket's
                    # own real per-market gameStartTime (see its own module
                    # docstring); wired through here the same way Kalshi's
                    # occurrence_datetime is above.
                    start_time = start_time_by_slug.get(slug)
                    if match is not None and match.winner is None:
                        apply_start(match, start_time)
                else:
                    match_id_by_slug[slug] = None

            matched = sum(1 for v in match_id_by_slug.values() if v is not None)

            for row in winner_rows:
                market_catalog_valorant.upsert_polymarket_valorant_match_winner_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )
            for row in map_rows:
                market_catalog_valorant.upsert_polymarket_valorant_map_winner_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )
            for row in total_maps_rows:
                market_catalog_valorant.upsert_polymarket_valorant_total_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )
            for row in map_handicap_rows:
                market_catalog_valorant.upsert_polymarket_valorant_handicap_row(
                    session, row, match_id_by_slug.get(row["event_slug"])
                )

            # Best_of backfill from Polymarket's own map-number coverage
            # too (some matches may only have Polymarket map markets, not
            # Kalshi) -- same helper as the Kalshi refresh, idempotent
            # (never overwrites an already-known best_of).
            max_map_by_slug: dict[str, int] = {}
            for row in map_rows:
                max_map_by_slug[row["event_slug"]] = max(max_map_by_slug.get(row["event_slug"], 0), row["map_number"])
            for slug, match_id in match_id_by_slug.items():
                if match_id is not None and slug in max_map_by_slug:
                    market_catalog_valorant.backfill_best_of(session, match_id, max_map_by_slug[slug])

            for row in futures_rows:
                market_catalog_valorant.upsert_polymarket_valorant_futures_row(session, row)

            session.commit()
            log.info("polymarket valorant: %d/%d matches matched", matched, len(match_id_by_slug))
        finally:
            session.close()


def refresh_valorant_map_results():
    """Per-map winners from vlr.gg match pages, so map_winner bets can settle.

    One fetch per settled match that still lacks map rows -- bounded by the
    backlog, not the catalogue, and capped per cycle. Network happens before the
    write lock is taken, like every other refresh here.
    """
    from app.ingestion.valorant_map_results import collect_map_results
    from app.ingestion.valorant_map_results_apply import (
        apply_valorant_map_results, matches_needing_maps,
    )

    session = SessionLocal()
    try:
        todo = matches_needing_maps(session)
    except Exception:
        log.exception("valorant map results: lookup failed")
        return
    finally:
        session.close()
    if not todo:
        return
    try:
        by_match = collect_map_results(todo)
    except Exception:
        log.exception("valorant map results fetch failed -- retried next cycle")
        return
    if not by_match:
        return
    try:
        with db_write_lock():
            session = SessionLocal()
            try:
                apply_valorant_map_results(session, by_match)
            finally:
                session.close()
    except Exception:
        log.exception("valorant map results apply failed")


def run_full_refresh_valorant():
    refresh_valorant_matches()
    refresh_valorant_ratings()
    refresh_kalshi_valorant_markets()
    refresh_polymarket_valorant_markets()
    refresh_valorant_map_results()
    # Roster-change scrape removed 2026-07-23 -- see poller_cs2.py's note (badge
    # retired for esports, no accuracy penalty, so no reason to scrape vlr.gg).
