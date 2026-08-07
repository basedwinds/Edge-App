"""UFC polling/refresh entrypoints -- parallel to poller_mlb.py.

Covers moneyline/distance/method-of-victory/method-of-finish/rounds/
round-of-victory game markets, plus the fighter Elo baseline (moneyline
only -- see elo_mma.py/elo_service_mma.py). Deliberately NOT built yet:
futures (KXUFCTITLE family -- user asked for these last/low-priority) and
the situational/news layer (Phase 3).
"""
import logging

from app.clients import kalshi_mma_client, polymarket_mma_client
from app.db.database import SessionLocal
from app.db.models import MmaFight
from app.ingestion import market_catalog_mma, ufc_data
from app.ingestion.market_matcher_mma import (
    date_from_fight_suffix,
    date_from_polymarket_slug,
    match_fight_by_names_only,
)
from app.ingestion.poller_lock import db_write_lock
from app.models import distance_service_mma, method_service_mma, rounds_service_mma
from app.models.baseline import elo_service_mma

log = logging.getLogger("poller_mma")


def refresh_mma_ratings():
    elo_service_mma.refresh_ratings()
    distance_service_mma.refresh_model()
    method_service_mma.refresh_model()
    rounds_service_mma.refresh_model()


def _load_fights(session) -> list[dict]:
    return [
        {
            "id": f.id, "event_date": f.event_date,
            "fighter_a_name": f.fighter_a_name, "fighter_b_name": f.fighter_b_name,
        }
        for f in session.query(MmaFight).all()
    ]


def refresh_mma_fights():
    """Live-scrapes ufcstats' upcoming-card list (cached 1h, see
    ufc_data.fetch_upcoming_fights) -- this is deliberately NOT the full
    historical crawl (scripts/build_ufc_fight_cache.py, a one-off run for
    model training data), just the handful of scheduled-ahead cards needed
    to match against live Kalshi/Polymarket markets."""
    fights = ufc_data.fetch_upcoming_fights()
    with db_write_lock():
        session = SessionLocal()
        try:
            count = market_catalog_mma.upsert_mma_fights(session, fights)
            log.info("refreshed %d upcoming mma fights", count)
        finally:
            session.close()


def _infer_scheduled_rounds_from_kalshi(session, rounds_rows: list[dict], fight_id_by_suffix: dict[str, str | None]) -> int:
    """ufcstats never publishes scheduled_rounds for an upcoming (not-yet-
    fought) fight -- confirmed live, its time_format field only gets filled
    in retroactively once a fight actually happens. Kalshi's own
    KXUFCROUNDS ladder ("ends before round N?") is real ground truth
    instead: its highest listed N IS the real scheduled_rounds (a 3-round
    fight lists N up to 3, a 5-round fight up to 5 -- confirmed live: the
    real Du Plessis vs. Usman main event lists N up to 5, every other fight
    on that same card lists up to 3). Only backfills MmaFight rows that
    don't already have a real value (never overwrites ufcstats' own
    post-fight time_format-derived number)."""
    max_round_by_suffix: dict[str, int] = {}
    for row in rounds_rows:
        suffix = row["fight_suffix"]
        max_round_by_suffix[suffix] = max(max_round_by_suffix.get(suffix, 0), row["before_round"])

    updated = 0
    for suffix, scheduled_rounds in max_round_by_suffix.items():
        fight_id = fight_id_by_suffix.get(suffix)
        if fight_id is None:
            continue
        fight = session.get(MmaFight, fight_id)
        if fight is not None and fight.scheduled_rounds is None:
            fight.scheduled_rounds = scheduled_rounds
            updated += 1
    return updated


def _infer_start_time_from_kalshi(session, moneyline_rows: list[dict], fight_id_by_suffix: dict[str, str | None]) -> int:
    """Kalshi's own per-fight `occurrence_datetime` (see kalshi_mma_client.py's
    _market_row docstring) is the only real, per-fight-granular estimated
    start time either platform exposes -- Polymarket's equivalent field is
    flat per-EVENT (same value for every fight on a card), confirmed live.
    Unlike scheduled_rounds (a fact that becomes permanent once known),
    this is a genuine ESTIMATE that can shift as a card's real fight order
    gets reshuffled -- always overwritten with the latest value while a
    fight is still upcoming, never backfilled-once."""
    time_by_suffix: dict[str, str] = {}
    for row in moneyline_rows:
        dt = row.get("occurrence_datetime")
        if dt:
            time_by_suffix[row["fight_suffix"]] = dt

    updated = 0
    for suffix, occurrence_dt in time_by_suffix.items():
        fight_id = fight_id_by_suffix.get(suffix)
        if fight_id is None:
            continue
        fight = session.get(MmaFight, fight_id)
        if fight is not None and fight.winner_id is None:  # only keep updating while still upcoming
            fight.estimated_start_time = occurrence_dt
            updated += 1
    return updated


