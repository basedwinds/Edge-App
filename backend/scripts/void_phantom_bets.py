"""Void PAPER bets that were booked against a phantom 0.500 price.

WHY. `_implied_prob` used to fall through to Polymarket's SEEDED last_price on
markets with no book at all. When that seed was exactly 0.500 the app recorded
it as the market's price, and a confident model scored against it booked a
~30pp edge that never existed. The pricing bug is fixed (see markets.py), but
every bet ALREADY logged at that fake price is still in the book: the settled
ones inflate past ROI, and the pending ones will do the same as they settle.

A void contributes 0.0 profit and is excluded from the ROI denominator
(placed_bets.py), so voiding is exactly "this was never a valid observation"
rather than "this lost".

SAFETY: only ever touches paper == True. Real bets are left completely alone --
they are the user's actual record, and only one was ever phantom-priced anyway.

Dry run by default; pass --apply to write.
"""
import os
import sqlite3
import sys
from collections import defaultdict

DB = os.path.join(os.environ["LOCALAPPDATA"], "nfl-edge-app", "app.db")
APPLY = "--apply" in sys.argv
NOTE = "voided: booked against a phantom 0.500 (market had no book; Polymarket seeded last_price)"


def roi(rows):
    """Flat-unit ROI so paper bets with no dollar stake still count."""
    num = den = 0.0
    for status, p in rows:
        if not p or p <= 0 or p >= 1:
            continue
        den += 1.0
        num += (1.0 / p - 1.0) if status == "won" else -1.0
    return (100.0 * num / den) if den else None


def main():
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT id, sport, status, paper, market_prob_at_placement "
        "FROM placed_bets WHERE status IN ('pending','won','lost')"
    ).fetchall()

    targets, by_sport = [], defaultdict(lambda: {"keep": [], "drop": []})
    for _id, sport, status, paper, p in rows:
        phantom = p is not None and abs(p - 0.5) < 1e-9
        if phantom and paper:
            targets.append(_id)
            if status in ("won", "lost"):
                by_sport[sport]["drop"].append((status, p))
        elif status in ("won", "lost"):
            by_sport[sport]["keep"].append((status, p))

    print(f"paper bets booked at a phantom 0.500: {len(targets)}")
    print(f"  (real bets are never touched by this script)\n")
    print(f"{'sport':10} {'settled kept':>13} {'ROI kept':>9} {'settled voided':>15} {'ROI before':>11}")
    for sport, d in sorted(by_sport.items(), key=lambda x: -len(x[1]["keep"])):
        if not d["keep"] and not d["drop"]:
            continue
        before = roi(d["keep"] + d["drop"])
        after = roi(d["keep"])
        print(
            f"{sport:10} {len(d['keep']):13} "
            f"{(f'{after:+.1f}%' if after is not None else '-'):>9} "
            f"{len(d['drop']):15} "
            f"{(f'{before:+.1f}%' if before is not None else '-'):>11}"
        )

    if APPLY and targets:
        c.executemany(
            "UPDATE placed_bets SET status='void', settlement_note=? WHERE id=?",
            [(NOTE, i) for i in targets],
        )
        c.commit()
        print(f"\nAPPLIED: voided {len(targets)} paper bets")
    elif not APPLY:
        print("\n(dry run -- pass --apply to write)")


if __name__ == "__main__":
    main()
