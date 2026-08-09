"""Fit the Dixon-Coles low-score correlation for soccer, and validate it on
held-out seasons before anything ships. (Task #131.)

THE DEFECT, measured by scripts/audit_soccer_market_types.py on 2019+ seasons
only -- so the declining-home-advantage era effect that wrecked the per-league
home fit could not contaminate it:

    win_draw      +0.0142  z +8.1    draws UNDER-priced by 1.4pp
    total_o4.5    -0.0117  z -8.6    the high-goal tail OVER-priced
    total_o3.5    -0.0122  z -6.9
    every tie under-priced:  1-1 +0.0081, 2-2 +0.0033, 0-0 +0.0031
    away scorelines over-priced: 0-1 -0.0053, 0-2 -0.0040

One cause with two faces: elo_soccer builds an INDEPENDENT Poisson joint grid.
Independence spreads mass too widely -- too little on level scorelines, too
much in the tail. Real matches do not score independently.

WHAT IS FITTED: a single rho, applied to the four cells where both sides score
at most once (elo_soccer._build_grid). rho < 0 lifts 0-0 and 1-1 and trims 1-0
and 0-1, then the grid is renormalised.

WHY THIS IS CHEAP, unlike the home-advantage fit. update_ratings takes its
residuals from expected_home_goals / expected_away_goals, NOT from the grid. So
rho cannot change the rating trajectory: the walk-forward replay runs ONCE, and
every candidate rho is scored against the same cached (expected goals, actual
score) pairs. No re-replay per candidate.

HOW IT IS FITTED: P(draw) is monotone in rho, so the value that zeroes the
TRAIN draw bias is found by bisection. Fitted on a MODERN window only, for the
same reason the home fit had to be -- see fit_soccer_home_advantage.py.

THE SHIP TEST, and it is a rejection test. On HELD-OUT seasons, PER MARKET
TYPE, never pooled:
  1. the draw leg's |bias| must fall
  2. no market type may get worse on Brier
A correlation term touches every market derived from the grid, so "the draw
improved" is not enough -- if totals or correct scores degrade to pay for it,
that is skill being moved around, not added, and it must be REJECTED.

===========================================================================
RESULT, 2026-08-09. rho = -0.0603 SHIPPED, over a FAILED pre-registered test.

The rule above says reject if any leg degrades on Brier. Five did, so this
FAILED as written. A per-leg paired bootstrap on the held-out set then showed
four of those five were noise:

    win_draw    -0.000222  [-0.000362, -0.000074]  significantly BETTER
    win_away    -0.000166  [-0.000238, -0.000092]  significantly BETTER
    cs_1-0      +0.000086  [+0.000038, +0.000138]  significantly WORSE
    cs_0-0 / total_o0.5 / total_o1.5 / win_home     indistinguishable from 0

and the aggregate on the market that carries money improves outright:

    3-way log-loss  -0.000890  95% CI [-0.001271, -0.000501]
    3-way Brier     -0.000332  95% CI [-0.000563, -0.000116]

"No leg may get worse" is close to unpassable for ANY change to a joint
distribution -- moving mass always nudges something -- so it was a flaw in the
rule, not evidence about rho. The user made the call to ship on the
significance evidence. Recorded rather than quietly reworded, because a
criterion changed after seeing results is exactly how a bad change gets
justified.

WHAT IT COST, honestly. On the full audit (2019+), two legs that were unbiased
before now lean:
    win_home  +0.0006 z +0.3  ->  +0.0075 z +3.9
    btts      +0.0007 z +0.3  ->  -0.0062 z -3.1
against win_draw +0.0142 z +8.1 -> +0.0005 z +0.3 and win_away -0.0148 z -8.4
-> -0.0079 z -4.5. The draw defect is closed; a smaller one was opened.

TWO THINGS THIS DOES NOT FIX, both still open:
  * THE TAIL. total_o3.5 (-0.0085) and total_o4.5 (-0.0111) are unchanged to
    the last digit -- tau only touches cells where both sides score at most
    once. That half of the audit's finding needs a dispersion fix, not this.
  * THE HALVES. Applying rho to predict_half made 5 of 6 half legs WORSE (H1
    draw bias -0.0039 -> -0.0160), because a half's expected goals are ~0.6
    rather than ~1.4 so the same tau is proportionally far larger. predict_half
    is now pinned to rho=0. Re-fit a separate half rho before ever reusing this
    number there.
===========================================================================
"""
from __future__ import annotations

