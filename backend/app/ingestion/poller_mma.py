"""UFC polling/refresh entrypoints -- parallel to poller_mlb.py.

Covers moneyline/distance/method-of-victory/method-of-finish/rounds/
round-of-victory game markets, plus the fighter Elo baseline (moneyline
only -- see elo_mma.py/elo_service_mma.py). Deliberately NOT built yet:
futures (KXUFCTITLE family -- user asked for these last/low-priority) and
the situational/news layer (Phase 3).
"""
import datetime as dt
import logging

from app.clients import kalshi_mma_client, polymarket_mma_client
from app.db.database import SessionLocal
from app.db.models import MmaFight
from app.ingestion import market_catalog_mma, ufc_data
from app.ingestion.market_matcher_mma import (
    date_from_fight_suffix,
    date_from_polymarket_slug,
    match_fight_by_names_only,
    names_from_event_title,
)
from app.ingestion.poller_lock import db_write_lock
from app.models import distance_service_mma, mma_model_disagreement, method_service_mma, rounds_service_mma
from app.models.baseline import elo_service_mma

log = logging.getLogger("poller_mma")


def refresh_mma_ratings():
    elo_service_mma.refresh_ratings()
    distance_service_mma.refresh_model()
    method_service_mma.refresh_model()
    rounds_service_mma.refresh_model()
    # Advisory only -- trains the style+defence model used to FLAG matchups where
    # the shipped Elo price may be missing something. Prices nothing. Fails soft.
    mma_model_disagreement.refresh()


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


# How many fights to resolve per pass. Each is one PoW-gated ufcstats fetch, so
# an unbounded backlog would stall the whole mma refresh behind it. A UFC card is
# ~12 fights and they run weekly, so 40 clears a normal week's backlog in one
# pass and any historical backlog within a few.
_RESULT_BACKFILL_LIMIT = 40


def backfill_mma_results() -> int:
    """Fill in winner/method/round for fights that have HAPPENED.

    refresh_mma_fights only ever sees ufcstats' UPCOMING list, so a fight is
    created with winner_id=None and never revisited once it is fought. See
    ufc_data.fetch_fight_results for the full measurement cost of that.

    Only the RESULT fields are written. The identity fields (event, date,
    fighters) stay as ingested -- fetch_fight_results stubs them because a
    fight-details page carries no event context, and copying those stubs over
    good data would be a far worse bug than the one being fixed.

    A fight that has not actually been fought yet comes back with winner_id=None
    and is left untouched, so an early call cannot blank anything.
    """
    today = dt.date.today().isoformat()
    session = SessionLocal()
    try:
        pending = [
            f.id for f in session.query(MmaFight)
            .filter(MmaFight.winner_id.is_(None),
                    MmaFight.event_date < today)
            .order_by(MmaFight.event_date.desc())
            .limit(_RESULT_BACKFILL_LIMIT).all()
        ]
    finally:
        session.close()
    if not pending:
        return 0

    results = ufc_data.fetch_fight_results(pending)
    updated = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for fid, f in results.items():
                row = session.get(MmaFight, fid)
                if row is None:
                    continue
                # Still unfought (or a genuine draw/NC with nothing to record) --
                # leave the row exactly as it is rather than writing nulls.
                if not f.get("winner_id") and not f.get("method"):
                    continue
                row.winner_id = f.get("winner_id")
                row.method = f.get("method")
                row.round = f.get("round")
                row.time = f.get("time")
                if f.get("scheduled_rounds"):
                    row.scheduled_rounds = f["scheduled_rounds"]
                if f.get("went_the_distance") is not None:
                    row.went_the_distance = f["went_the_distance"]
                updated += 1
            session.commit()
        finally:
            session.close()
    log.info("backfilled results for %d mma fights (%d checked)", updated, len(pending))
    return updated


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