def refresh_kalshi_mma_markets():
    """REAL BUG this fixes (found live 2026-07-20, same "hold the DB
    connection across slow network I/O" anti-pattern this app's other
    pollers all had -- see poller_lock.py's own docstring): all 6 of this
    function's own Kalshi calls used to happen INSIDE an open session.
    This one genuinely needs a quick DB READ first (the fight list to
    match names against), which doesn't need the write lock (WAL-mode
    reads don't contend with writes) -- then every network fetch with no
    session open, then a final session under
    poller_lock.py::db_write_lock() for the real writes."""
    read_session = SessionLocal()
    try:
        all_fights = _load_fights(read_session)
    finally:
        read_session.close()

    # Every series shares the same fight_suffix for a given fight (see
    # kalshi_mma_client.py's docstring) -- resolved to a fight_id once
    # here via the moneyline series' real fighter names, then reused for
    # every other series below rather than re-matching per series.
    moneyline_rows = kalshi_mma_client.get_moneyline_markets()
    distance_rows = kalshi_mma_client.get_distance_markets()
    mov_rows = kalshi_mma_client.get_method_of_victory_markets()
    mof_rows = kalshi_mma_client.get_method_of_finish_markets()
    rounds_rows = kalshi_mma_client.get_rounds_markets()
    round_of_victory_rows = kalshi_mma_client.get_round_of_victory_markets()

    names_by_suffix: dict[str, list[str]] = {}
    for row in moneyline_rows:
        names_by_suffix.setdefault(row["fight_suffix"], []).append(row["fighter_name"])

    fight_id_by_suffix: dict[str, str | None] = {}
    for suffix, names in names_by_suffix.items():
        if len(names) == 2:
            # The suffix leads with the card date, which narrows the matcher's
            # loose fallback to that one card (see market_matcher_mma).
            fight = match_fight_by_names_only(
                names[0], names[1], all_fights, date_from_fight_suffix(suffix)
            )
            fight_id_by_suffix[suffix] = fight["id"] if fight else None
        else:
            fight_id_by_suffix[suffix] = None

    matched = sum(1 for v in fight_id_by_suffix.values() if v is not None)
    unmatched = len(fight_id_by_suffix) - matched

    with db_write_lock():
        session = SessionLocal()
        try:
            for row in moneyline_rows:
                market_catalog_mma.upsert_kalshi_mma_moneyline_market(
                    session, row, fight_id_by_suffix.get(row["fight_suffix"])
                )
            for row in distance_rows:
                market_catalog_mma.upsert_kalshi_mma_distance_market(
                    session, row, fight_id_by_suffix.get(row["fight_suffix"])
                )
            for row in mov_rows:
                market_catalog_mma.upsert_kalshi_mma_mov_market(
                    session, row, fight_id_by_suffix.get(row["fight_suffix"])
                )
            for row in mof_rows:
                market_catalog_mma.upsert_kalshi_mma_mof_market(
                    session, row, fight_id_by_suffix.get(row["fight_suffix"])
                )
            for row in rounds_rows:
                market_catalog_mma.upsert_kalshi_mma_rounds_market(
                    session, row, fight_id_by_suffix.get(row["fight_suffix"])
                )
            for row in round_of_victory_rows:
                market_catalog_mma.upsert_kalshi_mma_round_of_victory_market(
                    session, row, fight_id_by_suffix.get(row["fight_suffix"])
                )
            rounds_inferred = _infer_scheduled_rounds_from_kalshi(session, rounds_rows, fight_id_by_suffix)
            start_times_updated = _infer_start_time_from_kalshi(session, moneyline_rows, fight_id_by_suffix)

            session.commit()
            log.info(
                "kalshi mma: %d/%d fights matched, %d scheduled_rounds inferred, %d start times updated",
                matched, matched + unmatched, rounds_inferred, start_times_updated,
            )
        finally:
            session.close()


def refresh_polymarket_mma_markets():
    """See refresh_kalshi_mma_markets's own docstring -- same real fix,
    Polymarket's own version (4 separate calls, same real DB-read-first
    shape for the fight list)."""
    read_session = SessionLocal()
    try:
        all_fights = _load_fights(read_session)
    finally:
        read_session.close()

    # Polymarket bundles every market type for a fight into ONE event
    # (keyed by event_slug), unlike Kalshi's per-series events -- same
    # "resolve once via moneyline, reuse for every other market type"
    # pattern as the Kalshi refresh above, just keyed by slug instead.
    moneyline_rows = polymarket_mma_client.get_moneyline_markets()
    distance_rows = polymarket_mma_client.get_distance_markets()
    method_rows = polymarket_mma_client.get_method_markets()
    rounds_rows = polymarket_mma_client.get_rounds_markets()

    names_by_slug: dict[str, list[str]] = {}
    for row in moneyline_rows:
        names_by_slug.setdefault(row["event_slug"], []).append(row["fighter_name"])

    fight_id_by_slug: dict[str, str | None] = {}
    for slug, names in names_by_slug.items():
        if len(names) == 2:
            # Polymarket's slug ends in the card date -- same narrowing as the
            # Kalshi path above.
            fight = match_fight_by_names_only(
                names[0], names[1], all_fights, date_from_polymarket_slug(slug)
            )
            fight_id_by_slug[slug] = fight["id"] if fight else None
        else:
            fight_id_by_slug[slug] = None

    matched = sum(1 for v in fight_id_by_slug.values() if v is not None)
    unmatched = len(fight_id_by_slug) - matched

    with db_write_lock():
        session = SessionLocal()
        try:
            for row in moneyline_rows:
                market_catalog_mma.upsert_polymarket_mma_moneyline_row(
                    session, row, fight_id_by_slug.get(row["event_slug"])
                )
            for row in distance_rows:
                market_catalog_mma.upsert_polymarket_mma_distance_row(
                    session, row, fight_id_by_slug.get(row["event_slug"])
                )
            for row in method_rows:
                market_catalog_mma.upsert_polymarket_mma_method_row(
                    session, row, fight_id_by_slug.get(row["event_slug"])
                )
            for row in rounds_rows:
                market_catalog_mma.upsert_polymarket_mma_rounds_row(
                    session, row, fight_id_by_slug.get(row["event_slug"])
                )

            session.commit()
            log.info("polymarket mma: %d/%d fights matched", matched, matched + unmatched)
        finally:
            session.close()


def run_full_refresh_mma():
    refresh_mma_fights()
    refresh_mma_ratings()
    refresh_kalshi_mma_markets()
    refresh_polymarket_mma_markets()
