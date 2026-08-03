"""Tennis polling/refresh entrypoints -- parallel to poller_mma.py, with one
structural difference: there's no external schedule source to resolve a
match_id against (see market_catalog_tennis.py's docstring) -- each
platform's own moneyline listing doubles as the schedule, so
find_or_create_upcoming_match derives/reuses TennisMatch rows directly
during the same refresh pass that ingests prices, rather than a separate
upstream "refresh schedule" step.

Set winner, game spread/total, and exact-match-score (Kalshi, ATP-only for
spread/total, see kalshi_tennis_client.py's docstring) resolve their
tennis_match_id the same way MMA resolves fight_id for every non-moneyline
series: once via moneyline's own match_suffix -> tennis_match_id mapping,
reused for every other series in the same refresh pass rather than
re-matching names per series.
"""
import logging

from app.clients import kalshi_tennis_client, polymarket_tennis_client
from app.db.database import SessionLocal
from app.ingestion import market_catalog_tennis
from app.ingestion.poller_lock import db_write_lock
from app.ingestion.market_matcher_tennis import kalshi_match_suffix
from app.models.baseline import elo_service_tennis

log = logging.getLogger("poller_tennis")


def refresh_tennis_ratings():
    elo_service_tennis.refresh_ratings()


def refresh_kalshi_tennis_markets():
    """REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    connection across slow network I/O" anti-pattern this app's other
    pollers all had -- see poller_lock.py's own docstring): all 5 of this
    function's own Kalshi calls used to happen INSIDE an open session,
    interleaved with real DB reads/writes -- fixed by fetching all 5 up
    front, then doing every DB-dependent step under
    poller_lock.py::db_write_lock()."""
    rows = kalshi_tennis_client.get_moneyline_markets()
    set_winner_rows = kalshi_tennis_client.get_set_winner_markets()
    game_spread_rows = kalshi_tennis_client.get_game_spread_markets()
    game_total_rows = kalshi_tennis_client.get_game_total_markets()
    exact_match_rows = kalshi_tennis_client.get_exact_match_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            by_event: dict[str, list[dict]] = {}
            for row in rows:
                by_event.setdefault(row["event_ticker"], []).append(row)

            match_id_by_event: dict[str, int | None] = {}
            match_id_by_suffix: dict[str, int | None] = {}
            for event_ticker, event_rows in by_event.items():
                if len(event_rows) != 2:
                    match_id_by_event[event_ticker] = None
                    continue
                names = [r["player_name"] for r in event_rows]
                match = market_catalog_tennis.find_or_create_upcoming_match(
                    session, event_rows[0]["tour"], event_rows[0]["tier"], names[0], names[1],
                    event_rows[0].get("competition", ""),
                )
                market_catalog_tennis.update_match_estimated_start_time(match, event_rows[0].get("estimated_start_time"))
                market_catalog_tennis.update_match_expected_expiration(match, event_rows[0].get("expected_expiration_time"))
                match_id_by_event[event_ticker] = match.id if match else None
                suffix = kalshi_match_suffix(event_ticker)
                if suffix:
                    match_id_by_suffix[suffix] = match.id if match else None

            matched = sum(1 for v in match_id_by_event.values() if v is not None)
            for row in rows:
                market_catalog_tennis.upsert_kalshi_tennis_moneyline_market(
                    session, row, match_id_by_event.get(row["event_ticker"])
                )

            for row in set_winner_rows:
                market_catalog_tennis.upsert_kalshi_tennis_set_winner_market(
                    session, row, match_id_by_suffix.get(row["match_suffix"])
                )
            for row in game_spread_rows:
                market_catalog_tennis.upsert_kalshi_tennis_game_spread_market(
                    session, row, match_id_by_suffix.get(row["match_suffix"])
                )
            for row in game_total_rows:
                market_catalog_tennis.upsert_kalshi_tennis_game_total_market(
                    session, row, match_id_by_suffix.get(row["match_suffix"])
                )
            for row in exact_match_rows:
                market_catalog_tennis.upsert_kalshi_tennis_exact_match_market(
                    session, row, match_id_by_suffix.get(row["match_suffix"])
                )

            session.commit()
            log.info("kalshi tennis: %d/%d matches resolved, %d moneyline rows", matched, len(by_event), len(rows))
        finally:
            session.close()


def refresh_kalshi_tennis_futures():
    """Tournament-winner futures -- just ingests real live prices, no match
    resolution needed (not tied to a single TennisMatch). The bracket
    simulation itself runs at request time in the router (tennis_markets.py),
    not here, since it needs a fresh draw scrape each time, not a poll-cycle
    snapshot."""
    rows = kalshi_tennis_client.get_tournament_winner_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_tennis.upsert_kalshi_tennis_tournament_winner_market(session, row)
            session.commit()
            log.info("kalshi tennis futures: %d rows across %d tournaments", len(rows), len({r["event_ticker"] for r in rows}))
        finally:
            session.close()


