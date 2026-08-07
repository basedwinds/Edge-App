"""Close flagged catalog entries automatically once their blocker is gone, and
classify the ones still blocked by WHAT they are waiting on.

WHY. "Flag to build" entries were only ever cleared by hand, so the backlog
rotted: on 2026-08-07 EIGHT of 48 flagged entries described work that was
already finished and live -- NFL playoff seed/host, four WNBA bracket markets,
NASCAR's champion -- still carrying notes like "no WNBA playoff bracket model
exists" and "NASCAR is excluded ON PURPOSE". A backlog that lies about what is
left is worse than no backlog, and this is the second time the list has had to
be hand-cleaned for being unreadable.

THE RESOLVE SIGNAL is deliberately narrow: a Kalshi series counts as built when
this app is ACTIVELY INGESTING it, i.e. it has active Market rows. That is
exactly what "flagged to build" asks for, and it is a DB-only check so this can
run inside a scheduled job with no self-HTTP (see app/shutdown.py for why a job
that self-HTTPs is a hazard).

TWO TRAPS, both of which this codebase has already been bitten by once:

  * Kalshi series names NEST. Matching "KXWNBA%" pulls in KXWNBAGAME, KXWNBAWINS
    and every other WNBA series -- 362 active markets, none of them the
    championship. It would have resolved the WNBA Championship entry off
    unrelated game markets. Matching "KXWNBA-%" gives the real 15. Always match
    the SERIES-ticker form.
  * Polymarket entries CANNOT be resolved this way at all. Their catalog
    identifier is an event SLUG ("wnba-2026-champion-464") while the stored
    source_ticker is a conditionId ("0x" + 64 hex), so a prefix check can never
    match and would silently report every Polymarket entry as still-blocked
    forever. They are reported as "needs a human" rather than quietly ignored.
"""
from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import CatalogEntry, Market

log = logging.getLogger("catalog_resolution")

# What a flagged entry is waiting on, read from the note the triage wrote.
# Ordered: the first match wins, so put the more specific phrases first.
_BLOCKER_RULES = (
    ("volume", ("waiting on volume", "~0 trades", "0 volume")),
    ("second-ingestion-route", ("not on football-data", "espn/mls path")),
    ("needs-data-source", ("needs a free", "no free", "we don't have")),
    ("measured-too-weak", ("measured and too weak", "too weak, not unbuilt", "rejected on supply", "too thin")),
    ("needs-validation", ("ships unvalidated", "needs validating")),
    ("needs-model", ("needs the", "no baseline", "model exists", "needs an")),
    ("ingestion-only", ("blocker is ingestion",)),
    ("ready", ("ready --", "ready:")),
)


def classify_blocker(note: str | None) -> str:
    """A short machine-readable reason a flagged entry is still open, derived
    from its written note. "unclassified" when the note doesn't say -- which is
    itself worth surfacing, since it means the triage note was too vague to act
    on later."""
    text = (note or "").lower().strip()
    if not text:
        return "unclassified"
    # Checked before the needle rules: a note that OPENS with READY is the
    # triage's own "nothing is blocking this any more" verdict and outranks any
    # phrase later in the same note describing what the remaining work is.
    # (Matching on "ready --" missed these, because the notes use an em dash.)
    if text.startswith("ready"):
        return "ready"
    for label, needles in _BLOCKER_RULES:
        if any(n in text for n in needles):
            return label
    return "unclassified"


def series_is_live(session: Session, entry: CatalogEntry) -> int | None:
    """Active Market rows this catalog entry's series is producing.

    None means "cannot be determined from the identifier" -- Polymarket, whose
    identifier is a slug and whose stored ticker is a conditionId. Never
    confuse that with 0.
    """
    if entry.platform != "kalshi":
        return None
    return (
        session.query(func.count(Market.id))
        .filter(
            Market.source == "kalshi",
            # SERIES-%, not SERIES% -- see the module docstring's nesting trap.
            Market.source_ticker.like(f"{entry.identifier}-%"),
            Market.status == "active",
        )
        .scalar()
    ) or 0


def auto_resolve_flagged(session: Session, apply: bool = True) -> dict:
    """Mark every flagged entry resolved whose series is now being ingested.

    Returns a summary dict: what was resolved, what is still blocked and on
    what, and what could not be checked automatically.
    """
    flagged = session.query(CatalogEntry).filter(CatalogEntry.disposition == "flagged").all()
    resolved: list[str] = []
    blocked: dict[str, list[str]] = {}
    unverifiable: list[str] = []

    for entry in flagged:
        live = series_is_live(session, entry)
        if live is None:
            unverifiable.append(f"{entry.identifier} ({entry.title})")
            continue
        if live > 0:
            resolved.append(f"{entry.identifier} ({entry.title}) -- {live} active markets")
            if apply:
                entry.disposition = "resolved"
                entry.note = (
                    f"AUTO-RESOLVED: this app is now ingesting {live} active markets for this "
                    f"series, so the build this was flagged for is done. Previous note: {entry.note or '(none)'}"
                )
            continue
        blocked.setdefault(classify_blocker(entry.note), []).append(f"{entry.identifier} ({entry.title})")

    if apply and resolved:
        session.commit()
        log.info("catalog auto-resolve: closed %d flagged entries", len(resolved))
    return {"resolved": resolved, "blocked": blocked, "unverifiable": unverifiable}
