"""Call of Duty refresh pass. Mirrors poller_cs2's structure, including the
two disciplines that file's own docstring earned the hard way:

  * NO DB CONNECTION IS HELD ACROSS NETWORK I/O. Every fetch happens first,
    outside any session, and only the writes take the lock.
  * STEPS RUN IN ORDER AND EACH IS NON-FATAL. A step that fails logs and the
    pass continues, so one dead upstream cannot take the whole sport down --
    but ORDER still protects correctness, since ratings and matches must be
    in place before markets bind to them.
"""
from __future__ import annotations

import logging

from app.clients import kalshi_cod_client, polymarket_cod_client
from app.db.database import SessionLocal
from app.ingestion import cod_data, market_catalog_cod
from app.ingestion.poller_lock import db_write_lock
from app.models.baseline import elo_service_cod

log = logging.getLogger("poller_cod")


def refresh_cod_ratings():
    """Rebuild ratings from the historical crawl plus the live table. Takes no
    session -- same contract as the other esports rating services."""
    elo_service_cod.refresh_ratings()


def refresh_cod_matches():
    """Upcoming, live and recently-completed matches from breakingpoint."""
    rows = cod_data.fetch_matches()
    if not rows:
        log.warning("cod: fetch_matches returned nothing -- not writing")
        return
    live = sum(1 for r in rows if r.get("is_live"))
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                market_catalog_cod.upsert_cod_match(session, row)
            session.commit()
            log.info("cod: %d matches upserted (%d live)", len(rows), live)
        finally:
            session.close()


def refresh_kalshi_cod_markets():
    """KXCODGAME match-winner markets, bound to breakingpoint fixtures.

    The bind is an EXACT name join -- Kalshi and breakingpoint both say
    "OpTic Gaming" -- so an unmatched market means a genuinely new team or a
    real spelling change, not fuzzy-match noise. It is logged rather than
    silently skipped, because an unbound market is an unpriced market and the
    only symptom would be a market that never appears."""
    rows = kalshi_cod_client.get_series_winner_markets()
    if not rows:
        log.info("cod kalshi: no open match-winner markets")
        return
    matched = unmatched = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for row in rows:
                match = market_catalog_cod.find_match_for_kalshi_event(
                    session, row.get("event_title", ""), row.get("occurrence_datetime"))
                if match is None:
                    unmatched += 1
                else:
                    matched += 1
                market_catalog_cod.upsert_kalshi_cod_series_winner_market(
                    session, row, match.id if match else None)
            session.commit()
            log.info("cod kalshi: %d/%d markets bound to a fixture", matched, len(rows))
            if unmatched:
                log.warning(
                    "cod kalshi: %d market(s) matched NO fixture -- they will stay unpriced. "
                    "Titles: %s", unmatched,
                    sorted({r.get("event_title", "") for r in rows})[:5])
        finally:
            session.close()


def refresh_polymarket_cod_markets():
    """Polymarket match-winner + total-maps markets.

    Polymarket is where CoD's liquidity actually is -- $36k on a single match
    line against Kalshi's whole board -- and wiring it is also what lets the
    cross-platform divergence scanner see CoD at all, since a divergence needs
    both venues.

    Per-game ("Game N Winner") markets are deliberately not ingested; see
    polymarket_cod_client's docstring."""
    markets = polymarket_cod_client.get_all_markets()
    winner_rows = markets["match_winner"]
    total_rows = markets["total_maps"]
    if not winner_rows and not total_rows:
        log.info("cod polymarket: no open markets")
        return

    matched = unmatched = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            # Resolve each event ONCE rather than per row -- a match-winner
            # event yields two rows and a totals event several, and they all
            # bind to the same fixture.
            match_id_by_slug: dict[str, int | None] = {}
            for row in winner_rows + total_rows:
                slug = row.get("event_slug")
                if slug in match_id_by_slug:
                    continue
                found = market_catalog_cod.find_match_for_polymarket_event(
                    session, row.get("event_title", ""), row.get("estimated_start_time"))
                match_id_by_slug[slug] = found.id if found else None
                if found:
                    matched += 1
                else:
                    unmatched += 1

            for row in winner_rows:
                market_catalog_cod.upsert_polymarket_cod_match_winner_row(
                    session, row, match_id_by_slug.get(row["event_slug"]))
            for row in total_rows:
                market_catalog_cod.upsert_polymarket_cod_total_row(
                    session, row, match_id_by_slug.get(row["event_slug"]))
            session.commit()
            log.info("cod polymarket: %d winner + %d total rows, %d/%d events bound",
                     len(winner_rows), len(total_rows), matched, matched + unmatched)
            if unmatched:
                log.warning(
                    "cod polymarket: %d event(s) matched NO fixture -- their markets stay "
                    "unpriced. An unbound market is invisible rather than wrong, which is "
                    "why this is logged rather than passed over.", unmatched)
        finally:
            session.close()


def run_full_refresh_cod():
    """Order matters even though each step is individually non-fatal:
    matches before markets (a market binds to a fixture), and ratings last so
    they see everything this pass ingested."""
    steps = (refresh_cod_matches, refresh_kalshi_cod_markets,
             refresh_polymarket_cod_markets, refresh_cod_ratings)
    for step in steps:
        try:
            step()
        except Exception:
            log.exception("cod refresh step %s failed; continuing", step.__name__)
