"""Rescale $20-era bets down to the $10 unit, so the whole history is one flat
stake size.

WHAT THIS DOES, AND THE APPROACH IT REPLACES.

  the rejected approach   keep the money, restate the UNITS ($20 bet -> 2 units
  (scripts/normalize_     of $10). Preserves realized P/L exactly -- but then an
   stake_units.py,        old bet counts DOUBLE in the unit record, which is not
   deleted with this)     what a flat-staking history means. The user's words:
                          "I don't want me to have taken 2 unit bets back then
                          influencing the smaller unit size now."

  this script             scale the old STAKES down instead ($20 bet -> $10,
                          still 1 unit). Every bet in the history is now the same
                          size, so nothing from the larger-unit era outweighs
                          anything from the current one.

THIS IS THE GENERAL TOOL for any future unit_dollars change: set CUTOVER to the
instant the size changed and factor to new/old, and the pre-cutover era is
restated onto the new size.

IT CHANGES THE REPORTED P/L, DELIBERATELY. Halving an old stake halves its
winnings: the record moves from +$1,079.19 (what the money actually did) to
+$836.19 (what the same picks make at a flat $10). That is the requested
counterfactual, not a correction of an error -- the original figure was right
about the money.

THE ERA CUTOVER IS MEASURED, NOT ASSUMED. stake_dollars was never rewritten by
either script, so the changeover is still visible in the raw amounts:

    last $20 stake   2026-08-04 22:52:34
    first  $5 stake  2026-08-04 22:57:36

so the unit size changed between those two instants. Bets before the cutover are
$20-era and are halved; bets after are already $10-era and are left alone. On the
changeover DAY this matters: 55 of that day's $10 stakes precede the last $20 (so
they were half-units of $20 and become $5) and 22 follow it (already full $10
units, untouched). Zero REAL bets are ambiguous.

REVERSIBLE: multiply the same pre-cutover rows by 2.
"""
import argparse
import collections
import datetime
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.db.database import SessionLocal
from app.db.models import PlacedBet

# Between the last $20 stake (22:52:34) and the first $5 stake (22:57:36).
CUTOVER = datetime.datetime(2026, 8, 4, 22, 55, 0)
LEGACY_UNIT, CURRENT_UNIT = 20.0, 10.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true", help="multiply the same rows back up")
    args = ap.parse_args()
    factor = 2.0 if args.undo else 0.5

    s = SessionLocal()
    rows = s.query(PlacedBet).all()
    changed = 0
    sizes_before, sizes_after = collections.Counter(), collections.Counter()
    examples = []
    for b in rows:
        if not b.stake_dollars or not b.placed_at or b.placed_at >= CUTOVER:
            if b.stake_dollars:
                sizes_before[b.stake_dollars] += 1
                sizes_after[b.stake_dollars] += 1
            continue
        sizes_before[b.stake_dollars] += 1
        nd = round(b.stake_dollars * factor, 4)
        nu = round(b.stake_units * factor, 6) if b.stake_units else b.stake_units
        sizes_after[nd] += 1
        if len(examples) < 5:
            examples.append(f"  bet {b.id} {str(b.placed_at)[:16]}: "
                            f"${b.stake_dollars}/{b.stake_units}u -> ${nd}/{nu}u")
        if args.apply:
            b.stake_dollars, b.stake_units = nd, nu
        changed += 1

    print(f"cutover {CUTOVER}   mode={'UNDO' if args.undo else 'RESCALE'}")
    print(f"rows: {len(rows)}   pre-cutover with a stake (to change): {changed}")
    print(f"stake_dollars BEFORE: {dict(sorted(sizes_before.items()))}")
    print(f"stake_dollars AFTER : {dict(sorted(sizes_after.items()))}")
    for e in examples:
        print(e)

    if args.apply:
        s.commit()
        print("\nAPPLIED.")
    else:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
    s.close()


if __name__ == "__main__":
    main()
