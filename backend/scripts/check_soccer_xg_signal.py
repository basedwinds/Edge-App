"""Does xG predict a team's FUTURE goals better than its own past goals do?

This is the cheap gate in front of any xG work, and it is deliberately the
FIRST thing measured. The expensive question -- would refitting Dixon-Coles on
xG beat the shipped goals-based model against real market prices -- costs days.
This one costs a query, and if xG cannot out-predict goals on the simplest
possible framing then nothing downstream can rescue it.

THE FRAMING. For each team, walk forward within a league-season keeping a
rolling mean of (a) goals scored and (b) xG generated over its ALREADY-PLAYED
matches, then predict the goals it scores in its NEXT match. Both predictors
get the same rows, the same window, and their own least-squares fit, so the
only thing varying is which history is used. Windows 5/10/20 are reported
because a single window is a parameter choice, and a signal that only appears
at one window length is a fluke -- the same reason the MLB team-scoring blend
was rejected across {5..50} rather than at one setting.

WHY EXPECT ANYTHING. Goals are a small-sample realisation of chance creation. A
side that generates 2.1 xG and scores 4 has been lucky, not transformed, and
regression will take the 4 away. This is the same relationship that made K-BB%
beat ERA for MLB starters (measured 2026-08-14): prefer the metric closest to
what the team controls and least polluted by luck.

WHAT A PASS HERE DOES AND DOES NOT LICENCE. It licenses building the real test.
It does NOT say the app's edge improves -- the market prices xG too, and
possibly better than we would. Air density and the global soccer goal scale
both looked plausible and died on measurement; this could still die at the
next step.

Data: data/soccer_xg_cache.json (build_soccer_xg_cache.py) -- Understat, free,
5 leagues that this app rates (E0/SP1/D1/I1/F1), seasons 2014-2025.

Run: backend/.venv/Scripts/python.exe scripts/check_soccer_xg_signal.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collections  # noqa: E402
import json  # noqa: E402

import numpy as np  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "soccer_xg_cache.json"
WINDOWS = (5, 10, 20)


def _rmse_of_best_fit(pred: np.ndarray, actual: np.ndarray) -> float:
    """Least-squares fit of `actual ~ a*pred + b`, then the residual RMSE. Each
    predictor gets its OWN intercept and slope on purpose: xG and goals live on
    slightly different scales, and comparing them raw would measure the scale
    difference rather than the information content."""
    design = np.column_stack([pred, np.ones_like(pred)])
    coef, *_ = np.linalg.lstsq(design, actual, rcond=None)
    return float(np.sqrt(np.mean((actual - design @ coef) ** 2)))


def main() -> None:
    if not CACHE_PATH.exists():
        raise SystemExit(f"missing {CACHE_PATH} -- run build_soccer_xg_cache.py first")
    cache = json.loads(CACHE_PATH.read_text())

    ordered = []
    for league, seasons in cache.items():
        for season, matches in seasons.items():
            for m in sorted(matches, key=lambda x: x["date"]):
                ordered.append((league, season, m))

    res = {w: {"goals": [], "xg": [], "actual": []} for w in WINDOWS}
    history: dict[str, dict[str, list]] = collections.defaultdict(lambda: {"g": [], "x": []})
    current = None
    for league, season, m in ordered:
        # Reset per league-season: carrying form across a summer break, or
        # across leagues for a promoted club, would leak the wrong history.
        if (league, season) != current:
            history.clear()
            current = (league, season)
        for side in ("h", "a"):
            team = m["home"] if side == "h" else m["away"]
            goals_for, xg_for = m[f"goals_{side}"], m[f"xg_{side}"]
            past = history[team]
            for w in WINDOWS:
                if len(past["g"]) >= w:
                    res[w]["goals"].append(float(np.mean(past["g"][-w:])))
                    res[w]["xg"].append(float(np.mean(past["x"][-w:])))
                    res[w]["actual"].append(goals_for)
            past["g"].append(goals_for)
            past["x"].append(xg_for)

    total = sum(len(s) for lg in cache.values() for s in lg.values())
    print(f"{total} matches across {len(cache)} leagues: {', '.join(sorted(cache))}")
    print("Predicting a team's NEXT-match goals from its own rolling history.")
    print("Walk-forward within each league-season. Lower RMSE is better.\n")
    print(f"{'window':>7}{'n':>9}{'corr goals':>13}{'corr xG':>10}"
          f"{'RMSE goals':>13}{'RMSE xG':>10}{'delta':>9}")

    deltas = []
    for w in WINDOWS:
        g = np.array(res[w]["goals"])
        x = np.array(res[w]["xg"])
        y = np.array(res[w]["actual"])
        rmse_g, rmse_x = _rmse_of_best_fit(g, y), _rmse_of_best_fit(x, y)
        deltas.append(rmse_x - rmse_g)
        print(f"{w:>7}{len(y):>9}{np.corrcoef(g, y)[0, 1]:>13.4f}"
              f"{np.corrcoef(x, y)[0, 1]:>10.4f}{rmse_g:>13.4f}{rmse_x:>10.4f}"
              f"{rmse_x - rmse_g:>+9.4f}")

    print()
    if all(d < 0 for d in deltas):
        print("PASS: xG out-predicts goals at EVERY window tested. Consistency across")
        print("windows is what matters here -- a single-window win would be a parameter")
        print("choice, not a signal.")
    else:
        print("MIXED/FAIL: xG does not beat goals at every window. Do not build on this.")
    print()
    print("Honest scale: the RMSE gain is well under 1%. This is a real but MODEST")
    print("edge in the input, exactly like K-BB% over ERA -- it justifies the next")
    print("test, not a claim that the model or the board improves.")


if __name__ == "__main__":
    main()
