"""Fit a SATURATING CFB margin curve to replace the linear one, and re-derive sigma.

THE DEFECT (measured in check_cfb_margin_saturation.py): expected_margin is
MARGIN_SLOPE * elo_diff, fitted through the origin. 59.2% of games sit inside
|elo_diff| < 200 so the fit is dominated by the middle, where it is excellent,
and it grows steadily worse outward -- 8.7 points off at 200-400, 16.2 at
400-600, 27.9 at 600-800. Football margins saturate (a team up 40 empties the
bench and the clock runs); a straight line cannot.

THE FORM. margin = A * tanh(elo_diff / B).

Chosen because it is the simplest function with the two properties the physics
demands and the line lacks: it is ODD-SYMMETRIC, so swapping home and away flips
the sign exactly and no home/away asymmetry can sneak in through the curve; and
it SATURATES, approaching +/-A rather than growing without bound. It also reduces
to a straight line of slope A/B near zero, so it cannot be worse than the
incumbent in the dense middle where the incumbent is already right.

Only two parameters, which matters: the whole failure being fixed is a model that
looked good on aggregate fit while being wrong in a sparse tail, and more
parameters would make that easier to repeat, not harder.

HOW IT IS JUDGED -- three tests, and it has to pass all three:

  1. WALK-FORWARD OUT-OF-SAMPLE. Fit on prior seasons only, score the held-out
     season. Aggregate RMSE is reported but is NOT the deciding number: RMSE is
     dominated by the same dense middle that hid the problem.
  2. PER-BUCKET BIAS. Mean residual inside each elo_diff band. This is the test
     the incumbent fails, and the one that decides.
  3. SIGMA, RE-DERIVED. MARGIN_STD (19.16) was the residual spread around the
     LINEAR fit. A different mean shape gives different residuals, so shipping a
     new mean with the old sigma would trade one bias for another. Reported
     overall AND per bucket, because if the spread itself varies with elo_diff
     then a single constant is the next thing to question.

Nothing is written to the model here. This prints the constants and the evidence.

Run: backend/.venv/Scripts/python.exe scripts/fit_cfb_margin_curve.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

import app.models.game_lines_cfb as G  # noqa: E402
from app.models.baseline import elo_service_cfb as service  # noqa: E402
from app.models.baseline.elo_cfb import (  # noqa: E402
    EloState, effective_home_field_adv, update_ratings,
)

BUCKETS = [(-10_000, -400), (-400, -200), (-200, 0), (0, 200),
           (200, 400), (400, 600), (600, 10_000)]
B_GRID = np.arange(200.0, 1600.0, 10.0)


def _walk_forward(games: list[dict]):
    state = EloState()
    out = []
    for g in games:
        hfa = effective_home_field_adv(bool(g.get("neutral")))
        diff = (state.get(g["home_team"]) + hfa) - state.get(g["away_team"])
        out.append((g["season"], diff, g["home_score"] - g["away_score"]))
        update_ratings(state, g["home_team"], g["away_team"],
                       g["home_score"], g["away_score"], hfa)
    return out


def _fit_tanh(d: np.ndarray, m: np.ndarray) -> tuple[float, float]:
    """Grid B, solve A in closed form. For fixed B the model is linear in A, so
    least squares is exact -- no optimiser, no starting-point sensitivity, and
    nothing that can silently converge to a boundary the way a bounded bisection
    once did on the soccer goal scale."""
    best = None
    for b in B_GRID:
        t = np.tanh(d / b)
        denom = float(np.sum(t * t))
        if denom <= 0:
            continue
        a = float(np.sum(t * m) / denom)
        sse = float(np.sum((m - a * t) ** 2))
        if best is None or sse < best[0]:
            best = (sse, a, b)
    return best[1], best[2]


def _label(lo: int, hi: int) -> str:
    if lo < -9000:
        return f"< {hi}"
    if hi > 9000:
        return f"> {lo}"
    return f"{lo}..{hi}"


def main() -> None:
    games = [g for g in service._historical_games()
             if g.get("home_score") is not None and g.get("away_score") is not None]
    games.sort(key=lambda x: (x["gameday"], str(x["id"])))
    rows = _walk_forward(games)
    seasons = sorted({r[0] for r in rows})

    d_all = np.array([r[1] for r in rows])
    m_all = np.array([r[2] for r in rows])
    a_all, b_all = _fit_tanh(d_all, m_all)
    print(f"{len(rows)} games, seasons {seasons[0]}-{seasons[-1]}")
    print(f"\nFULL-SAMPLE FIT:  margin = {a_all:.3f} * tanh(elo_diff / {b_all:.0f})")
    print(f"   near-zero slope = {a_all / b_all:.5f}   "
          f"(incumbent MARGIN_SLOPE = {G.MARGIN_SLOPE})")
    print(f"   saturates at +/-{a_all:.1f} points")

    print("\n1) WALK-FORWARD OUT-OF-SAMPLE (fit on prior seasons, score held-out)")
    print(f"{'season':>8}{'n':>7}{'RMSE linear':>14}{'RMSE tanh':>12}{'delta':>9}")
    lin_res, tanh_res = [], []
    for s in seasons[1:]:
        tr = [r for r in rows if r[0] < s]
        te = [r for r in rows if r[0] == s]
        if len(tr) < 300 or len(te) < 100:
            continue
        dtr = np.array([r[1] for r in tr]); mtr = np.array([r[2] for r in tr])
        dte = np.array([r[1] for r in te]); mte = np.array([r[2] for r in te])
        slope = float(np.sum(dtr * mtr) / np.sum(dtr * dtr))   # through-origin, as shipped
        a, b = _fit_tanh(dtr, mtr)
        r_lin = float(np.sqrt(np.mean((mte - slope * dte) ** 2)))
        r_tanh = float(np.sqrt(np.mean((mte - a * np.tanh(dte / b)) ** 2)))
        lin_res.append((mte - slope * dte, dte))
        tanh_res.append((mte - a * np.tanh(dte / b), dte))
        print(f"{s:>8}{len(te):>7}{r_lin:>14.4f}{r_tanh:>12.4f}{r_tanh - r_lin:>+9.4f}")

    res_lin = np.concatenate([r[0] for r in lin_res])
    res_tanh = np.concatenate([r[0] for r in tanh_res])
    d_oos = np.concatenate([r[1] for r in lin_res])
    print(f"{'ALL':>8}{len(res_lin):>7}"
          f"{np.sqrt(np.mean(res_lin**2)):>14.4f}{np.sqrt(np.mean(res_tanh**2)):>12.4f}"
          f"{np.sqrt(np.mean(res_tanh**2)) - np.sqrt(np.mean(res_lin**2)):>+9.4f}")

    print("\n2) PER-BUCKET BIAS on the SAME held-out predictions (the deciding test)")
    print(f"{'elo_diff':>14}{'n':>7}{'bias linear':>14}{'bias tanh':>12}")
    for lo, hi in BUCKETS:
        k = (d_oos >= lo) & (d_oos < hi)
        if k.sum() < 20:
            continue
        print(f"{_label(lo, hi):>14}{int(k.sum()):>7}"
              f"{res_lin[k].mean():>+14.2f}{res_tanh[k].mean():>+12.2f}")

    print("\n3) SIGMA, re-derived around the NEW mean")
    print(f"   incumbent MARGIN_STD (around the linear fit) = {G.MARGIN_STD}")
    print(f"   out-of-sample residual sd, linear = {res_lin.std():.2f}")
    print(f"   out-of-sample residual sd, tanh   = {res_tanh.std():.2f}   <- proposed")
    print(f"\n   per-bucket residual sd (is one constant sigma even right?)")
    print(f"{'elo_diff':>14}{'n':>7}{'sd linear':>12}{'sd tanh':>10}")
    for lo, hi in BUCKETS:
        k = (d_oos >= lo) & (d_oos < hi)
        if k.sum() < 20:
            continue
        print(f"{_label(lo, hi):>14}{int(k.sum()):>7}"
              f"{res_lin[k].std():>12.2f}{res_tanh[k].std():>10.2f}")

    print("\nPROPOSED CONSTANTS (full-sample, to ship only if the tests above pass):")
    print(f"   MARGIN_TANH_SCALE = {a_all:.4f}")
    print(f"   MARGIN_TANH_WIDTH = {b_all:.1f}")
    print(f"   MARGIN_STD        = {res_tanh.std():.2f}")


if __name__ == "__main__":
    main()
