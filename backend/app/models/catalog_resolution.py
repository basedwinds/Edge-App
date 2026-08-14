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


# ---------------------------------------------------------------------------
# Untriaged entries whose series is ALREADY being ingested.
# ---------------------------------------------------------------------------
#
# auto_resolve_flagged above closes entries a human already FLAGGED to build.
# This closes the ones nobody has looked at yet, and it is what stops the New
# Markets queue growing without bound: 167 entries on the morning of 2026-08-13
# and 259 by that evening, roughly 90 a day, with nothing ever removing one.
#
# Today's CFB win totals are the proof. The parser was fixed, 420 Polymarket
# markets began ingesting and pricing, and all 75 catalog rows still sat in the
# queue asking to be triaged. A queue that cannot go down is not a work list, it
# is noise, and real finds hide in it.
#
# THIS CLOSES THE POLYMARKET GAP THIS MODULE'S OWN DOCSTRING DOCUMENTS AS
# IMPOSSIBLE. That note is right about source_ticker -- a Polymarket ticker is a
# conditionId and can never prefix-match an event slug. But the ingested Market
# row ALSO stores source_event_id, which IS the event slug, and joins to the
# catalog identifier exactly. All 69 entries this closed on its first run were
# Polymarket, i.e. precisely the ones the ticker route cannot see.
#
# SAFE BY CONSTRUCTION: the evidence for closing an entry is the existence of
# this app's own ingested Market rows for it, so it cannot close something
# unbuilt. It never touches `dismissed`, so a human rejection stands, and it
# records which market proved the call.
#
# Identifier equality ONLY -- never a title or fuzzy match. Closing an entry on
# a similar-looking name is how a real find gets silently buried, which is the
# failure mode behind both the catalog-blindness and stale-dismissal findings.
def auto_close_ingested(session: Session, apply: bool = True) -> dict:
    """Close untriaged catalog entries whose series already has live markets."""
    from app.db.models import Market

    by_event: dict[str, int] = {}
    example: dict[str, str] = {}
    for market_id, event_id, ticker in session.query(
            Market.id, Market.source_event_id, Market.source_ticker).all():
        if not event_id:
            continue
        by_event[event_id] = by_event.get(event_id, 0) + 1
        example.setdefault(event_id, str(ticker or market_id))

    open_entries = [e for e in session.query(CatalogEntry).all()
                    if not e.dismissed and not e.disposition]
    closed: list[str] = []
    for entry in open_entries:
        n = by_event.get(entry.identifier or "")
        if not n:
            continue
        closed.append(f"{entry.identifier} ({entry.title})")
        if apply:
            entry.disposition = "built"
            entry.note = (
                f"AUTO-CLOSED: this app already ingests this series -- {n} live market rows "
                f"carry the same platform identifier (e.g. {example.get(entry.identifier)}). "
                f"Nothing left to triage."
            )
    if apply and closed:
        session.commit()
        log.info("catalog auto-close: closed %d untriaged entries already ingested", len(closed))
    return {"closed": closed, "open_before": len(open_entries),
            "open_after": len(open_entries) - len(closed)}
