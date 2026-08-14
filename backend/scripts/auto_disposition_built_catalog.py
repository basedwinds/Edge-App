"""Close catalog entries whose series this app is ALREADY ingesting.

THE PROBLEM. The New Markets queue only ever grows. It was 167 this morning and
259 by the evening -- roughly 90 new entries a day -- and nothing removes an
entry when the thing it describes gets built. Today's CFB win totals are the
proof: the parser was fixed, 420 Polymarket markets now ingest and price, and
all 75 catalog rows still sat in the queue asking to be triaged. A queue that
cannot go down is not a work list, it is noise, and real finds hide in it.

THE JOIN. A CatalogEntry carries the platform's own identifier for the series;
an ingested Market carries the same value in source_event_id. If a catalog
entry's identifier appears on a live Market row, that series is by definition
already flowing into the app -- there is nothing left to decide about it.

    CatalogEntry.identifier  ==  Market.source_event_id   ->  disposition="built"

Measured at build time: 69 of 259 open entries match, essentially all of them
today's CFB win totals.

WHY THIS IS SAFE. It only ever marks entries whose markets DEMONSTRABLY exist in
this app's own database -- it cannot dismiss something unbuilt, because the
evidence for closing is the existence of the ingested row. It writes a note
saying which market proves it, so the call is auditable and reversible.

It DOES set dismissed=1, and must. /catalog/new filters on dismissed=0 alone and
ignores disposition entirely, so writing a disposition by itself leaves the row
sitting in New Markets looking untriaged -- which is exactly what happened on
the first run here: it reported the badge going 259 -> 190 while the tab never
moved off 259, because it counted its own predicate instead of the endpoint's.
Setting both together is precisely what the UI's own dismiss button does.

WHAT IT DELIBERATELY DOES NOT DO. It does not close an entry merely because a
market exists with a *similar* name, and it does not guess from the title. Both
are how a real find gets silently buried -- see the catalog-blindness and
stale-dismissal-note findings. Identifier equality or nothing.

Run: backend/.venv/Scripts/python.exe scripts/auto_disposition_built_catalog.py [--apply]
"""
import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import CatalogEntry, Market  # noqa: E402

NOTE = ("auto-closed: this app already ingests this series -- a live Market row "
        "carries the same platform identifier ({n} market rows, e.g. {ticker}). "
        "Nothing left to triage.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args()

    s = SessionLocal()
    try:
        by_event: dict[str, list[Market]] = collections.defaultdict(list)
        for m in s.query(Market).all():
            if m.source_event_id:
                by_event[m.source_event_id].append(m)

        open_entries = [r for r in s.query(CatalogEntry).all()
                        if not r.dismissed and not r.disposition]
        hits = [(r, by_event[r.identifier]) for r in open_entries
                if r.identifier and r.identifier in by_event]

        print(f"open catalog entries (the New Markets badge): {len(open_entries)}")
        print(f"  already ingested -> closable: {len(hits)}")
        print(f"  remaining after this pass  : {len(open_entries) - len(hits)}\n")

        by_sport = collections.Counter((r.sport or "?") for r, _ in hits)
        print("closable by sport:", dict(by_sport.most_common()))
        print("\nsample:")
        for r, mk in hits[:8]:
            print(f"   [{r.platform}] {(r.title or '')[:52]:52s} -> {len(mk)} live markets")

        if not args.apply:
            print(f"\nDRY RUN -- nothing written. Re-run with --apply.")
            return

        for r, mk in hits:
            # dismissed=1 IS WHAT CLEARS THE TAB. /catalog/new returns every row
            # with dismissed=0 regardless of disposition, so setting a
            # disposition alone leaves the entry sitting in New Markets looking
            # untriaged -- an earlier run of this script reported the badge
            # going 259 -> 190 while the tab never moved off 259, because it
            # counted its own predicate instead of the endpoint's. This mirrors
            # exactly what the UI's own dismiss button does: dismissed +
            # disposition + note, together.
            r.dismissed = 1
            r.disposition = "built"
            r.note = NOTE.format(n=len(mk), ticker=(mk[0].source_ticker or mk[0].id))
        s.commit()
        # THE ENDPOINT'S predicate, not ours: /catalog/new filters on
        # dismissed=0 alone. Reporting our own definition of "open" is how an
        # earlier run claimed 259 -> 190 while the tab stayed at 259.
        left = [r for r in s.query(CatalogEntry).all() if not r.dismissed]
        print(f"\nAPPLIED. New Markets badge: {len(open_entries)} -> {len(left)}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
