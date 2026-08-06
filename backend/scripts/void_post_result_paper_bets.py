"""Void paper bets the logger created AFTER their event had already finished.

These are not observations. The paper logger used to gate on the PENDING set, so
a settled market became loggable again; the next run logged a fresh bet, the
settler graded it seconds later off an already-known result, and the loop ran
every poll. See paper_logger's own comment for the full story.

IDENTIFYING THEM WITHOUT GUESSING. The first attempt used a lifetime threshold
-- settled within 30 minutes of being logged -- which caught the right rows but
rested on an arbitrary number, and would also have swept up a legitimately
late-logged bet on a nearly-finished match. Checking the candidates showed a far
cleaner signal: 200/200 sampled F1 and WNBA rows, and 43/49 tennis, sat on a
market that ALREADY had another paper bet.

So the rule is duplication itself, with no threshold at all: keep the EARLIEST
paper bet per market_id and void the rest. That is exactly the set the fixed
guard would never have created, it needs no judgement about how fast is too
fast, and it leaves every market's original observation intact.

Voided rather than deleted, matching every other cleanup here: the row stays
auditable, but it leaves won/lost so it cannot inflate a win rate or an ROI.
Real-money bets are never touched.

Pass --apply to write. Default is a dry run.
"""
import sys
from collections import Counter

from app.db.database import SessionLocal
from app.db.models import PlacedBet
from app.ingestion.poller_lock import db_write_lock

APPLY = "--apply" in sys.argv

s = SessionLocal()
by_market: dict = {}
for b in s.query(PlacedBet).filter(PlacedBet.paper == True).all():  # noqa: E712
    by_market.setdefault(b.market_id, []).append(b)

rows = []
for mid, bets in by_market.items():
    if len(bets) < 2:
        continue
    # Keep the earliest -- that is the one real observation. Ties broken by id
    # so the choice is deterministic across runs.
    bets.sort(key=lambda b: (b.placed_at or "", b.id))
    rows.extend(b for b in bets[1:] if b.status in ("won", "lost"))

print(f"markets with more than one paper bet: {sum(1 for v in by_market.values() if len(v) > 1)}")
print(f"duplicate settled paper bets (all but the earliest per market): {len(rows)}")
print(f"  by sport: {dict(Counter(b.sport for b in rows).most_common())}")
print(f"  by market_type: {dict(Counter(b.market_type for b in rows).most_common(8))}")
print(f"  outcomes: {dict(Counter(b.status for b in rows))}")
print(f"  REAL-money among them (must be 0): {sum(1 for b in rows if not b.paper)}")

alive = sorted((b.settled_at - b.placed_at).total_seconds() / 60 for b in rows)
if alive:
    print(f"  lifetime min/median/max: {alive[0]:.1f} / {alive[len(alive)//2]:.1f} / {alive[-1]:.1f} min")

# What the sample looks like once they're gone.
staked = [b for b in rows if (b.stake_dollars or 0) > 0]
print(f"  of these, carrying a stake: {len(staked)}")

if not APPLY:
    print("\n(dry run -- pass --apply to write)")
    raise SystemExit

with db_write_lock():
    w = SessionLocal()
    try:
        n = 0
        for b in rows:
            row = w.get(PlacedBet, b.id)
            if row is None or row.status not in ("won", "lost") or not row.paper:
                continue
            row.status = "void"
            row.settlement_note = (
                "voided: duplicate paper bet on a market that already had one. The paper "
                "logger re-logged settled markets every poll (see its own comment), so these "
                "were logged against an already-known result. Not observations -- excluded so "
                "they cannot inflate a win rate or ROI. The earliest bet per market is kept."
            )
            n += 1
        w.commit()
        print(f"\nAPPLIED: voided {n} post-result paper bets")
    finally:
        w.close()
