"""Fit a PER-LEAGUE home advantage for the soccer model, and validate it on
held-out seasons before anything ships. (Task #128, raised by
scripts/audit_soccer_leagues.py.)

THE PROBLEM. Home advantage is a single global constant in this model
(elo_soccer.HOME_ADVANTAGE_LOG = 0.20, grid-searched across all leagues at
once). Home advantage genuinely differs by country, so one constant cannot fit
26 leagues -- and the audit measured exactly that: 17 of 28 leagues
systematically tilted, almost all in the same direction (the model UNDER-rates
home teams), Greece and Brazil by +6.3pp, Japan -2.5pp the other way.

WHY A NAIVE FIT WOULD BE WORTHLESS. Fitting a constant per league on all of
that league's history and then reporting the bias on the same history is
circular -- the bias goes to zero BY CONSTRUCTION, and the number proves
nothing about a future match. So:

  TRAIN = every season except the last HOLDOUT_SEASONS
  TEST  = the last HOLDOUT_SEASONS, never seen by the fit

The offset is fitted on TRAIN only and scored on TEST only.

HOW THE FIT WORKS. The home term is one number and P(home win) is monotone in
it, so the value that zeroes the TRAIN bias is found by bisection -- no grid,
no local minima. Note the term feeds the rating UPDATE as well as the
prediction (update_ratings calls predict_match to get its residuals), so every
candidate needs a full walk-forward replay of the league; the trajectory is not
reusable. That is why this is a bisection and not a 21-point grid.

SHRINKAGE. A league whose measured tilt is within noise should not get a
bespoke constant fitted to that noise. Two guards:
  * leagues with |z| <= MIN_Z on TRAIN keep the global constant untouched
  * survivors are shrunk toward the global by the James-Stein factor
    (1 - 1/z^2), so a marginal league moves a fraction of the way and a
    10-sigma league moves essentially all of it

THE SHIP TEST, and it is a rejection test, applied PER LEAGUE. On that
league's own HELD-OUT seasons:
  1. |bias| must fall -- that is the whole point
  2. Brier and log-loss must NOT get worse
If bias improves while Brier degrades, the fit is stealing genuine skill to buy
calibration and it must be REJECTED for that league.

The test is per-league and NOT pooled, because each league gets its own
constant -- they are separate one-parameter hypotheses, and a pooled average
lets one league that improves a lot pay for another that degrades. That is
exactly what happened on the first modern-window run: pooled PASS, but the
average hid MLS getting worse on all three metrics.

===========================================================================
RESULT, 2026-08-09. The premise of the task was largely wrong, and the
held-out check is what caught it.

RUN 1 -- fit on ALL pre-holdout seasons, as the task specified. FAILED, and
informatively. Held-out |bias| went 0.0006 -> 0.0278: the global constant was
ALREADY unbiased on recent seasons, and the fit INTRODUCED a 2.8pp tilt where
none existed. Six of 18 leagues improved. Shipping this would have made 12
leagues worse.

WHY: home advantage has DECLINED. Bias under the unchanged global 0.20, by era:

    1992-1994  +0.0472  z  +8.4     actual home win 47.9%
    1998-2000  +0.0413  z +11.4                     47.2%
    2007-2009  +0.0272  z  +7.9                     45.7%
    2019-2021  -0.0045  z  -1.5                     42.9%  <- crowdless seasons
    2022-2024  +0.0075  z  +2.6                     44.2%
    2025-2027  +0.0047  z  +1.0                     44.0%

The audit pooled 33 seasons, so its "per-league" tilt was mostly a measurement
of the 1990s. Restricted to 2019+, only 3 of 26 leagues are tilted at all
(pooled bias +0.0023), and the country ordering does NOT persist -- Greece goes
from the most tilted league on full history (+0.0628, z +11.1) to -0.0115
(z -1.0). A real national home-advantage effect would not do that.

RUN 2 -- fit on the modern window only (TRAIN_FROM_YEAR). Two leagues cleared
the significance guard; ONE survived its own held-out check:

    SHIP    BRA1  0.200 -> 0.322   holdout n= 945  |bias| 0.0587 -> 0.0200
                                   Brier 0.2398 -> 0.2366  LL 0.6726 -> 0.6659
    REJECT  MLS   0.200 -> 0.338   holdout n=1258  |bias| 0.0065 -> 0.0426
                                   Brier 0.2363 -> 0.2380  LL 0.6648 -> 0.6687

BRA1 is the only league tilted in the SAME direction in every window looked at
(full history +0.0640, modern era +0.0468, train +0.0407, holdout +0.0587) and
it improves on bias, Brier AND log-loss out of sample. MLS looked just as
strong on TRAIN (+0.0499, z +4.7) and was already unbiased on its holdout
(+0.0065) -- its tilt is not stable even within the modern era, so it keeps the
global constant.

THE STANDING CONCLUSION: for modern football the single global 0.20 is right
for 25 of 26 leagues, and soccer_home_advantage.json is near-empty on purpose.
===========================================================================
"""
from __future__ import annotations

