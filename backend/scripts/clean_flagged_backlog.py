"""Make the flagged backlog scannable: evict what will never be done, shorten the rest.

WHAT WENT WRONG (same day, my own doing). Notes were added to all 93 flagged
entries so nothing sat there unexplained -- but two mistakes made the result
worse to look at than the silence it replaced:

  1. 44 of the 93 were noted "NOT PRICEABLE" and LEFT FLAGGED. A permanent no
     does not belong in a list called "Flagged for future work"; it belongs in
     dismissed, with its reason attached. Nearly half the backlog was noise
     hiding the 19 entries that are actually actionable today.
  2. The notes averaged 466 characters. That is an essay per row, 93 rows deep.
     A backlog is read by scanning, so the note has to be a LABEL first and an
     explanation second.

THE FIX. Evict the permanent noes to dismissed (they keep a short reason, so a
future scan re-surfacing the series still inherits the decision), and rewrite
every remaining note to a single scannable line that leads with a status:

  READY   -- can be built now with models that already exist
  WAITING -- real, but gated on something outside our control or not yet done

Pass --apply to write. Default is a dry run.
"""
import sys
from collections import Counter

from app.db.database import SessionLocal
from app.db.models import CatalogEntry
from app.ingestion.poller_lock import db_write_lock

APPLY = "--apply" in sys.argv

# Old note prefix -> (new disposition, new short note).
# Order matters: longest/most specific prefix first.
_REWRITE = [
    ("Buildable from a model that ALREADY EXISTS", "flagged",
     "READY — this sport's season sim already prices this kind of question. "
     "Wiring job, no new model."),
    ("Buildable from the esports tournament sim", "flagged",
     "WAITING — the esports sim prices this but ships unvalidated (priced-not-staked). "
     "Needs validating against real brackets first."),
    ("Buildable -- this sport has a working game model", "flagged",
     "READY — NFL's half-market maths already exists; reuse it here. "
     "Must measure this sport's own half tie-rate first, not reuse NFL's."),
    ("Priceable in principle -- football-data.co.uk carries this league", "flagged",
     "WAITING on volume — the league drops into the existing football-data template, "
     "but Kalshi quotes it with ~0 trades (winner/spread at a ~0.95 spread)."),
    ("Priceable only via a SECOND data path", "flagged",
     "WAITING — not on football-data, so it needs the ESPN/MLS path (2nd ingestion route). "
     "Also ~0 volume. Do the football-data leagues first."),
    ("Investigated and REJECTED on supply", "flagged",
     "WAITING — non-UFC MMA supply measured too thin to be worth a pipeline. "
     "Revisit only if promotions start listing more fights."),
    # From the first triage pass, whose prefixes differ from the second's.
    ("Priceable in principle but on a DIFFERENT path", "flagged",
     "WAITING — not on football-data, so it needs the ESPN/MLS path (2nd ingestion route). "
     "Also ~0 volume. Do the football-data leagues first."),
    ("Genuinely deferrable, not dead. The racing engine", "flagged",
     "WAITING — the racing engine already models this shape (grid + finish). "
     "Needs a free MotoGP results/grid source, which we don't have."),
    ("Genuinely deferrable. The esports tournament sim", "flagged",
     "WAITING — the esports sim prices this but ships unvalidated (priced-not-staked). "
     "Needs validating against real brackets first."),
    ("Already built", "resolved",
     "Already built — markets under this series are live in the app. Flag was stale."),
    # The permanent noes leave the backlog entirely.
    ("NOT PRICEABLE -- data blocked. Soccer Elo here is PER-LEAGUE", "not_relevant",
     "Not priceable — cups/continental comps are cross-league and our soccer Elo is "
     "per-league. Needs a cross-league rating scale to ever work."),
    ("NOT PRICEABLE -- resolves on a VOTE", "not_relevant",
     "Not priceable — resolves on a vote, not on play. Nothing in box-score data "
     "can model it."),
    ("NOT PRICEABLE with what exists", "not_relevant",
     "Not priceable yet — needs a per-player projection model we don't have, and "
     "player-stat futures are tracking-only by policy."),
    ("NOT PRICEABLE, permanently", "not_relevant",
     "Not priceable — resolves on transfer/roster news, not a sporting outcome."),
]

s = SessionLocal()
rows = s.query(CatalogEntry).filter(CatalogEntry.disposition.in_(("flagged", "resolved"))).all()
print(f"entries in scope: {len(rows)}")

plan, unmatched = [], []
for e in rows:
    note = e.note or ""
    hit = next(((d, n) for p, d, n in _REWRITE if note.startswith(p)), None)
    if hit is None:
        unmatched.append(e)
    else:
        plan.append((e, hit[0], hit[1]))

moves = Counter((e.disposition, d) for e, d, _n in plan)
print("\ndisposition changes:")
for (old, new), n in moves.most_common():
    arrow = "  (unchanged)" if old == new else "  <-- leaves the backlog" if new != "flagged" else ""
    print(f"  {str(old):12} -> {new:14} {n:3}{arrow}")

# Count only entries that are flagged NOW and stay flagged. The unmatched rows
# are pre-existing `resolved` ones with no note -- already done, never shown in
# the backlog -- so adding them here overstated the after-figure.
after_flagged = (sum(1 for e, d, _n in plan if d == "flagged")
                 + sum(1 for e in unmatched if e.disposition == "flagged"))
print(f"\nflagged backlog: {sum(1 for e in rows if e.disposition == 'flagged')} -> {after_flagged}")
lens = [len(n) for _e, _d, n in plan]
print(f"note length: avg {sum(len(e.note or '') for e in rows)//max(len(rows),1)} -> {sum(lens)//max(len(lens),1)} chars")
if unmatched:
    print(f"\nUNMATCHED (left exactly as-is): {len(unmatched)}")
    for e in unmatched[:8]:
        print(f"  {e.identifier[:44]:46} {(e.note or '')[:60]}")

if not APPLY:
    print("\n(dry run -- pass --apply to write)")
    raise SystemExit

with db_write_lock():
    w = SessionLocal()
    try:
        for e, disposition, note in plan:
            row = w.get(CatalogEntry, e.id)
            row.disposition = disposition
            row.note = note
        w.commit()
        print(f"\nAPPLIED: rewrote {len(plan)} entries")
    finally:
        w.close()
