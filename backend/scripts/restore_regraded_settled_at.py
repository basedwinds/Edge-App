"""Restore settled_at on the bets the 2026-08-06 re-grade stamped with "now".

THE MISTAKE. regrade_mistyped_tennis_spreads.py corrected the OUTCOME of 59
tennis bets that the stale-market_type bug had routed to the wrong grader. It
also set settled_at = now, which was wrong: a re-grade changes what the result
WAS, not when the match settled. A bet placed 2026-07-24 on a match played
2026-07-23 ended up reading "settled 2026-08-06".

WHY THAT MATTERED. The Bet Tracker sorts by settle date, so all 59 jumped to the
top as if they had just resolved -- which is how it surfaced (user report: "I'm
seeing a bet settled for Alevtina Ibragimova vs Clara Burel for Aug 6th but this
match took place the 24th of July, and I don't recall marking this bet as
placed"). They were genuinely their bets, placed weeks earlier; only the settle
timestamp lied, and that made them look new.

THE RECONSTRUCTION. The original settled_at was overwritten and not recorded, so
it is recovered from SIBLING bets: other bets on the SAME tennis match that the
re-grade did not touch, and that were settled before the re-grade ran. Those were
graded by the original pass, in the same run, so their timestamp is the real one.
Verified available for all 59 before this was written.

Bets whose status the re-grade legitimately changed KEEP the corrected status --
only the timestamp is repaired. Pass --apply to write; default is a dry run.
"""
import sys
from collections import Counter

from app.db.database import SessionLocal
from app.db.models import PlacedBet
from app.ingestion.poller_lock import db_write_lock

APPLY = "--apply" in sys.argv
_NOTE = "re-graded from Polymarket resolution%"
# The re-grade ran on this date; any settled_at on it is the stamp to replace,
# and any sibling settled ON it is not trustworthy as a reference.
_REGRADE_DAY = "2026-08-06"

s = SessionLocal()
targets = s.query(PlacedBet).filter(PlacedBet.settlement_note.like(_NOTE)).all()
print(f"re-graded bets: {len(targets)}")

plan, unresolved = [], []
for b in targets:
    sibs = []
    if b.tennis_match_id:
        sibs = [
            x for x in s.query(PlacedBet).filter(
                PlacedBet.tennis_match_id == b.tennis_match_id,
                PlacedBet.id != b.id,
                PlacedBet.settled_at.isnot(None),
            ).all()
            if not (x.settlement_note or "").startswith("re-graded from Polymarket")
            and str(x.settled_at)[:10] != _REGRADE_DAY
        ]
    if not sibs:
        unresolved.append(b)
        continue
    # Earliest sibling: the original grading pass settled a match's bets together,
    # so the earliest is the moment that pass reached this match.
    plan.append((b, min(x.settled_at for x in sibs)))

print(f"  recoverable from an un-regraded sibling: {len(plan)}")
print(f"  NOT recoverable (left alone, not guessed): {len(unresolved)}")
if plan:
    print(f"\n  restored settle dates: {dict(Counter(str(ts)[:10] for _b, ts in plan))}")
    print("\n  sample:")
    for b, ts in plan[:6]:
        print(f"    bet {b.id} ({'REAL' if not b.paper else 'paper'}) {b.team}: "
              f"{str(b.settled_at)[:19]} -> {str(ts)[:19]}  (placed {str(b.placed_at)[:10]})")
    real = [(b, ts) for b, ts in plan if not b.paper]
    if real:
        print("\n  REAL-money bets:")
        for b, ts in real:
            print(f"    bet {b.id} {b.team}: settled_at {str(b.settled_at)[:19]} -> {str(ts)[:19]}")

if not APPLY:
    print("\n(dry run -- pass --apply to write)")
    raise SystemExit

with db_write_lock():
    w = SessionLocal()
    try:
        n = 0
        for b, ts in plan:
            row = w.get(PlacedBet, b.id)
            if row is None:
                continue
            row.settled_at = ts
            n += 1
        w.commit()
        print(f"\nAPPLIED: restored settled_at on {n} bets")
    finally:
        w.close()
