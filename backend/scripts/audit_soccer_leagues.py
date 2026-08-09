"""Per-league audit of the soccer model. (Task #127, first slice.)

THE QUESTION THE USER ACTUALLY ASKED: are the leagues added recently as robust
as the ones built first, and is anything being missed?

DEPTH WAS ALREADY ANSWERED and is not re-done here: every domestic league
carries 111-588 matches per team (thinnest new ones CHN1 111, SWE1 152; the
originals sit at E0 312, SP1 350, I1 272). Only INTL is an outlier at 24, which
is #126. So depth is NOT the differentiator.

WHAT THIS TESTS INSTEAD is calibration per league, which depth cannot tell you:

  1. SYSTEMATIC BIAS. Mean residual (actual - predicted) on P(home win), per
     league. A non-zero mean is a mispricing in ONE DIRECTION on every match in
     that league. It never looks wrong on any single bet, so it survives
     indefinitely -- exactly how CoD's +2.9pp first-listed bias went unnoticed
     until it was measured.

     For soccer this is the sharpest possible probe, because home advantage is
     a SINGLE GLOBAL CONSTANT in this model. Home advantage genuinely differs by
     country (a well-documented effect), so if the constant is wrong for a
     league, it shows up here as a per-league bias and nowhere else.

  2. SKILL vs BASELINE. Brier and log-loss against that league's own base rate
     (its actual home-win frequency). Beating a base rate that already knows the
     league's home-win share is a real bar -- it means the model is separating
     TEAMS, not just re-learning that home teams win.

WALK-FORWARD, per league. The service keeps one rating state per league, so
each is replayed independently in date order, scoring each match before
updating from it. MIN_MATCHES guards the warm-up.

READ THE BIAS COLUMN FIRST. A league can have plenty of data, beat its base
rate, and still be systematically tilted -- those are different failures and
only the third column shows the tilt.

===========================================================================
RESULT, 2026-08-09. TWO FINDINGS, and the second is the important one.

1. EVERY league beats its own base rate on BOTH Brier and log-loss -- all 28,
   including every recently-added one. The model genuinely separates teams
   rather than re-learning that home sides win. On this measure the new
   leagues are indistinguishable from the originals:
       CHN1 Brier 0.2184 vs base 0.2484      E0 0.2199 vs 0.2481
       SWE1 0.2214 vs 0.2453                 SP1 0.2275 vs 0.2493
       BRA1 0.2420 vs 0.2498                 I1  0.2192 vs 0.2478

2. 17 OF 28 LEAGUES ARE SYSTEMATICALLY TILTED (|z| > 3), and almost all in the
   SAME direction: the model UNDER-rates home teams.

       G1   +0.0628  z +11.1      MLS  +0.0379  z +4.7
       BRA1 +0.0640  z  +9.5      P1   +0.0328  z +6.6
       SP1  +0.0408  z  +9.5      T1   +0.0284  z +5.8
       F1   +0.0369  z  +8.1      JPN1 -0.0247  z -3.4   <- the only negative
       ...                        SC0  -0.0023  z -0.4   <- unbiased

   THE CAUSE IS STRUCTURAL, not per-league data quality: home advantage is a
   single GLOBAL CONSTANT in this model. Home advantage genuinely differs by
   country -- and the ranking here recovers that unprompted. Greece, Brazil,
   Spain, Turkey, Argentina and MLS (long-haul travel) sit at the top; Japan,
   famously low home advantage, is the one league the model OVER-rates;
   Scotland and England League Two come out unbiased.

   A model fitting noise would not order countries the way the football
   literature does.

   MAGNITUDE: G1 and BRA1 are tilted +6.3pp -- more than TWICE the +2.9pp CoD
   bias that was worth fixing, on the largest live board in the app.

WHAT THIS SETTLES ABOUT THE ORIGINAL QUESTION. The recently-added leagues are
NOT worse than the originals. SP1 (original, z +9.5) is more tilted than CHN1
(new, z +1.7). The tilt tracks the COUNTRY's real home advantage, not when the
league was wired up. So the answer is: the new leagues are fine, and the model
has a global structural gap that has always affected the originals too.

THE FIX, and it should be fitted rather than eyeballed: a per-league home
term, fitted on each league's own residual and validated on held-out seasons
before shipping. See the task raised from this run.
===========================================================================
RESOLVED 2026-08-09 by scripts/fit_soccer_home_advantage.py -- and finding 2
above turned out to be MOSTLY WRONG about its own cause. Read this before
re-raising it.

The per-league tilt is an ERA effect, not a country effect. Home advantage in
football has declined steadily, and this script pools 33 seasons, so what it
measured is mostly the 1990s. Bias under the SAME global 0.20 constant, by era:

    1992-1994  +0.0472  z  +8.4     home win 47.9%
    2001-2003  +0.0312  z  +7.5              46.3%
    2010-2012  +0.0178  z  +5.3              45.1%
    2019-2021  -0.0045  z  -1.5              42.9%   <- crowdless COVID seasons
    2025-2027  +0.0047  z  +1.0              44.0%

Restricted to 2019-onward, only 3 of 26 leagues are tilted (was 17 of 28) and
the pooled bias is +0.0023. The country ORDERING did not survive either: Greece,
the most tilted league on full history (+0.0628, z +11.1), is -0.0115 (z -1.0)
in the modern era, and Serie A flipped sign. So the "the ranking recovers the
football literature" reading above is retrospectively an artifact of which
leagues have the most pre-2010 data, not a measurement of national home
advantage.

WHAT ACTUALLY SHIPPED: exactly ONE league, BRA1 (0.20 -> 0.322), the only one
tilted in the same direction in every window tested AND passing its own
held-out check. MLS cleared the fit but was REJECTED on held-out data. Its
row here is now +0.0260 z +3.9 (was +0.0640 z +9.5) with Brier 0.2420 ->
0.2386; it does not go to zero on THIS table and should not, because the fit
targets modern football and this table is dominated by the old high-home-
advantage era.

So a large full-history bias in the table below is NOT by itself grounds to
fit that league a constant. Split it by era first.
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

# Both sides need this many prior matches before a prediction is scored, so a
# league's first season does not dominate its own bias figure.
MIN_MATCHES = 10


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def main() -> None:
    matches = soccer_data.load_matches()
    by_league: dict[str, list[dict]] = collections.defaultdict(list)
    for m in matches:
        lg = m.get("division") or m.get("league")
        if not lg:
            continue
        if m.get("home_goals_ft") is None or m.get("away_goals_ft") is None:
            continue
        by_league[lg].append(m)
    print(f"{len(matches)} matches loaded, {len(by_league)} leagues with results\n")

    rows = []
    for lg, games in by_league.items():
        games.sort(key=lambda m: (m.get("match_date") or "", m.get("home_team") or ""))
        # Built the way refresh_ratings() builds it, so this audit measures the
        # home term a league ACTUALLY ships with rather than the global default.
        state = E.SoccerRatingState(home_log=E.home_advantage_for_league(lg))
        seen: collections.Counter = collections.Counter()
        resid, briers, lls, actuals, preds = [], [], [], [], []

        for m in games:
            home, away = m["home_team"], m["away_team"]
            scoreable = seen[home] >= MIN_MATCHES and seen[away] >= MIN_MATCHES
            dist = E.predict_and_update(state, m)
            seen[home] += 1
            seen[away] += 1
            if not scoreable or dist is None:
                continue
            p = dist.prob_home_win()
            y = 1.0 if m["home_goals_ft"] > m["away_goals_ft"] else 0.0
            resid.append(y - p)
            briers.append(brier(p, y))
            lls.append(logloss(p, y))
            actuals.append(y)
            preds.append(p)

        if len(resid) < 300:
            continue
        n = len(resid)
        mean_res = statistics.mean(resid)
        se = statistics.stdev(resid) / math.sqrt(n)
        base = statistics.mean(actuals)  # this league's own home-win rate
        base_brier = statistics.mean(brier(base, y) for y in actuals)
        base_ll = statistics.mean(logloss(base, y) for y in actuals)
        rows.append({
            "lg": lg, "n": n, "bias": mean_res, "z": mean_res / se if se else 0.0,
            "brier": statistics.mean(briers), "base_brier": base_brier,
            "ll": statistics.mean(lls), "base_ll": base_ll,
            "home_rate": base, "pred_home": statistics.mean(preds),
        })

    rows.sort(key=lambda r: -abs(r["z"]))
    print(f"{'league':8s}{'n':>7s}{'bias':>9s}{'z':>7s}{'homeWin':>9s}{'pred':>8s}"
          f"{'Brier':>9s}{'base':>8s}{'LogL':>8s}{'base':>8s}  verdict")
    for r in rows:
        beats = r["brier"] < r["base_brier"] and r["ll"] < r["base_ll"]
        tilt = "TILTED" if abs(r["z"]) > 3 else ("lean" if abs(r["z"]) > 2 else "ok")
        verdict = ("beats base" if beats else "NO SKILL vs base") + f" / {tilt}"
        print(f"{r['lg']:8s}{r['n']:7d}{r['bias']:+9.4f}{r['z']:+7.1f}"
              f"{r['home_rate']:9.3f}{r['pred_home']:8.3f}"
              f"{r['brier']:9.4f}{r['base_brier']:8.4f}{r['ll']:8.4f}{r['base_ll']:8.4f}  {verdict}")

    print("\nbias = mean(actual - predicted) on P(home win).")
    print("  positive -> the model UNDER-rates home teams in that league")
    print("  negative -> it OVER-rates them")
    print("|z| > 3 is a real tilt, not sampling noise.")


if __name__ == "__main__":
    main()
