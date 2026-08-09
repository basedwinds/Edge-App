"""Give every registered sport an equal share of the bankroll, and stop.

WHY THIS EXISTS. Adding a sport should not need a conversation about
percentages. The intent is simply: every sport this app has built gets an equal
slot, the combined total stays under a safe ceiling, and a new sport slots into
the grid rather than being bolted on at whatever number was left over.

That is exactly what went wrong before Call of Duty: the allocations had drifted
into a 6.4 / 6.0 / 4.0 mix nobody chose deliberately, and CoD opened on the
leftover 4%.

RUN THIS AFTER ADDING A SPORT. It reads app.sports (the registry), so a sport is
included the moment it is registered -- there is no second list to remember.

EQUAL, NOT WEIGHTED, ON PURPOSE. The real bet tracker's per-sport confidence
intervals all span zero across 146 settled bets, so any ranking would be fitting
noise. Equal is the only split the evidence supports.

WHAT THIS DOES NOT TOUCH, because they are the real risk controls and are
already correct:

    TOTAL_EXPOSURE_CAP_PCT              0.60   combined game + futures ceiling
    DEFAULT_GAME_EXPOSURE_CAP_PCT       0.40
    DEFAULT_FUTURES_EXPOSURE_CAP_PCT    0.20
    DEFAULT_GAME_PER_SPORT_CAP_FRACTION 0.25   -> no sport can take the game side
    DEFAULT_FUTURES_PER_SPORT_CAP_FRACTION 0.25  -> nor the futures side

Those cap what can be HELD. The per-sport percentages below only divide the
nominal pools. The 60% ceiling is what actually keeps the bankroll safe.

HEADROOM. At 6% each the guard (which scales everything down once allocations
exceed 100%) stays dormant until the 17th sport. Below that, adding a sport is
genuinely free -- the total grows but never crosses the line where pools start
shrinking. This script reports where that line is each time it runs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routers import settings as S  # noqa: E402
from app.db.database import SessionLocal  # noqa: E402

#: Keep the combined nominal allocation at or under this. Not a risk control --
#: the 60% exposure ceiling is -- but past 100% the proportional guard engages
#: and every sport's pool silently shrinks, which is confusing rather than safe.
TARGET_MAX_TOTAL = 1.00


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="apply the change (default is a dry run)")
    ap.add_argument("--pct", type=float, default=None,
                    help="explicit per-sport percentage, e.g. 0.06. "
                         "Default keeps the CURRENT total and divides it evenly.")
    args = ap.parse_args()

    with SessionLocal() as session:
        keys = S._ALL_ALLOCATION_KEYS
        current = {key: S._get_float(session, key, default) for key, default in keys}
        total_now = sum(current.values())
        n = len(keys)

        # Default behaviour: hold the total steady and split it evenly, so
        # adding a sport redistributes rather than inflating exposure.
        per_sport = args.pct if args.pct is not None else round(total_now / n, 4)
        new_total = per_sport * n

        print(f"{n} registered sports")
        print(f"current total {total_now:.2%}   ->   new total {new_total:.2%}")
        print(f"per sport     {per_sport:.2%}\n")

        changed = [(k, v) for k, v in current.items() if abs(v - per_sport) > 1e-9]
        if not changed:
            print("already equal -- nothing to do")
        for key, was in sorted(changed):
            print(f"  {key:28s} {was:.2%} -> {per_sport:.2%}")

        if new_total > TARGET_MAX_TOTAL:
            print(f"\nREFUSING: {new_total:.2%} exceeds {TARGET_MAX_TOTAL:.0%}. Past that the "
                  f"over-allocation guard scales every pool down, which looks like a bug. "
                  f"Pass --pct with a smaller share.")
            return

        headroom = int(TARGET_MAX_TOTAL / per_sport) - n if per_sport > 0 else 0
        print(f"\nheadroom: {headroom} more sport(s) can be added at this share "
              f"before the guard engages")

        if not args.write:
            print("\ndry run -- pass --write to apply")
            return

        for key, _default in keys:
            S._set_float(session, key, per_sport)
        session.commit()
        print("\napplied.")
        print(f"verify: total is now {S._allocation_total(session):.2%}, "
              f"guard scale {S._scale_for_total(S._allocation_total(session)):.4f}")


if __name__ == "__main__":
    main()
