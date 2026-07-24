"""CS2 polling/refresh entrypoints -- parallel to poller_valorant.py.

Covers series winner + total maps (both real, live Kalshi inventory,
confirmed 2026-07-19) + map winner + tournament winner futures (both real
Kalshi SERIES but zero open markets at build time -- kept so they start
working the moment either populates, see kalshi_cs2_client.py's own
docstring), plus the team Elo baseline. No Polymarket CS2 client exists --
checked live, real current Polymarket CS2 inventory is prop/roster-
change/futures markets only (FaZe tier-1 event, map pool changes, roster
changes), no standard match-outcome market type at all -- an honest
inventory gap, not a build gap.

REAL COVERAGE GAP found live (2026-07-19, not a matcher bug -- verified by
checking every unmatched team name against the full raw liquipedia.net
fetch): Liquipedia:Matches' own default listing is curated toward bigger/
higher-tier tournaments -- real lower-tier/regional/qualifier/Academy-roster
matches (e.g. "eSuba vs EAC Rising", "MIBR Academy vs QUINTESSENCIA", "ARCRED
vs 1WIN") simply never appear anywhere in Liquipedia's own ~72-row fetch,
upcoming or completed. Same category of honest, structural free-source gap
as Tennis's original ATP/WTA-only coverage before tennisexplorer.com closed
the Challenger/ITF gap -- a similarly deeper Liquipedia scrape (individual
lower-tier tournament/qualifier pages, not just the curated Matches page,
see cs2_data.py::parse_matches_from_html, already built for the historical
crawl and reusable here) could close this further, not attempted yet.

REAL COVERAGE GAP found live and mostly closed (2026-07-20, user-reported --
"what else should we do for esports"): being unmatched to a real Liquipedia
row turned out to matter far less than initially assumed -- find_or_create_
upcoming_match's live-fallback path already gives every one of these matches
a real Cs2Match row. The ACTUAL blocker was best_of staying None forever on
those live-fallback rows (no Liquipedia schedule text to read "(Bo3)" from),
which gates _game_model_prob entirely regardless of Elo rating quality --
confirmed live: 24/30 real open series_winner matches had zero model_prob,
not the ~6/30 the "unmatched" framing above implied. Closed via
market_catalog_cs2.py::backfill_best_of_from_total_maps_line (KXCS2TOTALMAPS's
own O/U line structurally implies best_of even though KXCS2MAPWINNER itself
has zero real inventory) -- lifted live coverage from 6/30 to 26/30 matches
with a real model_prob. The remaining 4/30 have no KXCS2TOTALMAPS market
either, so there's genuinely no signal to backfill best_of from yet -- the
deeper Liquipedia scrape noted above is the only way to close that last
slice.

REAL BUG fixed here (found live 2026-07-19, user report: "Matches tracked"
showing 0 despite real Kalshi market rows existing): same root cause and
fix as poller_valorant.py's own docstring -- market_catalog_cs2.py::
find_or_create_upcoming_match existed but was never actually called from
refresh_kalshi_cs2_markets(), which used a strict lookup-only helper
instead and gave up with a permanently-unmatched market whenever
liquipedia.net hadn't (yet) captured that match. Fixed by calling
find_or_create_upcoming_match, same "the live listing IS the schedule"
fallback Tennis/Soccer already rely on.
"""
import datetime as dt
import logging

from app.clients import kalshi_cs2_client
from app.db.database import SessionLocal
from app.ingestion import cs2_data, market_catalog_cs2
from app.ingestion.poller_lock import db_write_lock
from app.models.baseline import elo_service_cs2

log = logging.getLogger("poller_cs2")


def refresh_cs2_ratings():
    elo_service_cs2.refresh_ratings()


