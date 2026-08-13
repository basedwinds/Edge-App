"""Fit the expected-goals scale for soccer, validated on held-out seasons. (#133)

THE FINDING THAT REDIRECTED THIS. The task was raised as "the high-goal tail is
over-priced, probably Poisson dispersion". It is not dispersion. Measured over
62,584 matches (2019+):

    actual Var(total) / model-implied Var(total) = 1.0038

essentially exactly Poisson. The tail is over-priced for a duller reason: the
model expects too many goals outright.

    predicted total 2.7210   actual 2.6733   +0.0477 per match, z +7.3

Decomposed, the running league average is not to blame either -- it sits at
2.6325, slightly BELOW actual, because it lags a gently rising scoring trend.
The rating multiplier adds +0.0885 on top of it. That is Jensen's inequality:
the rating update is unbiased in LOG space, but E[exp(X)] > exp(E[X]) as soon
as the ratings have any spread, so even perfectly calibrated log-ratings
inflate expected goals -- here by ~3.4%.

So the fix is one multiplicative constant, not a new distribution.

WHY THIS NEEDS A FULL REPLAY PER CANDIDATE, unlike the rho fit. GOAL_SCALE
changes expected goals, and update_ratings takes its residual from expected
goals -- so it moves the whole rating trajectory. Worse, it is SELF-CORRECTING:
deflating expected goals makes (actual - expected) more positive, which pushes
ratings back up and partly undoes the deflation. Bisection still works because
it measures the settled outcome, but each candidate is a real replay.

THE SHIP TEST. On HELD-OUT seasons, per leg, with a paired bootstrap rather
than a bare "did anything get worse" rule -- that rule proved close to
unpassable for any change to a joint distribution (see the rho fit). Required:
  1. total-goals bias must fall
  2. no leg may be SIGNIFICANTLY worse (CI excluding zero)
  3. the over-3.5 / over-4.5 legs, the ones this exists for, must improve

HALVES ARE CHECKED EXPLICITLY. predict_half derates predict_match's expected
goals, so this scale flows into the half markets automatically -- which is
exactly how rho nearly shipped a silent half regression. Verified, not assumed.

======================= VERDICT 2026-08-13: DO NOT SHIP ======================

A global GOAL_SCALE is the wrong shape for this defect. Two runs:

    bounds        fitted    train bias        held-out bias      ship test
    0.90-1.05     0.90004   -0.0595 -> -0.0152   -0.0419 -> +0.0040   FAIL
    0.70-1.05     0.86757   -0.0595 -> +0.0000   -0.0419 -> +0.0196   FAIL

THE FIRST RUN WAS CLIPPED -- 0.90004 against a floor of 0.90 is a boundary hit,
not an optimum, and could not be read however good it looked. Widening the
bounds gave the honest interior value, 0.86757.

**And the honest value is WORSE out of sample than the clipped one** (+0.0196 vs
+0.0040 held-out). That is not noise, it is a mis-targeted objective: the
bisection drives TRAIN bias to zero, but train wants more deflation than the
held-out seasons do, so the train-optimal scale overshoots on them. The cause is
already in this docstring -- scoring is on "a gently rising trend", so the later
held-out seasons carry more goals and need less correction. Fitting a constant
to the older window and applying it to the newer one bakes in the drift.

TWO OPPOSING BIASES ARE BEING CONFLATED. The running league average LAGS the
rising trend (under-predicts); Jensen's inequality inflates E[exp(X)] above
exp(E[X]) (over-predicts, +0.0885). One constant cannot separate them, and their
sum is not stable over time -- which is exactly why the fit does not transfer.

THE JENSEN TERM IS ANALYTIC, NOT A CONSTANT TO FIT. The inflation is exp(sigma^2/2)
where sigma^2 is the variance of the summed log-ratings, so it depends on how
spread the ratings are -- which differs by league and moves over time. The
principled fix subtracts sigma^2/2 in log space per match rather than scaling
every match by one number. That also predicts the failure above: a global
constant is only right at the average spread.

BOTH RUNS FAIL ON THE SAME TWO LEGS -- total_o0.5 and win_home -- and win_home
already carries a lean from the rho ship, so a second regression stacks on the
one leg that can least afford it. The gains are real (o3.5, o4.5, btts,
total_o1.5 all improve, which is what this exists for), but not by a route worth
taking.

GOAL_SCALE stays 1.0. Next attempt should be the per-match Jensen correction,
scored on the SAME held-out seasons and the same pre-registered ship test.
"""
from __future__ import annotations