import collections
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline import elo_service_soccer as E  # noqa: E402
from app.models.baseline import elo_soccer as M  # noqa: E402

MIN_MATCHES = 10
TRAIN_FROM_YEAR = 2019  # modern window only -- see the home-advantage fit
HOLDOUT_SEASONS = 3
BISECT_LO, BISECT_HI = -0.30, 0.30
BISECT_ITERS = 24
TOTAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)
CORRECT_SCORES = ((0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2), (2, 1), (1, 2), (2, 2))


def season_year(m) -> int:
    s = (m.get("season") or "")[:4]
    return int(s) if s.isdigit() else 0


def collect() -> tuple[list, list]:
    """One walk-forward replay. Returns (train, test) lists of
    (expected_home, expected_away, home_goals, away_goals) -- everything any
    rho needs, with the ratings already fixed."""
    matches = [m for m in soccer_data.load_matches()
               if m.get("home_goals_ft") is not None and m.get("away_goals_ft") is not None]
    by_league: dict[str, list[dict]] = collections.defaultdict(list)
    for m in matches:
        if m.get("league"):
            by_league[m["league"]].append(m)

    train, test = [], []
    for lg, games in by_league.items():
        games.sort(key=lambda m: (m.get("match_date") or "", m.get("home_team") or ""))
        seasons = sorted({m["season"] for m in games if m.get("season")})
        if len(seasons) <= HOLDOUT_SEASONS:
            continue
        holdout = set(seasons[-HOLDOUT_SEASONS:])
        state = E.SoccerRatingState(home_log=E.home_advantage_for_league(lg))
        seen: collections.Counter = collections.Counter()
        for m in games:
            home, away = m["home_team"], m["away_team"]
            scoreable = seen[home] >= MIN_MATCHES and seen[away] >= MIN_MATCHES
            dist = E.predict_and_update(state, m)
            seen[home] += 1
            seen[away] += 1
            if not scoreable or dist is None or season_year(m) < TRAIN_FROM_YEAR:
                continue
            row = (dist.expected_home_goals, dist.expected_away_goals,
                   m["home_goals_ft"], m["away_goals_ft"])
            (test if m.get("season") in holdout else train).append(row)
    return train, test


def score(rows, rho: float) -> dict:
    """Every market leg, scored off a grid rebuilt at this rho."""
    b: dict[str, list] = collections.defaultdict(list)
    for eh, ea, hg, ag in rows:
        d = M.MatchGoalDistribution(expected_home_goals=eh, expected_away_goals=ea,
                                    grid=M._build_grid(eh, ea, rho))
        tot = hg + ag
        b["win_home"].append((d.prob_home_win(), float(hg > ag)))
        b["win_draw"].append((d.prob_draw(), float(hg == ag)))
        b["win_away"].append((d.prob_away_win(), float(hg < ag)))
        b["btts"].append((d.prob_btts(), float(hg > 0 and ag > 0)))
        for ln in TOTAL_LINES:
            b[f"total_o{ln}"].append((d.prob_total_over(ln), float(tot > ln)))
        for h, a in CORRECT_SCORES:
            b[f"cs_{h}-{a}"].append((d.prob_correct_score(h, a), float(hg == h and ag == a)))
    out = {}
    for k, obs in b.items():
        n = len(obs)
        resid = [y - p for p, y in obs]
        mean_res = statistics.mean(resid)
        se = statistics.stdev(resid) / math.sqrt(n) if n > 1 else 0.0
        out[k] = {"n": n, "bias": mean_res, "z": mean_res / se if se else 0.0,
                  "brier": statistics.mean((p - y) ** 2 for p, y in obs)}
    return out


def draw_bias(rows, rho: float) -> float:
    tot = 0.0
    for eh, ea, hg, ag in rows:
        d = M.MatchGoalDistribution(expected_home_goals=eh, expected_away_goals=ea,
                                    grid=M._build_grid(eh, ea, rho))
        tot += (1.0 if hg == ag else 0.0) - d.prob_draw()
    return tot / len(rows)


