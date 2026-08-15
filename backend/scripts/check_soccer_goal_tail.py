"""Is the soccer model's HIGH-GOAL tail mis-priced, and can a shrink fix it? (#133)

WHAT WAS EXPECTED. Soccer prices totals off independent Poissons with a
low-score correction. Real goal counts are usually OVERdispersed relative to
Poisson, so the standing assumption was that the model UNDER-prices the tail --
the same shape that made MLB totals a negative binomial in #194.

THE DIRECTION IS BACKWARDS. Walk-forward over 21,589 big-5 matches with the
production predict_match / update_ratings and the shipped xG blend:

    P(total > 2.5)   model 53.03%   actual 52.46%   +0.57pp
    P(total > 3.5)   model 31.32%   actual 30.23%   +1.09pp
    P(total > 4.5)   model 15.99%   actual 14.79%   +1.20pp
    P(total > 5.5)   model  7.17%   actual  6.39%   +0.78pp
    P(total > 6.5)   model  2.87%   actual  2.38%   +0.49pp

The model OVER-prices the tail at every threshold. At n=21,589 the +1.20pp peak
is ~4.8 SE, so it is real -- but it is 5x smaller than the MLB Normal's error was
and it points the other way.

THE PER-TOTAL SHAPE SAYS WHY: too much mass at 0 AND at 5-9+, too little at 1 and
3. That is the model being OVER-dispersed -- its expected-goals estimates vary
more across matches than reality does, which is a rating-spread problem rather
than a distribution-family problem.

TWO SHRINKS TESTED, and the first one was aimed wrong:

  1. Shrink the HOME/AWAY SPLIT toward its own mean -- ZERO effect (0.55601 ->
     0.55600). Obvious in hindsight: that transformation preserves the TOTAL, and
     a total-goals outcome barely depends on how the goals are split.
  2. Shrink the TOTAL expected goals toward the league average -- the right axis.

RESULT: DO NOT SHIP. Sweeping the total shrink, train = seasons <=2021,
test = 2022-2025:

    shrink   TRAIN ll   TEST ll
      1.00    0.55601   0.55559   (production)
      0.90    0.55599   0.55523   <- train "best", by 0.00002
      0.80    0.55619   0.55513   <- test best
      0.70    0.55661   0.55531
      0.60    0.55727   0.55577

The TRAIN objective is FLAT: 0.00002 separates production from its own optimum,
which is noise, not a basin. Every parameter shipped alongside this one (CS2
GAP_SHRINK 0.80, Valorant 0.86, MLB pitcher 0.4, soccer xG 0.50) had a clear
interior minimum on train. Choosing 0.80 here would mean reading it off the test
set -- the exact move refused in #196 and #199.

WHAT WOULD CHANGE THE ANSWER: a fix aimed at the DISPERSION directly rather than
at the mean's spread (the tail error survives even when the expected total is
right), or more data at the tail. The defect is quantified and open, not closed.

Run: backend/.venv/Scripts/python.exe scripts/check_soccer_goal_tail.py
"""
from __future__ import annotations

import json
import math
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.models.baseline import elo_soccer as E  # noqa: E402
from app.models.baseline import soccer_xg  # noqa: E402
from app.models.baseline.elo_soccer import (  # noqa: E402
    MAX_GOALS, SoccerRatingState, predict_match, update_ratings,
)

CACHE = pathlib.Path(__file__).resolve().parents[2] / "data" / "soccer_xg_cache.json"
LINES = [1.5, 2.5, 3.5, 4.5]
TRAIN = set(range(2014, 2022))
TEST = set(range(2022, 2026))


def load():
    xg = json.loads(CACHE.read_text(encoding="utf-8"))
    rows = []
    for lg, seasons in xg.items():
        for s, ms in seasons.items():
            for m in ms:
                if m.get("goals_h") is None:
                    continue
                rows.append({**m, "league": lg, "season": int(s)})
    rows.sort(key=lambda r: (r["league"], r["season"], r["date"]))
    return rows


