from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.db.models import CatalogEntry
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

    class Config:
        from_attributes = True


def _to_out(r: CatalogEntry) -> CatalogEntryOut:
    category, note = classify(r.identifier, r.title, r.sport)
    return CatalogEntryOut(
        id=r.id, platform=r.platform, identifier=r.identifier, title=r.title,
        sport=r.sport, first_seen=r.first_seen.isoformat(),
        category=category, category_note=note, auto_priceable=is_auto_priceable(category),
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