def refresh_polymarket_tennis_markets():
    """See refresh_kalshi_tennis_markets's own docstring -- same real fix,
    Polymarket's own version (6 separate calls)."""
    rows = polymarket_tennis_client.get_moneyline_markets()
    set_winner_rows = polymarket_tennis_client.get_set_winner_markets()
    match_total_rows = polymarket_tennis_client.get_match_total_markets()
    set_handicap_rows = polymarket_tennis_client.get_set_handicap_markets()
    set_game_total_rows = polymarket_tennis_client.get_set_game_total_markets()
    total_sets_rows = polymarket_tennis_client.get_total_sets_markets()

    with db_write_lock():
        session = SessionLocal()
        try:
            by_event: dict[str, list[dict]] = {}
            for row in rows:
                by_event.setdefault(row["event_slug"], []).append(row)

            match_id_by_event: dict[str, int | None] = {}
            for event_slug, event_rows in by_event.items():
                if len(event_rows) != 2:
                    match_id_by_event[event_slug] = None
                    continue
                names = [r["player_name"] for r in event_rows]
                # series_slug alone can't distinguish tour vs challenger
                # here (see polymarket_tennis_client.py's docstring) --
                # "tour" is a reasonable default tier for matching purposes
                # only (tier isn't used by the name matcher, just stored
                # for display/bookkeeping on brand-new rows this poller
                # creates).
                tour = "wta" if event_rows[0]["series_slug"] == "wta" else "atp"
                match = market_catalog_tennis.find_or_create_upcoming_match(
                    session, tour, "tour", names[0], names[1],
                    event_rows[0].get("event_title", ""),
                )
                market_catalog_tennis.update_match_estimated_start_time(match, event_rows[0].get("estimated_start_time"))
                market_catalog_tennis.update_match_expected_expiration(match, event_rows[0].get("expected_expiration_time"))
                match_id_by_event[event_slug] = match.id if match else None

            matched = sum(1 for v in match_id_by_event.values() if v is not None)
            for row in rows:
                market_catalog_tennis.upsert_polymarket_tennis_moneyline_row(
                    session, row, match_id_by_event.get(row["event_slug"])
                )

            for row in set_winner_rows:
                market_catalog_tennis.upsert_polymarket_tennis_set_winner_row(
                    session, row, match_id_by_event.get(row["event_slug"])
                )
            for row in match_total_rows:
                market_catalog_tennis.upsert_polymarket_tennis_match_total_row(
                    session, row, match_id_by_event.get(row["event_slug"])
                )
            for row in set_handicap_rows:
                market_catalog_tennis.upsert_polymarket_tennis_set_handicap_row(
                    session, row, match_id_by_event.get(row["event_slug"])
                )
            for row in set_game_total_rows:
                market_catalog_tennis.upsert_polymarket_tennis_set_game_total_row(
                    session, row, match_id_by_event.get(row["event_slug"])
                )
            for row in total_sets_rows:
                market_catalog_tennis.upsert_polymarket_tennis_total_sets_row(
                    session, row, match_id_by_event.get(row["event_slug"])
                )

            session.commit()
            log.info("polymarket tennis: %d/%d matches resolved, %d moneyline rows", matched, len(by_event), len(rows))
        finally:
            session.close()


def refresh_tennis_results():
    """Backfill final winner+score onto finished TennisMatch rows so tennis bets
    can auto-settle. Network fetch (multiple flaky tennisexplorer requests) runs
    BEFORE the write lock; only the short apply/commit takes it."""
    from app.ingestion.tennis_results import fetch_results_index, apply_results_index
    try:
        session = SessionLocal()
        try:
            index = fetch_results_index(session)  # reads + network, no write lock
        finally:
            session.close()
        if not index:
            return
        with db_write_lock():
            session = SessionLocal()
            try:
                apply_results_index(session, index)
            finally:
                session.close()
    except Exception:
        log.exception("tennis results backfill failed")



def refresh_tennis_start_times():
    """Overwrite estimated_start_time from tennisexplorer's live order of play.

    Kalshi's occurrence_datetime is a scheduled estimate it never revises, so a
    slipping order of play leaves the app showing the original time and every
    "has it started?" gate reasoning from a wrong number. tennisexplorer tracks
    the real schedule -- verified 2026-08-03 on two matches the user confirmed
    were already under way (17:10Z and 16:50Z actual vs 23:00Z and 19:00Z from
    Kalshi).

    Only unfinished matches are touched, and only when the site actually lists
    the player, so a missing row leaves the existing value alone rather than
    blanking it.
    """
    from app.clients.tennisexplorer_client import TennisExplorerClient
    import datetime

    from app.db.models import TennisMatch

    try:
        with TennisExplorerClient() as client:
            times = client.get_scheduled_times()
    except Exception:
        log.exception("tennisexplorer schedule fetch failed")
        return
    if not times:
        return

    def _key(full_name: str | None) -> str | None:
        if not full_name:
            return None
        parts = full_name.split()
        return f"{parts[-1]} {parts[0][0]}." if len(parts) >= 2 else None

    with db_write_lock():
        session = SessionLocal()
        try:
            updated = 0
            # TODAY'S matches only. The schedule page is today's order of play, and
            # the lookup key is just "Surname I." -- without this, an unfinished
            # fixture from weeks ago involving the same player would be stamped
            # with today's time.
            today = datetime.date.today().isoformat()
            candidates = (
                session.query(TennisMatch)
                .filter(TennisMatch.winner_key.is_(None), TennisMatch.match_date == today)
                .all()
            )
            for match in candidates:
                for name in (match.player_a_name, match.player_b_name):
                    key = _key(name)
                    fresh = times.get(key) if key else None
                    if fresh and fresh != match.estimated_start_time:
                        match.estimated_start_time = fresh
                        match.start_time_source = "tennisexplorer"
                        updated += 1
                        break
            session.commit()
            log.info("tennis start times refreshed from tennisexplorer: %d updated", updated)
        finally:
            session.close()

def run_full_refresh_tennis():
    refresh_tennis_start_times()
    refresh_tennis_ratings()
    refresh_kalshi_tennis_markets()
    refresh_polymarket_tennis_markets()
    refresh_kalshi_tennis_futures()
    refresh_tennis_results()
