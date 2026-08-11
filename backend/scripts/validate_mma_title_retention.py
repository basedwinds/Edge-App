"""Walk-forward validation of the UFC belt-retention model. (Task #111.)

WHY. fit_mma_title_retention.py fitted the parameters and then named its own
gate: "NO WALK-FORWARD VALIDATION. Matching 2 of 4 champions by eye is not
validation, it is four data points. The archive supports scoring this against
real past year-ends, and that is the gate." This is that gate.

THE TEST. Stand at 11 August of each past year -- the same vantage the live
market is priced from, ~4.7 months out from 31 December -- identify each
division's champion from fights BEFORE that date, predict P(still champion on
31 Dec), and check what actually happened.

NO LEAKAGE. Hazard and retention for year Y are fitted only on title fights
before 1 January of Y. A model that saw Y's own fights would score itself.

THE OUTCOME IS DERIVED, NOT ASSUMED. "Held the belt" means no decisive
non-interim title fight in that division between the vantage and 31 December was
won by someone other than the champion. Vacations, retirements and interim
elevations are invisible in fight results -- the same limitation that made
FIX C fail -- so a division whose belt changed hands administratively scores as
"held" here. That biases the measured accuracy UP, and is stated rather than
hidden.

THE STALENESS GUARD IS PART OF WHAT IS BEING TESTED. fit_ recommended refusing a
division whose last title fight is over ~12 months old, because "most recent
title-fight winner" cannot see a vacated belt (it returns Jon Jones for a
Heavyweight belt Tom Aspinall holds). This applies that guard and reports how
many division-years it excludes, so the cost of the guard is visible.

BASELINE. Scored against the historical base rate of champions holding, because
a model that cannot beat "champions usually hold" is not a model. Same
discipline as every other validation in this app: never against a coin flip.
"""
from __future__ import annotations

import collections
import datetime
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fit_mma_title_retention import load_title_fights  # noqa: E402

VANTAGE_MONTH, VANTAGE_DAY = 8, 11
STALE_DAYS = 365
FIRST_YEAR = 2013          # modern era; the 1990s tournament rows are excluded upstream
MIN_PRIOR_FIGHTS = 6       # per division, before it can be fitted at all


def _division(fight) -> str:
    return (fight["weight_class"] or "").replace("Interim ", "").strip()


def main() -> None:
    fights = [f for f in load_title_fights() if not f["interim"] and f["decisive"]]
    print(f"decisive non-interim title fights in archive: {len(fights)}")
    if not fights:
        return
    print(f"date range: {fights[0]['date']} .. {fights[-1]['date']}")

    years = sorted({f["date"].year for f in fights})
    years = [y for y in years if y >= FIRST_YEAR and y < max(years)]
    rows, skipped_stale, skipped_thin = [], 0, 0

    for y in years:
        vantage = datetime.date(y, VANTAGE_MONTH, VANTAGE_DAY)
        year_end = datetime.date(y, 12, 31)
        window_years = (year_end - vantage).days / 365.25
        prior = [f for f in fights if f["date"] < datetime.date(y, 1, 1)]
        by_div_prior = collections.defaultdict(list)
        for f in prior:
            by_div_prior[_division(f)].append(f)

        for div, hist in by_div_prior.items():
            if len(hist) < MIN_PRIOR_FIGHTS:
                skipped_thin += 1
                continue
            # Champion as of the vantage: winner of the most recent decisive
            # non-interim title fight in this division before that date.
            before = [f for f in fights if _division(f) == div and f["date"] < vantage]
            if not before:
                continue
            last = before[-1]
            if (vantage - last["date"]).days > STALE_DAYS:
                skipped_stale += 1
                continue
            champ = last["winner_name"]

            # HAZARD, fitted pre-Y: title fights per year in this division.
            span_years = max((hist[-1]["date"] - hist[0]["date"]).days / 365.25, 0.5)
            rate = len(hist) / span_years
            p_fight = 1.0 - math.exp(-rate * window_years)

            # RETENTION, fitted pre-Y: how often the defending champion won.
            defended = held = 0
            chain = None
            for f in hist:
                if chain is not None:
                    defended += 1
                    if f["winner_name"] == chain:
                        held += 1
                chain = f["winner_name"]
            retention = (held / defended) if defended >= 3 else 0.5

            p_hold = 1.0 - p_fight * (1.0 - retention)

            # ACTUAL: did anyone else win this belt before year end?
            after = [f for f in fights
                     if _division(f) == div and vantage <= f["date"] <= year_end]
            lost = any(f["winner_name"] != champ for f in after)
            rows.append({"year": y, "div": div, "champ": champ, "p": p_hold,
                         "held": 0 if lost else 1, "n_after": len(after)})

    if not rows:
        print("no scorable division-years")
        return

    n = len(rows)
    base = sum(r["held"] for r in rows) / n
    brier = sum((r["p"] - r["held"]) ** 2 for r in rows) / n
    brier_base = sum((base - r["held"]) ** 2 for r in rows) / n
    mean_p = sum(r["p"] for r in rows) / n

    print(f"\nscored division-years: {n}  ({len(years)} years, "
          f"{skipped_stale} skipped by the 12-month staleness guard, "
          f"{skipped_thin} skipped as too thin)")
    print(f"  actual hold rate      {base:.4f}")
    print(f"  mean predicted        {mean_p:.4f}   bias {mean_p - base:+.4f}")
    print(f"  model Brier           {brier:.5f}")
    print(f"  base-rate Brier       {brier_base:.5f}   <- the bar to beat")
    print(f"  -> {'MODEL BEATS base rate' if brier < brier_base else 'MODEL FAILS to beat base rate'}"
          f" ({100 * (brier_base - brier) / brier_base:+.1f}%)")

    print("\ncalibration by predicted band:")
    buckets = [(0.0, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in buckets:
        sel = [r for r in rows if lo <= r["p"] < hi]
        if len(sel) < 5:
            continue
        pm = sum(r["p"] for r in sel) / len(sel)
        am = sum(r["held"] for r in sel) / len(sel)
        print(f"  {lo:.2f}-{hi:<4.2f} n={len(sel):3d}  pred {pm:.3f}  actual {am:.3f}  "
              f"gap {pm - am:+.3f}")

    print("\nper-year:")
    by_year = collections.defaultdict(list)
    for r in rows:
        by_year[r["year"]].append(r)
    for y in sorted(by_year):
        sel = by_year[y]
        print(f"  {y}: n={len(sel):2d}  actual {sum(s['held'] for s in sel) / len(sel):.2f}  "
              f"pred {sum(s['p'] for s in sel) / len(sel):.2f}")


if __name__ == "__main__":
    main()