import collections
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline import elo_service_soccer as E  # noqa: E402
from app.models.baseline import elo_soccer as M  # noqa: E402

MIN_MATCHES = 10
TRAIN_FROM_YEAR = 2019
HOLDOUT_SEASONS = 3
# WIDENED 2026-08-13. The first run converged to 0.90004 against a floor of
# 0.90 -- a boundary hit, not an interior optimum, so the value could not be
# trusted however good its ship test looked. The self-correction in the
# docstring is why the root sits so far below 1: deflating expected goals makes
# (actual - expected) more positive, which pushes ratings back up and partly
# undoes the deflation, so a ~3.4% inflation needs a much larger nominal cut.
# Always check a bisection result against its own bounds before reading it.
BISECT_LO, BISECT_HI = 0.70, 1.05
BISECT_ITERS = 11
TOTAL_LINES = (0.5, 1.5, 2.5, 3.5, 4.5)


def season_year(m) -> int:
    s = (m.get("season") or "")[:4]
    return int(s) if s.isdigit() else 0


def bisect_bias(by_league, scale: float) -> float:
    """TRAIN total-goals bias only. Deliberately skips half-distributions and
    stores nothing: predict_half re-enters predict_match, so building halves
    during the search triples the cost of the one number bisection needs."""
    M.GOAL_SCALE = scale
    tot = 0.0
    n = 0
    for lg, games in by_league.items():
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
            if (not scoreable or dist is None or season_year(m) < TRAIN_FROM_YEAR
                    or m.get("season") in holdout):
                continue
            tot += (m["home_goals_ft"] + m["away_goals_ft"]) - (dist.expected_home_goals + dist.expected_away_goals)
            n += 1
    return tot / n if n else 0.0


def replay(by_league, scale: float):
    """Full walk-forward replay at this scale. Returns (train, test) rows of
    (dist, home_goals, away_goals, half1_dist, half2_dist)."""
    M.GOAL_SCALE = scale
    train, test = [], []
    for lg, games in by_league.items():
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
            hh, ha = m.get("home_goals_ht"), m.get("away_goals_ht")
            halves = None
            if hh is not None and ha is not None:
                halves = (E.predict_half(state, home, away, 1), E.predict_half(state, home, away, 2),
                          hh, ha, m["home_goals_ft"] - hh, m["away_goals_ft"] - ha)
            row = (dist, m["home_goals_ft"], m["away_goals_ft"], halves)
            (test if m.get("season") in holdout else train).append(row)
    return train, test


def total_bias(rows) -> float:
    return statistics.mean((hg + ag) - (d.expected_home_goals + d.expected_away_goals)
                           for d, hg, ag, _ in rows)


def legs(rows) -> dict[str, list]:
    """(prob, outcome) per leg, full-time and both halves."""
    b: dict[str, list] = collections.defaultdict(list)
    for d, hg, ag, halves in rows:
        tot = hg + ag
        b["win_home"].append((d.prob_home_win(), float(hg > ag)))
        b["win_draw"].append((d.prob_draw(), float(hg == ag)))
        b["win_away"].append((d.prob_away_win(), float(hg < ag)))
        b["btts"].append((d.prob_btts(), float(hg > 0 and ag > 0)))
        for ln in TOTAL_LINES:
            b[f"total_o{ln}"].append((d.prob_total_over(ln), float(tot > ln)))
        if halves:
            d1, d2, h1h, h1a, h2h, h2a = halves
            for tag, dd, gh, ga in (("H1", d1, h1h, h1a), ("H2", d2, h2h, h2a)):
                b[f"{tag}_draw"].append((dd.prob_draw(), float(gh == ga)))
                b[f"{tag}_btts"].append((dd.prob_btts(), float(gh > 0 and ga > 0)))
                b[f"{tag}_o1.5"].append((dd.prob_total_over(1.5), float(gh + ga > 1.5)))
    return b