def _match_date_from_iso(occurrence_datetime: str | None) -> str | None:
    if not occurrence_datetime:
        return None
    try:
        return dt.datetime.fromisoformat(occurrence_datetime.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def refresh_cs2_matches():
    """liquipedia.net's own live schedule (upcoming + recently-decided, one
    page) -- NOT a full historical crawl, same cold-start caveat as
    elo_service_cs2.py's own docstring.

    REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    connection across slow network I/O" anti-pattern this app's other
    pollers all had -- see poller_lock.py's own docstring): the fetch used
    to happen INSIDE an open SessionLocal()."""
    rows = cs2_data.fetch_matches()
    with db_write_lock():
        session = SessionLocal()
        try:
            count = 0
            for row in rows:
                market_catalog_cs2.upsert_liquipedia_match(session, row)
                count += 1
            session.commit()
            log.info("refreshed %d liquipedia.net cs2 matches", count)
        finally:
            session.close()


def refresh_kalshi_cs2_markets():
    """REAL BUG this fixes (found live 2026-07-20): all 4 of this
    function's own Kalshi calls used to happen INSIDE an open session,
    interleaved with real DB reads/writes -- fixed by fetching all 4 up
    front, then doing every DB-dependent step under
    poller_lock.py::db_write_lock()."""
    winner_rows = kalshi_cs2_client.get_series_winner_markets()
    total_maps_rows = kalshi_cs2_client.get_total_maps_markets()
    map_rows = kalshi_cs2_client.get_map_winner_markets()
    tournament_winner_rows = kalshi_cs2_client.get_tournament_winner_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            # Series winner (KXCS2GAME): one event per match, resolve team
            # names -> match_id ONCE per event, reuse for the total-maps
            # series below (different Kalshi series but same real match,
            # joined by team-name pair rather than a shared ticker suffix
            # -- KXCS2GAME and KXCS2TOTALMAPS confirmed live to use the
            # SAME event-ticker date+team-code suffix, e.g. both
            # "...26JUL211200FOKAST", so this could also join on that
            # shared prefix; joining by team names instead is simpler and
            # doesn't assume that ticker convention holds forever).
            # find_or_create_upcoming_match matches onto a real
            # liquipedia.net-sourced row when one exists, or creates a
            # live fallback row from Kalshi's OWN team names when it
            # doesn't (see module docstring's real-bug note).
            teams_by_event: dict[str, set[str]] = {}
            date_by_event: dict[str, str | None] = {}
            occurrence_by_event: dict[str, str | None] = {}
            for row in winner_rows:
                teams_by_event.setdefault(row["event_ticker"], set()).add(row["team_name"])
                date_by_event.setdefault(row["event_ticker"], _match_date_from_iso(row.get("occurrence_datetime")))
                occurrence_by_event.setdefault(row["event_ticker"], row.get("occurrence_datetime"))

            match_id_by_event: dict[str, int | None] = {}
            for event_ticker, teams in teams_by_event.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_cs2.find_or_create_upcoming_match(
                        session, team_a, team_b, match_date=date_by_event.get(event_ticker)
                    )
                    match_id_by_event[event_ticker] = match.id if match else None
                    # REAL BUG this fixes (user-reported 2026-07-20: esports
                    # recommended bets missing a real match start time) --
                    # see poller_valorant.py's own version of this comment.
                    # occurrence_datetime was already being fetched above
                    # (for date_by_event) and then thrown away -- only the
                    # date survived onto the match record.
                    occurrence = occurrence_by_event.get(event_ticker)
                    if match is not None and match.winner is None and occurrence:
                        match.estimated_start_time = occurrence
                else:
                    match_id_by_event[event_ticker] = None

            matched = sum(1 for v in match_id_by_event.values() if v is not None)
            for row in winner_rows:
                market_catalog_cs2.upsert_kalshi_cs2_series_winner_market(
                    session, row, match_id_by_event.get(row["event_ticker"])
                )

            best_of_backfilled = 0
            for row in total_maps_rows:
                match = market_catalog_cs2.find_or_create_upcoming_match(session, row["team_a"], row["team_b"])
                occurrence = row.get("occurrence_datetime")
                if match is not None and match.winner is None and occurrence:
                    match.estimated_start_time = occurrence
                # REAL COVERAGE GAP this closes (found live 2026-07-20) --
                # see market_catalog_cs2.py::backfill_best_of_from_total_maps_line's
                # own docstring for the full story. Kept even after
                # KXCS2MAP's own real per-map ladder data became available
                # below (see kalshi_cs2_client.py's own real-bug fix) --
                # this total-maps-line signal covers matches that have a
                # KXCS2TOTALMAPS market but no KXCS2MAP one, or vice versa,
                # and never overwrites a value the other one already set.
                if match is not None and row.get("line") is not None:
                    if market_catalog_cs2.backfill_best_of_from_total_maps_line(session, match.id, row["line"]):
                        best_of_backfilled += 1
                market_catalog_cs2.upsert_kalshi_cs2_total_maps_market(
                    session, row, match.id if match else None
                )

            teams_by_code: dict[str, set[str]] = {}
            occurrence_by_code: dict[str, str | None] = {}
            max_map_by_code: dict[str, int] = {}
            for row in map_rows:
                teams_by_code.setdefault(row["match_code"], set()).add(row["team_name"])
                occurrence_by_code.setdefault(row["match_code"], row.get("occurrence_datetime"))
                max_map_by_code[row["match_code"]] = max(max_map_by_code.get(row["match_code"], 0), row["map_number"])
            match_id_by_code: dict[str, int | None] = {}
            for code, teams in teams_by_code.items():
                if len(teams) == 2:
                    team_a, team_b = tuple(teams)
                    match = market_catalog_cs2.find_or_create_upcoming_match(session, team_a, team_b)
                    match_id_by_code[code] = match.id if match else None
                    occurrence = occurrence_by_code.get(code)
                    if match is not None and match.winner is None and occurrence:
                        match.estimated_start_time = occurrence
                    # REAL COVERAGE GAP this closes (found live 2026-07-20,
                    # part of the same KXCS2MAP ticker fix -- see
                    # kalshi_cs2_client.py's own docstring): CS2 never had
                    # this per-map-ladder-depth backfill at all (Valorant/
                    # LoL both already had it), since its own map_winner
                    # inventory was genuinely empty until now.
                    if match is not None:
                        if market_catalog_cs2.backfill_best_of(session, match.id, max_map_by_code[code]):
                            best_of_backfilled += 1
            for row in map_rows:
                market_catalog_cs2.upsert_kalshi_cs2_map_winner_market(
                    session, row, match_id_by_code.get(row["match_code"])
                )

            for row in tournament_winner_rows:
                market_catalog_cs2.upsert_kalshi_cs2_tournament_winner_market(session, row)

            session.commit()
            log.info("kalshi cs2: %d/%d matches matched, %d best_of backfilled (total-maps line + map ladder)", matched, len(match_id_by_event), best_of_backfilled)
        finally:
            session.close()


def run_full_refresh_cs2():
    refresh_cs2_matches()
    refresh_cs2_ratings()
    refresh_kalshi_cs2_markets()
    # Roster-change scrape removed 2026-07-23: the informational "Wait" badge
    # it fed was retired for esports (no post-roster-change accuracy penalty
    # found -- see scripts/calibrate_cs2_roster_window.py), so there's nothing
    # left to display and no reason to hit Liquipedia every cycle for it.
