"""Does feeding xG instead of GOALS into the soccer ratings predict OUTCOMES better?

WHAT WAS ALREADY ESTABLISHED (#167 feasibility, 2026-08-14). Understat xG is
reachable free (only with an `X-Requested-With: XMLHttpRequest` header; a plain
GET 404s) and cached at data/soccer_xg_cache.json -- 21,589 played matches,
2014-2025, five leagues E0/SP1/D1/I1/F1. Rolling-window check: xG beat goals at
predicting a team's NEXT-match goals on both RMSE and correlation at all three
windows (5/10/20).

WHAT THAT DID NOT ESTABLISH, and why this script exists. Predicting a team's next
GOALS is not the same as pricing a MATCH. The app sells 1X2 / totals / BTTS
probabilities, and the RMSE gain was under 1%. A sub-1% improvement in a
component can vanish, or invert, once it passes through a Dixon-Coles grid into
an outcome probability. So this scores what is actually sold: match OUTCOME.

THE INTERVENTION IS ONE ARGUMENT. `update_ratings` drives attack/concede off
`_pearson_residual(observed, expected)`. Arm A passes real goals (production).
Arm B passes xG. Everything else -- the same production `predict_match`,
`SoccerRatingState`, rho, K constants, season regression -- is untouched, so any
difference is the residual signal and not a reimplementation.

THE LEAGUE BASELINE STAYS ON REAL GOALS in both arms (`goals_sum`/`goals_n`).
The model predicts real goals; xG is used as a better estimate of a team's true
scoring RATE, not as a replacement for the level being predicted. Feeding xG into
the baseline too would shift the scale being scored against and confound the test.

COVERAGE CEILING, stated up front: five of thirty-three rated leagues. This can
only ever upgrade the best-traded five and is never a whole-model answer.

STILL NOT ESTABLISHED EVEN IF THIS PASSES: beating the MARKET. Bookmakers price
xG too, possibly better. A win here is necessary, not sufficient.

Run: backend/.venv/Scripts/python.exe scripts/fit_soccer_xg_ratings.py
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.baseline.elo_soccer import (  # noqa: E402
    SoccerRatingState, predict_match, update_ratings,
)

CACHE = Path(__file__).resolve().parents[2] / "data" / "soccer_xg_cache.json"
# Train on the earlier years, hold out the rest. Split by SEASON, never by
# shuffling: a rating model carries state forward, so a random split would let
# later matches inform earlier predictions.
TRAIN_THROUGH = 2021


def load():
    d = json.loads(CACHE.read_text(encoding="utf-8"))
    out = {}
    for lg, seasons in d.items():
        rows = []
        for season, matches in seasons.items():
            for m in matches:
                if m.get("goals_h") is None or m.get("goals_a") is None:
                    continue
                if m.get("xg_h") is None or m.get("xg_a") is None:
                    continue
                rows.append({**m, "season": int(season)})
        rows.sort(key=lambda r: (r["season"], r["date"]))
        out[lg] = rows
    return out


def logloss(p, y):
    eps = 1e-12
    return -math.log(max(min(p[y], 1 - eps), eps))


def brier(p, y):
    t = [0.0, 0.0, 0.0]
    t[y] = 1.0
    return sum((a - b) ** 2 for a, b in zip(p, t))


def replay(rows, use_xg: bool):
    """Walk-forward through one league. Returns per-match (probs, outcome, season).

    Production predict_match / update_ratings / SoccerRatingState throughout --
    the ONLY difference between arms is what `observed` is fed to the residual."""
    state = SoccerRatingState()
    out = []
    for m in rows:
        state.start_season_if_new(m["season"])
        dist = predict_match(state, m["home"], m["away"])
        gh, ga = m["goals_h"], m["goals_a"]
        y = 0 if gh > ga else (1 if gh == ga else 2)
        out.append((
            (dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win()),
            y, m["season"],
        ))
        if use_xg:
            update_ratings(state, m["home"], m["away"], m["xg_h"], m["xg_a"])
            # the LEVEL being predicted stays real goals in both arms
            state.goals_sum += (gh + ga) - (m["xg_h"] + m["xg_a"])
        else:
            update_ratings(state, m["home"], m["away"], gh, ga)
    return out


def score(rows, seasons=None):
    sel = [r for r in rows if seasons is None or r[2] in seasons]
    if not sel:
        return None
    n = len(sel)
    return (sum(logloss(p, y) for p, y, _ in sel) / n,
            sum(brier(p, y) for p, y, _ in sel) / n,
            sum(1 for p, y, _ in sel if max(range(3), key=lambda i: p[i]) == y) / n,
            n)


def main() -> None:
    data = load()
    print(f"leagues: {', '.join(sorted(data))}")
    print(f"matches: {sum(len(v) for v in data.values())} with BOTH goals and xG")

    all_goals, all_xg = [], []
    per_league = {}
    for lg, rows in sorted(data.items()):
        g = replay(rows, use_xg=False)
        x = replay(rows, use_xg=True)
        all_goals += g
        all_xg += x
        per_league[lg] = (g, x)

    test_seasons = {s for _, _, s in all_goals if s > TRAIN_THROUGH}
    print(f"train seasons <= {TRAIN_THROUGH}, held-out {sorted(test_seasons)}")

    for label, seasons in (("TRAIN (in sample)", {s for _, _, s in all_goals if s <= TRAIN_THROUGH}),
                           ("HELD OUT", test_seasons)):
        sg, sx = score(all_goals, seasons), score(all_xg, seasons)
        print(f"\n{label}  n={sg[3]}")
        print(f"{'arm':>8}{'logloss':>11}{'brier':>10}{'accuracy':>11}")
        print(f"{'goals':>8}{sg[0]:>11.5f}{sg[1]:>10.5f}{sg[2]:>11.4f}")
        print(f"{'xG':>8}{sx[0]:>11.5f}{sx[1]:>10.5f}{sx[2]:>11.4f}")
        print(f"{'delta':>8}{sx[0]-sg[0]:>+11.5f}{sx[1]-sg[1]:>+10.5f}{sx[2]-sg[2]:>+11.4f}"
              f"   (negative logloss/brier = xG better)")

    print(f"\nPER LEAGUE, HELD-OUT ONLY -- consistency is the point. A single league")
    print(f"winning is a coin flip; five of five is a finding.")
    print(f"{'league':>8}{'n':>7}{'logloss goals':>15}{'logloss xG':>12}{'delta':>10}  better")
    wins = 0
    for lg, (g, x) in sorted(per_league.items()):
        sg, sx = score(g, test_seasons), score(x, test_seasons)
        if sg is None:
            continue
        d = sx[0] - sg[0]
        wins += d < 0
        print(f"{lg:>8}{sg[3]:>7}{sg[0]:>15.5f}{sx[0]:>12.5f}{d:>+10.5f}  {'xG' if d < 0 else 'goals'}")

    sg, sx = score(all_goals, test_seasons), score(all_xg, test_seasons)
    print()
    if sx[0] < sg[0] and sx[1] < sg[1] and wins >= 4:
        print(f"  SHIP-WORTHY: xG better on BOTH logloss and Brier out of sample,")
        print(f"  and in {wins}/5 leagues individually.")
    elif sx[0] < sg[0] and sx[1] < sg[1]:
        print(f"  MIXED: better in aggregate on both measures but only {wins}/5 leagues.")
        print(f"  Aggregate wins driven by one or two leagues are how a parameter")
        print(f"  choice gets mistaken for a finding -- look at which.")
    else:
        print(f"  DO NOT SHIP: xG does not improve OUTCOME prediction out of sample")
        print(f"  ({wins}/5 leagues). Beating goals at predicting GOALS did not survive")
        print(f"  the trip through the Dixon-Coles grid into an outcome probability.")


if __name__ == "__main__":
    main()
