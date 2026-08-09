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
        state = E.SoccerRatingState()
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
