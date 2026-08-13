"""Restate every bet's stake_units against TODAY's unit size.

WHY. unit_dollars was $20 through 2026-08-03 and $10 from 2026-08-05, and each
bet froze its own stake_dollars while recording 1.0 unit either way. So a "unit"
meant two different amounts of money, and summing units across the change made
net-dollars and net-units disagree -- valorant showed -$19.96 next to +1.02u,
which reads as a broken page.

WHAT THIS CHANGES AND WHAT IT DOES NOT. Only stake_units is rewritten:

    stake_units = stake_dollars / current unit_dollars

stake_dollars is NEVER touched, so realized P/L in money is bit-for-bit
unchanged -- this cannot alter what actually happened to the bankroll. After it
runs, $/unit is uniform, so net_units * unit_dollars == net_profit_dollars by
construction and the two figures can no longer disagree in sign.

NO INFORMATION IS LOST. "How many units was this by the standard of its own day"
is still recoverable: stake_dollars is intact and the placed_at date tells you
the unit size then in force ($20 before 2026-08-04, $10 after).

IDEMPOTENT, and re-run it after any future unit_dollars change -- otherwise the
same split reappears. Nothing schedules it, deliberately: silently rewriting
historical rows on every poll is worse than a stale figure.
"""
import argparse
import collections
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.api.routers.settings import get_unit_dollars
from app.db.database import SessionLocal
from app.db.models import PlacedBet


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args()

    s = SessionLocal()
    unit = get_unit_dollars(s)
    if not unit or unit <= 0:
        print("no usable unit_dollars; refusing")
        return
    rows = s.query(PlacedBet).all()
    before_sizes = collections.Counter()
    changed, unchanged, skipped = 0, 0, 0
    examples = []
    for b in rows:
        if not b.stake_dollars:
            skipped += 1          # zero-stake observation rows carry no units
            continue
        if b.stake_units:
            before_sizes[round(b.stake_dollars / b.stake_units, 2)] += 1
        want = round(b.stake_dollars / unit, 6)
        if b.stake_units is not None and abs(b.stake_units - want) < 1e-9:
            unchanged += 1
            continue
        if len(examples) < 6:
            examples.append(f"  bet {b.id}: ${b.stake_dollars} {b.stake_units}u -> {want}u")
        if args.apply:
            b.stake_units = want
        changed += 1

    print(f"current unit_dollars = ${unit}")
    print(f"$/unit BEFORE: {dict(sorted(before_sizes.items()))}")
    print(f"rows: {len(rows)}   to change: {changed}   already correct: {unchanged}   "
          f"no stake (skipped): {skipped}")
    for e in examples:
        print(e)

    if args.apply:
        s.commit()
        after = collections.Counter()
        for b in s.query(PlacedBet).all():
            if b.stake_dollars and b.stake_units:
                after[round(b.stake_dollars / b.stake_units, 2)] += 1
        print(f"\nAPPLIED. $/unit AFTER: {dict(sorted(after.items()))}")
        print("  (a single key means net_units * unit_dollars == net_profit_dollars)")
    else:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
    s.close()


if __name__ == "__main__":
    main()
