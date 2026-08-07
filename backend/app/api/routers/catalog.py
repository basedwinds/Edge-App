from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.db.models import CatalogEntry
import datetime

from app.ingestion.catalog_classify import classify, is_auto_priceable

router = APIRouter(prefix="/catalog", tags=["catalog"])

# Newest batch first, then alphabetical WITHIN a day -- not raw first_seen desc.
# first_seen is microsecond-precise, so a scan that adds many rows at once
# orders them by insertion, and insertion follows Kalshi's /series response,
# which comes back in arbitrary order (confirmed live 2026-08-02, NOT sorted).
# That scattered a single scan's near-identical series all over the list: the
# 24 Brasileiro series, the 8 WBC boxing title series and the ~40 WNBA prop
# series each landed in 558 unrelated places, so triaging one market type
# meant finding its siblings by eye. Truncating the sort key to the DAY keeps
# "newest first" meaningful for the steady-state trickle (a handful of rows,
# and days apart) while making a bulk backlog skimmable, since one decision
# then covers a contiguous run of rows.
_CATALOG_ORDER = (func.date(CatalogEntry.first_seen).desc(), CatalogEntry.identifier.asc())


class CatalogEntryOut(BaseModel):
    id: int
    platform: str
    identifier: str
    title: str
    sport: str
    first_seen: str
    # First-pass auto-triage (see catalog_classify.py) so the New Markets page
    # can bucket each entry without the user opening it. auto_priceable is the
    # only "an existing model could handle this" verdict, reserved for clean
    # head-to-head outcomes; everything else needs review / a model that
    # doesn't exist yet.
    category: str
    category_note: str
    auto_priceable: bool
    # The human's own reason, distinct from `category_note` (which is the
    # auto-classifier's guess). Only this one records a DECISION -- what was
    # concluded and what would unblock it. See CatalogEntry.note.
    note: str | None = None
    disposition: str | None = None
    # WHAT this entry is waiting on, derived from `note` -- "volume",
    # "needs-model", "measured-too-weak", "ready", etc. Lets the backlog be read
    # by blocker instead of as 40 paragraphs: 26 of the current 40 are the same
    # "waiting on volume" story and collapse into one line. See
    # catalog_resolution.classify_blocker.
    blocker: str | None = None

    class Config:
        from_attributes = True


def _to_out(r: CatalogEntry) -> CatalogEntryOut:
    from app.models.catalog_resolution import classify_blocker

    category, note = classify(r.identifier, r.title, r.sport)
    return CatalogEntryOut(
        id=r.id, platform=r.platform, identifier=r.identifier, title=r.title,
        sport=r.sport, first_seen=r.first_seen.isoformat(),
        category=category, category_note=note, auto_priceable=is_auto_priceable(category),
        note=r.note, disposition=r.disposition,
        blocker=classify_blocker(r.note),
    )


class DismissIn(BaseModel):
    # "not_relevant": reviewed, nothing to build -- same as the old
    # single-button behavior, just recorded explicitly now instead of
    # leaving disposition null.
    # "flagged": reviewed AND worth building -- this app has no code-
    # generation capability and never auto-ingests an unreviewed market
    # (see the standing "never guess a number" rule), so this does NOT
    # auto-build ingestion. It keeps the entry in a persistent /catalog/
    # flagged backlog instead of letting it vanish the moment it's
    # dismissed, so a real "worth doing" decision doesn't depend on
    # someone remembering it later.
    disposition: str = "not_relevant"
    # Optional free-text reason, stored on the entry. Worth writing for BOTH
    # dispositions: on a flag it says what would unblock the build, and on a
    # not_relevant it says why, so a later scan re-surfacing the same series
    # does not restart the analysis from nothing.
    note: str | None = None


@router.get("/new", response_model=list[CatalogEntryOut])
def list_new_entries(session: Session = Depends(get_session)):
    rows = (
        session.query(CatalogEntry)
        .filter_by(dismissed=0)
        .order_by(*_CATALOG_ORDER)
        .all()
    )
    return [_to_out(r) for r in rows]


@router.get("/flagged", response_model=list[CatalogEntryOut])
def list_flagged_entries(session: Session = Depends(get_session)):
    """Persistent backlog of entries marked "worth building" on dismiss --
    survives independently of the dismissed=0/1 "new" list so a real
    to-do doesn't get lost the moment it's reviewed. Resolved via
    POST /catalog/{id}/resolve once actually built (or decided against)."""
    rows = (
        session.query(CatalogEntry)
        .filter_by(disposition="flagged")
        .order_by(*_CATALOG_ORDER)
        .all()
    )
    return [_to_out(r) for r in rows]


@router.post("/{entry_id}/dismiss")
def dismiss_entry(entry_id: int, body: DismissIn = DismissIn(), session: Session = Depends(get_session)):
    row = session.get(CatalogEntry, entry_id)
    if row is not None:
        row.dismissed = 1
        row.disposition = body.disposition
        # ALWAYS leave a trace, even on a one-click dismissal. DismissIn's own
        # docstring says a reason is "worth writing for BOTH dispositions ... so
        # a later scan re-surfacing the same series does not restart the
        # analysis from nothing" -- and then makes it optional, so 1,469 of the
        # 1,530 not_relevant entries carry no reason at all (measured
        # 2026-08-07).
        #
        # Boxing is the case that proved the cost. KXBOXING/KXBOXINGDISTANCE/
        # KXBOXINGMOV were dismissed as not_relevant with no note, so months
        # later there was no way to tell "evaluated and rejected on the merits"
        # from "swept away in a bulk triage" -- and the whole analysis had to be
        # redone from scratch (supply re-probed, ESPN checked for a boxing feed,
        # BoxRec found Cloudflare-gated) only to land somewhere the original
        # dismissal may well have already reached.
        #
        # A synthesised stamp is worth far less than a real reason, but it is
        # not nothing: it records WHEN the call was made and that no reason was
        # given, which distinguishes a deliberate silent dismissal from an entry
        # nobody has looked at. Never overwrites a real note.
        if body.note:
            row.note = body.note
        elif not (row.note or "").strip():
            stamp = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            row.note = (f"Dismissed {stamp} as {body.disposition} with no reason recorded. "
                        f"Series was live enough to be surfaced by the scan at the time "
                        f"(first seen {str(row.first_seen)[:10]}).")
        session.commit()
    return {"status": "ok"}


@router.post("/{entry_id}/resolve")
def resolve_flagged_entry(entry_id: int, session: Session = Depends(get_session)):
    """Clears a flagged entry's backlog status once it's actually been
    built (or the user's decided against it after all) -- doesn't touch
    `dismissed` (already 1 since the moment it was flagged)."""
    row = session.get(CatalogEntry, entry_id)
    if row is not None and row.disposition == "flagged":
        row.disposition = "resolved"
        session.commit()
    return {"status": "ok"}