import collections
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline import elo_soccer as M  # noqa: E402

MIN_MATCHES = 10  # same warm-up guard the audit uses, so the two are comparable
# Only seasons from here on are SCORED or fitted. The replay still starts from
# the beginning of history -- ratings need the warm-up -- but older matches
# cannot vote on the home term, because home advantage has DECLINED steadily
# (see the era table in the result block) and fitting across 33 seasons just
# measures the 1990s.
TRAIN_FROM_YEAR = 2019
HOLDOUT_SEASONS = 3  # last N seasons per league, never seen by the fit
MIN_TRAIN = 800  # scored TRAIN matches required before a league is fitted at all
MIN_TEST = 150  # scored TEST matches required before the held-out check means anything
MIN_Z = 3.0  # below this the TRAIN tilt is noise; keep the global constant
BISECT_LO, BISECT_HI = -0.30, 0.70  # brackets every plausible home term
BISECT_ITERS = 22  # -> resolution ~2e-7, far finer than needed

OUT_PATH = Path(__file__).resolve().parents[1] / "app" / "models" / "baseline" / "soccer_home_advantage.json"


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def season_year(m) -> int | None:
    s = (m.get("season") or "")[:4]
    return int(s) if s.isdigit() else None


def replay(games: list[dict], home_log: float, test_seasons: set[str]) -> tuple[list, list]:
    """One walk-forward pass over a league at a given home term. Returns
    (train_obs, test_obs), each a list of (p_home_win, actual). Scoring happens
    BEFORE the update, so no match informs its own prediction. The pass covers
    ALL history so ratings are warm, but only TRAIN_FROM_YEAR onward is
    returned for scoring."""
    state = M.SoccerRatingState(home_log=home_log)
    seen: collections.Counter = collections.Counter()
    train, test = [], []
    for m in games:
        home, away = m["home_team"], m["away_team"]
        scoreable = seen[home] >= MIN_MATCHES and seen[away] >= MIN_MATCHES
        dist = M.predict_and_update(state, m)
        seen[home] += 1
        seen[away] += 1
        if not scoreable or dist is None:
            continue
        yr = season_year(m)
        if yr is None or yr < TRAIN_FROM_YEAR:
            continue
        obs = (dist.prob_home_win(), 1.0 if m["home_goals_ft"] > m["away_goals_ft"] else 0.0)
        (test if m.get("season") in test_seasons else train).append(obs)
    return train, test


def bias_of(obs) -> float:
    return statistics.mean(y - p for p, y in obs)


def score(obs) -> dict:
    n = len(obs)
    resid = [y - p for p, y in obs]
    mean_res = statistics.mean(resid)
    se = statistics.stdev(resid) / math.sqrt(n) if n > 1 else 0.0
    return {
        "n": n,
        "bias": mean_res,
        "z": mean_res / se if se else 0.0,
        "brier": statistics.mean(brier(p, y) for p, y in obs),
        "ll": statistics.mean(logloss(p, y) for p, y in obs),
    }