def _infer_scheduled_rounds_from_polymarket(session, rounds_rows: list[dict], fight_id_by_slug: dict[str, str | None]) -> int:
    """The Polymarket twin of _infer_scheduled_rounds_from_kalshi, and the one
    that actually fires for most cards.

    REAL BUG THIS FIXES (2026-08-09). ufcstats never publishes scheduled_rounds
    for an upcoming fight, so it has to be inferred from a book's round ladder.
    That inference existed for KALSHI ONLY -- and Kalshi does not list
    distance/method/rounds markets for every card. On the 2026-08-15 card it
    listed moneyline and nothing else, so the ladder was empty, nothing was
    inferred, and scheduled_rounds stayed NULL on all 19 fights.

    Every downstream model takes scheduled_rounds and returns None without it,
    so that single missing field silently zeroed the entire non-moneyline half
    of MMA: 30 distance + 60 method_of_finish + 60 method_of_victory + 96 rounds
    rows served unpriced, from three models that are built, backtested and
    working. The poller even reported "11/11 fights matched", because matching
    WAS fine -- the failure was one field further on.

    Polymarket's own ladder answers it exactly the way Kalshi's does: the
    highest "over N.5 rounds" rung is one short of the scheduled distance, so
    max_line + 0.5 IS the real number. Verified against the 2026-08-15 card:
    16 fights topped out at 2.5 (three rounds) and three at 4.5 (five), and the
    three included Hernandez vs Rodrigues, which is a five-round MAIN EVENT but
    NOT a title fight. A "title bout implies five rounds" shortcut would have
    got that one wrong -- the ladder is real evidence, not a proxy for it.

    REFUSES ANYTHING THAT IS NOT 3 OR 5. Those are the only distances the UFC
    actually schedules, so any other answer means the ladder was truncated or
    the rows were mis-joined, and a wrong scheduled_rounds is worse than a
    missing one: it would not leave the market unpriced, it would price it
    against the wrong fight length. Never overwrites a value already known.
    """
    max_line_by_slug: dict[str, float] = {}
    for row in rounds_rows:
        line = row.get("line")
        if line is None:
            continue
        slug = row.get("event_slug")
        max_line_by_slug[slug] = max(max_line_by_slug.get(slug, 0.0), float(line))

    updated = 0
    for slug, max_line in max_line_by_slug.items():
        fight_id = fight_id_by_slug.get(slug)
        if fight_id is None:
            continue
        scheduled = int(round(max_line + 0.5))
        if scheduled not in (3, 5):
            log.warning("mma: polymarket rounds ladder for %s tops out at %.1f -> %d "
                        "scheduled rounds, which is not a real UFC distance; refusing "
                        "to set it", slug, max_line, scheduled)
            continue
        fight = session.get(MmaFight, fight_id)
        if fight is not None and fight.scheduled_rounds is None:
            fight.scheduled_rounds = scheduled
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

    # A suffix can be missing from the MONEYLINE series while its other series
    # are still listed -- live 2026-08-06: 26AUG08JOHROS (Miles Johns vs Jessie
    # Rosas) was gone from all 22 open moneyline suffixes while still trading in
    # distance, method of finish, rounds AND round of victory. Resolving names
    # from the moneyline alone therefore loses a fight that is still listed, and
    # (before market_catalog_mma._set_fight_id) actively NULLed its 14 existing
    # links. Those series carry both fighters in their event title, so fall back
    # to it for any suffix the moneyline didn't name.
    #
    # The cause turned out to be an opponent REPLACEMENT (Johns vs Vazquez
    # replaced Johns vs Rosas the next day), not a retirement ordering -- see
    # market_catalog_mma._set_fight_id for the correction. Worth knowing here
    # because it means a suffix recovered this way may belong to a matchup that
    # is being wound down: Kalshi does close those markets, but not instantly.
    for row in (*distance_rows, *mov_rows, *mof_rows, *rounds_rows, *round_of_victory_rows):
        suffix = row.get("fight_suffix")
        if not suffix or suffix in names_by_suffix:
            continue
        pair = names_from_event_title(row.get("event_title", ""))
        if pair:
            names_by_suffix[suffix] = list(pair)

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

            # AFTER the rows are upserted, so a fight that only just got its
            # first market still gets its distance set on the same pass.
            rounds_inferred = _infer_scheduled_rounds_from_polymarket(
                session, rounds_rows, fight_id_by_slug)

            session.commit()
            log.info("polymarket mma: %d/%d fights matched, %d scheduled_rounds inferred",
                     matched, matched + unmatched, rounds_inferred)
        finally:
            session.close()


def run_full_refresh_mma():
    # Each step wrapped so one upstream failure -- a Kalshi 429 is the common
    # one -- cannot silently skip everything after it. That exact failure was
    # skipping soccer's settlement until 2026-08-08; the same shape applies here.
    for name, fn in (
        ("fights", refresh_mma_fights),
        # AFTER fights, BEFORE ratings: a fight resolved this pass should feed
        # the Elo refresh in the SAME pass rather than waiting a cycle.
        ("results backfill", backfill_mma_results),
        ("ratings", refresh_mma_ratings),
        ("kalshi markets", refresh_kalshi_mma_markets),
        ("kalshi title futures", refresh_kalshi_mma_title_markets),
        ("polymarket markets", refresh_polymarket_mma_markets),
    ):
        try:
            fn()
        except Exception:
            log.exception("mma %s refresh failed -- continuing the rest of the pass", name)


def refresh_kalshi_mma_title_markets():
    """UFC weight-class title futures. Own entrypoint, not folded into
    refresh_kalshi_mma_markets, for a structural reason: that function reads the
    fight list first and hangs every row on a fight_suffix. These are NOT
    fight-tied -- there is no fight to join to -- so threading them through
    would put a null-fight special case into the busiest path in this file.

    Fetch before the session opens, same discipline as every poller here.
    """
    rows = kalshi_mma_client.get_title_markets()
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_mma.upsert_kalshi_mma_title_market(session, row)
            session.commit()
        finally:
            session.close()
    log.info("kalshi mma title markets refreshed: %d rows", len(rows))
    return len(rows)