def main() -> None:
    matches = [m for m in soccer_data.load_matches()
               if m.get("home_goals_ft") is not None and m.get("away_goals_ft") is not None]
    by_league: dict[str, list[dict]] = collections.defaultdict(list)
    for m in matches:
        if m.get("league"):
            by_league[m["league"]].append(m)
    for games in by_league.values():
        games.sort(key=lambda m: (m.get("match_date") or "", m.get("home_team") or ""))

    lo, hi = BISECT_LO, BISECT_HI
    for _ in range(BISECT_ITERS):
        mid = (lo + hi) / 2
        # bias = actual - predicted; too few goals predicted -> raise the scale
        if bisect_bias(by_league, mid) > 0:
            lo = mid
        else:
            hi = mid
    scale = (lo + hi) / 2

    tr0, te0 = replay(by_league, 1.0)
    tr1, te1 = replay(by_league, scale)
    print(f"{len(tr0)} train / {len(te0)} held-out matches (seasons {TRAIN_FROM_YEAR}+, "
          f"last {HOLDOUT_SEASONS} held out)")
    print(f"fitted GOAL_SCALE = {scale:.5f}   train total-goals bias "
          f"{total_bias(tr0):+.4f} -> {total_bias(tr1):+.4f}")
    print(f"held-out total-goals bias {total_bias(te0):+.4f} -> {total_bias(te1):+.4f}\n")

    b0, b1 = legs(te0), legs(te1)
    rng = random.Random(20260809)
    print(f"{'leg':12s}{'n':>7s}{'bias before':>13s}{'bias after':>12s}"
          f"{'Brier delta':>14s}{'95% CI':>26s}   verdict")
    regressions = []
    for k in sorted(b0):
        o, n_ = b0[k], b1[k]
        bias0 = statistics.mean(y - p for p, y in o)
        bias1 = statistics.mean(y - p for p, y in n_)
        diffs = [(p1 - y) ** 2 - (p0 - y) ** 2 for (p0, y), (p1, _) in zip(o, n_)]
        n = len(diffs)
        mean = statistics.mean(diffs)
        boot = sorted(statistics.mean([diffs[rng.randrange(n)] for _ in range(n)]) for _ in range(1500))
        cl, ch = boot[37], boot[1462]
        if ch < 0:
            v = "BETTER"
        elif cl > 0:
            v = "WORSE"
            regressions.append(k)
        else:
            v = "no sig. change"
        print(f"{k:12s}{n:7d}{bias0:+13.4f}{bias1:+12.4f}{mean:+14.7f}"
              f"   [{cl:+.7f}, {ch:+.7f}]   {v}")

    tail_ok = all(abs(statistics.mean(y - p for p, y in b1[k]))
                  < abs(statistics.mean(y - p for p, y in b0[k])) for k in ("total_o3.5", "total_o4.5"))
    bias_ok = abs(total_bias(te1)) < abs(total_bias(te0))
    print(f"\ntotal-goals bias improved: {bias_ok}")
    print(f"over-3.5 and over-4.5 bias both improved: {tail_ok}")
    print(f"significantly WORSE legs: {regressions if regressions else 'none'}")
    passed = bias_ok and tail_ok and not regressions
    print("\nSHIP TEST:", "PASS" if passed else "FAIL")
    if passed:
        print(f"  set elo_soccer.GOAL_SCALE = {scale:.5f}")


if __name__ == "__main__":
    main()