def main() -> None:
    train, test = collect()
    print(f"{len(train)} train / {len(test)} held-out scored matches "
          f"(seasons {TRAIN_FROM_YEAR}+, last {HOLDOUT_SEASONS} held out)\n")

    # P(draw) rises as rho falls, so bias falls as rho falls.
    lo, hi = BISECT_LO, BISECT_HI
    for _ in range(BISECT_ITERS):
        mid = (lo + hi) / 2
        if draw_bias(train, mid) > 0:
            hi = mid   # still under-pricing draws -> need MORE correlation
        else:
            lo = mid
    rho = (lo + hi) / 2
    print(f"fitted rho = {rho:+.4f} (train draw bias {draw_bias(train, 0.0):+.4f} -> "
          f"{draw_bias(train, rho):+.4f})\n")

    before, after = score(test, 0.0), score(test, rho)
    print(f"HELD-OUT, per market leg          {'bias':>19s}      {'Brier':>17s}")
    print(f"{'leg':16s}{'n':>7s}{'before':>10s}{'after':>10s}{'  ':2s}{'before':>10s}{'after':>10s}   verdict")
    worse, better = [], []
    for k in sorted(before):
        b0, b1 = before[k], after[k]
        d_brier = b1["brier"] - b0["brier"]
        tag = ""
        if d_brier > 1e-6:
            tag = "WORSE"
            worse.append(k)
        elif d_brier < -1e-6:
            tag = "better"
            better.append(k)
        print(f"{k:16s}{b0['n']:7d}{b0['bias']:+10.4f}{b1['bias']:+10.4f}  "
              f"{b0['brier']:10.5f}{b1['brier']:10.5f}   {tag}")

    d0, d1 = before["win_draw"], after["win_draw"]
    bias_ok = abs(d1["bias"]) < abs(d0["bias"])
    print(f"\ndraw |bias| {abs(d0['bias']):.4f} -> {abs(d1['bias']):.4f}   "
          f"{'improved' if bias_ok else 'NOT improved'}")
    print(f"legs better on Brier: {len(better)}   legs WORSE on Brier: {len(worse)}"
          + (f"  {worse}" if worse else ""))
    passed = bias_ok and not worse
    print("\nIS ANY OF THIS REAL? paired bootstrap on the held-out 3-way outcome,")
    print("which is the market that actually carries money (1,554 live moneyline_3way):")
    significance(test, rho)

    print("\nSHIP TEST (pre-registered):", "PASS" if passed else "FAIL")
    if not passed:
        print("  A correlation term touches every market read off the grid. If any leg")
        print("  degrades to pay for the draw, that is skill moved around, not added.")
        return
    print(f"\n  set elo_soccer.LOW_SCORE_RHO = {rho:.4f}")




def significance(test, rho: float, iters: int = 2000) -> None:
    """Is any of this real? A paired bootstrap on the 3-way outcome, which is
    the market that actually carries money (1,554 live moneyline_3way rows).

    Paired, because both models score the SAME matches -- the per-match
    difference has far less variance than either score alone, and comparing
    two independent confidence intervals would hide a real effect.
    """
    import random
    diffs_ll, diffs_br = [], []
    for eh, ea, hg, ag in test:
        d0 = M.MatchGoalDistribution(eh, ea, M._build_grid(eh, ea, 0.0))
        d1 = M.MatchGoalDistribution(eh, ea, M._build_grid(eh, ea, rho))
        y = 0 if hg > ag else (1 if hg == ag else 2)
        p0 = (d0.prob_home_win(), d0.prob_draw(), d0.prob_away_win())
        p1 = (d1.prob_home_win(), d1.prob_draw(), d1.prob_away_win())
        diffs_ll.append(-math.log(max(p1[y], 1e-9)) + math.log(max(p0[y], 1e-9)))
        diffs_br.append(sum((p1[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3))
                        - sum((p0[i] - (1.0 if i == y else 0.0)) ** 2 for i in range(3)))

    rng = random.Random(20260809)
    for name, diffs in (("3-way log-loss", diffs_ll), ("3-way Brier", diffs_br)):
        n = len(diffs)
        mean = statistics.mean(diffs)
        boot = []
        for _ in range(iters):
            boot.append(statistics.mean([diffs[rng.randrange(n)] for _ in range(n)]))
        boot.sort()
        lo, hi = boot[int(0.025 * iters)], boot[int(0.975 * iters)]
        verdict = ("IMPROVES (CI excludes 0)" if hi < 0 else
                   "DEGRADES (CI excludes 0)" if lo > 0 else "no significant change")
        print(f"  {name:16s} mean delta {mean:+.6f}  95% CI [{lo:+.6f}, {hi:+.6f}]  {verdict}")
    print("  (negative = the correlation term is BETTER)")


if __name__ == "__main__":
    main()
