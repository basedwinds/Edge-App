"""Scheduled backfill for TennisMatch.surface on live-created matches.

Real gap this closes (2026-07-19, same day as the rest of this
investigation): only the 4 Grand Slams get a real surface at match-creation
time (see market_catalog_tennis.py::_infer_slam_attributes) -- every other
live match (the vast majority) ships with surface=None forever, meaning
elo_tennis.py's surface blend silently falls back to pure overall rating for
almost every real match. Real ATP/WTA tournaments have a known, stable
surface (unlike a live score, this doesn't change once a tournament starts),
and tennisexplorer_client.py::TennisExplorerClient.get_tournament_draw
already fetches it live for the tournament-winner futures bracket sim -- this
reuses that exact same call, just for a different purpose.

REAL LIMITATION found while building this, not assumed away: converting a
live match's own tournament text into a resolvable tennisexplorer slug is
much less reliable here than for the futures feature. Futures tournaments
come from Kalshi's own clean `competition` field ("ATP Bastad"); a live
match's own `tourney_name` is frequently Polymarket's raw event_title
instead, shaped "{City}: {Player A} vs {Player B}" (confirmed live for the
majority of currently-tracked matches) -- the city name has to be extracted
first, and even then, small Challenger-adjacent tour stops often don't
resolve to a real tennisexplorer slug at all under the same naive
lowercase-city convention that works for bigger/cleaner-named events
(confirmed live: Bastad/Umag/Gstaad/Palermo resolved; Zug/Tampere/Winnipeg/
Segovia/Prague did not, using the same conversion). This backfill is
therefore intentionally CONSERVATIVE: a resolution failure (including the
"[tournament]" placeholder page bug fixed in tennisexplorer_client.py) just
leaves surface=None untouched -- the same honest default as today, never a
guessed/wrong surface. Real coverage gain will be partial, concentrated on
tour-level events with well-known city names, not universal.
"""
import datetime
import logging
import re

from sqlalchemy.orm import Session

from app.clients.tennisexplorer_client import TennisExplorerClient
from app.db.database import SessionLocal
from app.db.models import TennisMatch
from app.ingestion.market_catalog_tennis import tournament_name_to_slug

log = logging.getLogger("tennis_surface_backfill")

_VS_SUFFIX_RE = re.compile(r"^(.*?):\s.+\svs\s.+$")


def _clean_tourney_name(raw: str) -> str:
    """Strips a Polymarket-style "{City}: {Player A} vs {Player B}" event
    title down to just the city/tournament part before the colon -- Kalshi's
    own `competition` text ("ATP Bastad") never matches this shape and is
    returned unchanged."""
    match = _VS_SUFFIX_RE.match(raw)
    return match.group(1).strip() if match else raw.strip()


def _tour_suffix(tour: str) -> str:
    return "atp-men" if tour == "atp" else "wta-women"


def run_tennis_surface_backfill(session: Session | None = None) -> dict:
    """Returns {"groups_tried": n, "resolved": n, "matches_updated": n} --
    callers besides the scheduler can inspect this directly. Only touches
    matches still missing a real surface AND not yet decided (winner_key is
    None) -- a finished match's surface no longer feeds any live prediction,
    not worth spending a real network request on.

    REAL BUG this fixes (caught live 2026-07-19, shortly after shipping the
    first version): the first cut held ONE SQLAlchemy session open across
    the ENTIRE function, including ~20 sequential tennisexplorer.com network
    round-trips in the loop below -- each one kept the session's pooled DB
    connection checked out the whole time (SQLAlchemy doesn't release it
    just because no query is currently running), starving the 5 concurrent
    per-sport pollers of the small shared pool (size 5 + overflow 10) and
    triggering real `QueuePool ... connection timed out` errors across the
    app, confirmed live in the actual server logs. Fixed by doing the two DB
    phases (read candidates, write results) in their OWN short-lived
    sessions, with ALL the slow network calls happening in between with NO
    DB session open at all."""
    stats = {"groups_tried": 0, "resolved": 0, "matches_updated": 0}

    read_session = session or SessionLocal()
    try:
        candidates = (
            read_session.query(TennisMatch)
            .filter(TennisMatch.source == "live", TennisMatch.surface.is_(None), TennisMatch.winner_key.is_(None))
            .all()
        )
        # Extract plain data now -- these ORM objects become unusable once
        # their session closes below (or gets reused by the caller).
        candidate_data = [(m.id, m.tourney_name, m.tour, m.match_date) for m in candidates]
    finally:
        if session is None:
            read_session.close()

    if not candidate_data:
        log.info("tennis surface backfill: no live matches missing surface")
        return stats

    groups: dict[tuple[str, str, int], list[int]] = {}
    for match_id, tourney_name, tour, match_date in candidate_data:
        if not tourney_name or not tour or not match_date:
            continue
        try:
            year = int(match_date[:4])
        except (TypeError, ValueError):
            year = datetime.date.today().year
        cleaned = _clean_tourney_name(tourney_name)
        groups.setdefault((cleaned, tour, year), []).append(match_id)

    resolved_surface_by_match_id: dict[int, str] = {}
    with TennisExplorerClient() as client:
        for (cleaned_name, tour, year), match_ids in groups.items():
            stats["groups_tried"] += 1
            slug = tournament_name_to_slug(cleaned_name)
            try:
                _rounds, surface = client.get_tournament_draw(slug, year, _tour_suffix(tour))
            except Exception:
                log.exception("tennis surface backfill: request failed for %r (slug=%s)", cleaned_name, slug)
                continue
            if surface is None:
                continue
            stats["resolved"] += 1
            for match_id in match_ids:
                resolved_surface_by_match_id[match_id] = surface

    if resolved_surface_by_match_id:
        write_session = session or SessionLocal()
        try:
            rows = (
                write_session.query(TennisMatch)
                .filter(TennisMatch.id.in_(resolved_surface_by_match_id.keys()))
                .all()
            )
            for m in rows:
                m.surface = resolved_surface_by_match_id[m.id]
                stats["matches_updated"] += 1
            write_session.commit()
        finally:
            if session is None:
                write_session.close()

    log.info(
        "tennis surface backfill: %d/%d tournament group(s) resolved, %d match row(s) updated",
        stats["resolved"], stats["groups_tried"], stats["matches_updated"],
    )
    return stats
