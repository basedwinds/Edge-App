"""CFB expected margin is LINEAR in elo_diff, but real margins saturate.

WHY THIS WAS LOOKED FOR. After the truncated-schedule fix, CFB still sat ~50pp
above the market on season-long markets, and the largest single edge on the whole
board was a TCU spread. The question was whether the RATING SCALE itself breaks
down at the top -- IU is rated 2239 with a 112-point gap to second, and
expected_margin turns that into a 75-point win over a median FBS team. That is
not a football score, so either the ratings or the margin model is wrong at the
extremes.

It is the margin model.

    expected_margin(elo_diff) = MARGIN_SLOPE * elo_diff      (0.08569, no intercept)

MARGIN_SLOPE was fitted through the origin across ALL games. 60% of games sit
inside |elo_diff| < 200, so the fit is dominated by the middle and is genuinely
excellent there -- and badly wrong outside it, in exactly the tail this app bets.

MEASURED, 4,836 games walked forward with the production Elo:

    elo_diff       n     actual margin   linear predicts   error
      0..200    1813          8.09             8.34        -0.25   <- fitted here
    200..400    1053         15.90            24.65        -8.74
    400..600     389         24.88            41.09       -16.20
    600..800      97         29.60            57.46       -27.86

Real margins flatten because football saturates: a team up 40 empties its bench,
the clock runs, and the scoreboard stops. A straight line cannot represent that,
and extrapolating one 800 Elo out predicts a 57-point win where the truth is 30.

WHAT IT AFFECTS. SPREAD markets directly -- P(cover) is read off a Normal centred
on this number. TCU vs UNC sits at ~400 elo_diff: the model centres on 34.3
points where comparable games average ~25, which inflates P(win by >7.5) from
roughly 0.82 to 0.92.

WHAT IT DOES NOT AFFECT: win totals and moneyline, which read the LOGISTIC
win_prob, not the margin. That path is measured here too and is a much smaller
problem -- about +4pp of over-confidence at large gaps, which is what the shipped
T=1.26 temperature already exists to soften.

NOT FIXED HERE. Replacing a linear margin with a saturating one (a fitted curve,
or a cap) is a live-pricing change that needs its own refit and validation --
including re-deriving MARGIN_STD, which was measured as the residual around the
LINEAR fit and will change with the shape. This script is the evidence and the
reproduction.

Run: backend/.venv/Scripts/python.exe scripts/check_cfb_margin_saturation.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

import app.models.game_lines_cfb as G  # noqa: E402
from app.models.baseline import elo_service_cfb as service  # noqa: E402
from app.models.baseline.elo_cfb import (  # noqa: E402
    EloState, effective_home_field_adv, update_ratings, win_prob,
)

BUCKETS = [(0, 200), (200, 400), (400, 600), (600, 800), (800, 10_000)]


def main() -> None:
    games = [g for g in service._historical_games()
             if g.get("home_score") is not None and g.get("away_score") is not None]
    games.sort(key=lambda x: (x["gameday"], str(x["id"])))

    state = EloState()
    diffs, margins, probs, outcomes = [], [], [], []
    for g in games:
        hfa = effective_home_field_adv(bool(g.get("neutral")))
        rh, ra = state.get(g["home_team"]), state.get(g["away_team"])
        diffs.append((rh + hfa) - ra)
        margins.append(g["home_score"] - g["away_score"])
        probs.append(win_prob(rh, ra, hfa))
        outcomes.append(1.0 if g["home_score"] > g["away_score"]
                        else (0.5 if g["home_score"] == g["away_score"] else 0.0))
        update_ratings(state, g["home_team"], g["away_team"],
                       g["home_score"], g["away_score"], hfa)

    d = np.array(diffs); m = np.array(margins)
    p = np.array(probs); y = np.array(outcomes)

    print(f"{len(games)} games, seasons "
          f"{min(g['season'] for g in games)}-{max(g['season'] for g in games)}")
    print(f"elo_diff spread: p1={np.percentile(d,1):.0f}  p50={np.percentile(d,50):.0f}  "
          f"p99={np.percentile(d,99):.0f}  max={d.max():.0f}")
    print(f"share of games inside |elo_diff|<200: {(np.abs(d)<200).mean():.1%}"
          "   <- where MARGIN_SLOPE was effectively fitted\n")

    print(f"MARGIN (used by SPREAD markets).  MARGIN_SLOPE={G.MARGIN_SLOPE}")
    print(f"{'elo_diff':>14}{'n':>7}{'actual margin':>16}{'linear says':>14}{'error':>10}")
    for lo, hi in BUCKETS:
        k = (d >= lo) & (d < hi)
        if k.sum() < 20:
            continue
        act = m[k].mean()
        pred = G.MARGIN_SLOPE * d[k].mean()
        label = f"{lo}..{hi}" if hi < 9000 else f">{lo}"
        print(f"{label:>14}{int(k.sum()):>7}{act:>16.2f}{pred:>14.2f}{act - pred:>+10.2f}")

    print(f"\nWIN PROBABILITY (used by MONEYLINE and WIN TOTALS) -- the logistic, "
          "not the margin")
    print(f"{'elo_diff':>14}{'n':>7}{'predicted':>12}{'actual':>10}{'over by':>10}")
    for lo, hi in BUCKETS:
        k = (d >= lo) & (d < hi)
        if k.sum() < 20:
            continue
        label = f"{lo}..{hi}" if hi < 9000 else f">{lo}"
        print(f"{label:>14}{int(k.sum()):>7}{p[k].mean():>12.4f}{y[k].mean():>10.4f}"
              f"{p[k].mean() - y[k].mean():>+10.4f}")

    print("\nREAD IT THIS WAY. The margin error grows without bound as the gap widens --")
    print("that is a SHAPE failure, a straight line where the truth bends. The win-")
    print("probability error stays around 4pp -- a CALIBRATION wobble the shipped")
    print("T=1.26 already addresses. They are different problems of very different")
    print("size, and only the first is large enough to move the top of the board.")


if __name__ == "__main__":
    main()