def over(grid, line):
    return sum(grid[h][a] for h in range(MAX_GOALS + 1)
               for a in range(MAX_GOALS + 1) if h + a > line)


def replay(rows, shrink=1.0):
    """shrink applies to the TOTAL expected goals, pulled toward the running
    league average. 1.0 is production."""
    states, res, model, emp = {}, [], defaultdict(float), defaultdict(int)
    for r in rows:
        st = states.setdefault(r["league"], SoccerRatingState())
        st.start_season_if_new(r["season"])
        d = predict_match(st, r["home"], r["away"])
        eh, ea = d.expected_home_goals, d.expected_away_goals
        if shrink != 1.0:
            tot = eh + ea
            ref = (st.goals_sum / max(st.goals_n, 1)) * 2.0 if st.goals_n else tot
            k = (ref + (tot - ref) * shrink) / tot if tot > 0 else 1.0
            eh, ea = eh * k, ea * k
        grid = E._build_grid(eh, ea)
        act = r["goals_h"] + r["goals_a"]
        res.append((r["season"], [(over(grid, l), 1.0 if act > l else 0.0) for l in LINES]))
        for h in range(MAX_GOALS + 1):
            for a in range(MAX_GOALS + 1):
                model[h + a] += grid[h][a]
        emp[act] += 1
        oh = soccer_xg.blended(r["goals_h"], r["xg_h"])
        oa = soccer_xg.blended(r["goals_a"], r["xg_a"])
        update_ratings(st, r["home"], r["away"], oh, oa)
        st.goals_sum += (r["goals_h"] + r["goals_a"]) - (oh + oa)
    return res, model, emp


def ll(res, seasons):
    t = n = 0
    for s, pairs in res:
        if s not in seasons:
            continue
        for p, y in pairs:
            p = min(max(p, 1e-9), 1 - 1e-9)
            t += -(y * math.log(p) + (1 - y) * math.log(1 - p))
            n += 1
    return t / max(n, 1)


def main() -> None:
    rows = load()
    res, model, emp = replay(rows)
    n = len(rows)
    print(f"matches {n}")
    print(f"\n{'total':>7}{'model %':>10}{'actual %':>10}{'model-actual':>14}")
    for k in range(0, 9):
        print(f"{k:>7}{100*model[k]/n:>10.2f}{100*emp[k]/n:>10.2f}"
              f"{100*(model[k]-emp[k])/n:>+14.2f}")
    mh = sum(v for k, v in model.items() if k >= 9)
    eh_ = sum(v for k, v in emp.items() if k >= 9)
    print(f"{'9+':>7}{100*mh/n:>10.2f}{100*eh_/n:>10.2f}{100*(mh-eh_)/n:>+14.2f}")

    print("\nCUMULATIVE TAIL -- what an over-bet actually prices:")
    for thr in (2.5, 3.5, 4.5, 5.5, 6.5):
        mm = 100 * sum(v for k, v in model.items() if k > thr) / n
        aa = 100 * sum(v for k, v in emp.items() if k > thr) / n
        print(f"  P(total > {thr}):  model {mm:6.2f}%   actual {aa:6.2f}%   {mm-aa:+.2f}pp")

    print(f"\nTOTAL-EXPECTED-GOALS SHRINK SWEEP  (train <=2021, test 2022-2025)")
    print(f"{'shrink':>8}{'TRAIN ll':>11}{'TEST ll':>11}")
    for sh in (1.0, 0.9, 0.8, 0.7, 0.6):
        r2, _, _ = replay(rows, sh) if sh != 1.0 else (res, None, None)
        tag = "  (production)" if sh == 1.0 else ""
        print(f"{sh:>8.2f}{ll(r2, TRAIN):>11.5f}{ll(r2, TEST):>11.5f}{tag}")
    print("\n  VERDICT: do NOT ship. The TRAIN objective is flat (0.00002 between")
    print("  production and its own optimum) -- picking the test-best 0.80 would be")
    print("  selecting on the data reserved to judge the choice.")


if __name__ == "__main__":
    main()