def fit_home_log(games, test_seasons) -> float:
    """Bisect the home term that zeroes the TRAIN bias. Bias is DECREASING in
    the home term (more home advantage -> higher predicted P(home win) ->
    smaller actual-minus-predicted), so the bracket is oriented accordingly."""
    lo, hi = BISECT_LO, BISECT_HI
    for _ in range(BISECT_ITERS):
        mid = (lo + hi) / 2
        train, _ = replay(games, mid, test_seasons)
        if not train:
            return M.HOME_ADVANTAGE_LOG
        if bias_of(train) > 0:  # under-rating home -> need MORE home advantage
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main() -> None:
    matches = soccer_data.load_matches()
    by_league: dict[str, list[dict]] = collections.defaultdict(list)
    for m in matches:
        lg = m.get("division") or m.get("league")
        if not lg or m.get("home_goals_ft") is None or m.get("away_goals_ft") is None:
            continue
        by_league[lg].append(m)

    print(f"{len(matches)} matches loaded, {len(by_league)} leagues with results")
    print(f"global constant = {M.HOME_ADVANTAGE_LOG}, holdout = last {HOLDOUT_SEASONS} seasons per league\n")

    fitted: dict[str, float] = {}
    rows = []
    for lg in sorted(by_league):
        games = by_league[lg]
        games.sort(key=lambda m: (m.get("match_date") or "", m.get("home_team") or ""))
        seasons = sorted({m["season"] for m in games if m.get("season")})
        if len(seasons) <= HOLDOUT_SEASONS:
            print(f"{lg:8s} SKIPPED -- only {len(seasons)} seasons, nothing left to train on")
            continue
        test_seasons = set(seasons[-HOLDOUT_SEASONS:])

        base_train, base_test = replay(games, M.HOME_ADVANTAGE_LOG, test_seasons)
        if len(base_train) < MIN_TRAIN or len(base_test) < MIN_TEST:
            print(f"{lg:8s} SKIPPED -- train {len(base_train)} / test {len(base_test)} below minimum")
            continue

        bt = score(base_train)
        raw = fit_home_log(games, test_seasons)
        # Shrink the OFFSET (not the absolute term) toward the global constant.
        z = abs(bt["z"])
        if z <= MIN_Z:
            shrink, final = 0.0, M.HOME_ADVANTAGE_LOG
        else:
            shrink = 1.0 - 1.0 / (z * z)  # James-Stein
            final = M.HOME_ADVANTAGE_LOG + shrink * (raw - M.HOME_ADVANTAGE_LOG)

        _, new_test = replay(games, final, test_seasons)
        b0, b1 = score(base_test), score(new_test)
        changed_lg = abs(final - M.HOME_ADVANTAGE_LOG) > 1e-9
        passed = (changed_lg
                  and abs(b1["bias"]) < abs(b0["bias"])
                  and b1["brier"] <= b0["brier"]
                  and b1["ll"] <= b0["ll"])
        rows.append({"lg": lg, "train": bt, "raw": raw, "shrink": shrink, "final": final,
                     "before": b0, "after": b1, "changed": changed_lg, "passed": passed})
        if passed:
            fitted[lg] = round(final, 4)
        print(f"{lg:8s} train n={bt['n']:6d} bias {bt['bias']:+.4f} z {bt['z']:+6.1f} | "
              f"raw {raw:+.3f} shrink {shrink:.2f} -> {final:+.3f} | "
              f"TEST bias {b0['bias']:+.4f} -> {b1['bias']:+.4f}  "
              f"Brier {b0['brier']:.4f} -> {b1['brier']:.4f}  LL {b0['ll']:.4f} -> {b1['ll']:.4f}"
              + ("  SHIP" if passed else ("  REJECTED on holdout" if changed_lg else "")))

    changed = [r for r in rows if r["changed"]]
    shipped = [r for r in rows if r["passed"]]
    print("\n" + "=" * 100)
    print(f"HELD-OUT VERDICT: {len(changed)} of {len(rows)} leagues cleared the TRAIN significance guard, "
          f"{len(shipped)} survived their own held-out check\n")
    for r in changed:
        b0, b1 = r["before"], r["after"]
        tag = "SHIP" if r["passed"] else "REJECT"
        print(f"  {tag:7s} {r['lg']:6s} home_log {M.HOME_ADVANTAGE_LOG:.3f} -> {r['final']:.3f}   "
              f"holdout n={b0['n']:5d}  |bias| {abs(b0['bias']):.4f} -> {abs(b1['bias']):.4f}  "
              f"Brier {b0['brier']:.4f} -> {b1['brier']:.4f}  LL {b0['ll']:.4f} -> {b1['ll']:.4f}")
    if not shipped:
        print("\nnothing survived -- the global constant stands, nothing written")
        return

    OUT_PATH.write_text(json.dumps(fitted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {len(fitted)} per-league term(s) to {OUT_PATH.name}: {fitted}")
    print("every other league keeps the global constant, which the era table shows is")
    print("already unbiased on modern football.")


if __name__ == "__main__":
    main()
