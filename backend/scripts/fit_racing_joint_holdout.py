"""Joint fit of (grid_pts, attrition) for racing, scored on BOTH the markets we
price, and validated on races the fit never saw.

WHY THIS EXISTS. Two separate failures led here.

  1. grid_pts/con_w were originally fitted on P(1st) alone. Top-N was never in
     the objective, so nothing ever pulled the finishing-order distribution
     toward being right -- and it isn't: measured walk-forward over 142 NASCAR
     races with real grids, top-20 said 92% where the truth was 69%.
     The model didn't fail; it was never asked the question.

  2. The first repair attempt (fit_racing_attrition.py) fitted attrition ALONE
     against top-N calibration. It ran to an absurd 0.55 -- more than half the
     field having a race-ruining event every race -- because uniform attrition
     barely changes RELATIVE win odds, so win Brier was flat across every rate
     and could not push back. It was an unconstrained variance knob.

Plackett-Luce with one strength per driver has a single parameter governing both
"who is best" and "how random the race is", so the head and the tail cannot be
tuned independently. Adding attrition gives a second knob that moves the tail
without reordering the head. Fitting them TOGETHER is what makes the pair
identifiable: grid_pts strongly affects win Brier, so the winner-model
constraint actually binds now, and attrition no longer has to do all the work
alone.

OBJECTIVE. Minimise top-N calibration error subject to win Brier staying within
WIN_BRIER_TOLERANCE of the baseline. A constraint rather than a weighted sum
because the two quantities have no common scale, and because "don't damage the
market that already works" is genuinely a hard requirement, not a preference.

HOLD-OUT. Fit on the earlier TRAIN_FRAC of each series' history, report on the
remainder. The previous attempt was in-sample on the same races the error was
measured with, which guarantees some improvement whether or not anything real
was learned. A parameter that only helps in-sample is not a fix.

Ratings are always walk-forward within whichever split is being scored, so a
race is never priced using its own result.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import racing_sim
from app.models.baseline.racing_ratings import (
    BASE, K_CON, K_DRIVER, PARAMS, SEASON_REGRESSION, _pairwise,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
WARMUP = 15
TRIALS = 800
TOP_NS = (3, 5, 10, 20)
TRAIN_FRAC = 0.70
WIN_BRIER_TOLERANCE = 1.02

GRID_PTS = (10.0, 20.0, 30.0, 40.0)
ATTRITION = (0.0, 0.10, 0.20, 0.30)

BUCKETS = [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
           (0.20, 0.35), (0.35, 0.60), (0.60, 1.01)]
MIN_BUCKET = 20


def load(series: str) -> list[dict]:
    path = DATA_DIR / f"racing_{series}.json"
    if not path.exists():
        return []
    races = list(json.loads(path.read_text(encoding="utf-8")).values())
    races.sort(key=lambda r: (r.get("date") or "", r["id"]))
    return races


def bucket_of(p: float):
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def evaluate(races, gp, cw, attrition, score_from=0, score_to=None):
    """Walk forward over ALL races (ratings must accumulate from the start), but
    only SCORE those in [score_from, score_to). That is what makes a clean
    train/test split possible without giving the test split cold ratings."""
    if score_to is None:
        score_to = len(races)
    drv, con = {}, {}
    cur_season = None
    agg = defaultdict(lambda: [0.0, 0, 0])
    win_se = win_n = 0.0
    scored = 0

    for i, race in enumerate(races):
        if race.get("season") != cur_season:
            cur_season = race.get("season")
            for d in drv:
                drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)
            for c in con:
                con[c] = BASE + (1 - SEASON_REGRESSION) * (con[c] - BASE)

        results = race["results"]
        field = [r["driver_id"] for r in results]
        in_window = score_from <= i < score_to
        if len(field) >= 2 and i >= WARMUP and in_window and \
                any(r.get("start_order") is not None for r in results):
            strength = {}
            for r in results:
                d = r["driver_id"]
                s = drv.get(d, BASE)
                c = r.get("constructor")
                if c is not None:
                    s += cw * (con.get(c, BASE) - BASE)
                g = r.get("start_order")
                if g is not None and gp:
                    s -= gp * (g - 1)
                strength[d] = s
            sim = racing_sim.simulate(strength, trials=TRIALS, top_ns=(1,) + TOP_NS,
                                      seed=i, attrition=attrition)
            if sim:
                scored += 1
                n_field = len(results)
                for r in results:
                    got = sim.get(r["driver_id"]) or {}
                    wp = got.get("win")
                    if wp is not None:
                        win_se += (wp - (1.0 if r.get("winner") else 0.0)) ** 2
                        win_n += 1
                    for n in TOP_NS:
                        if n >= n_field:
                            continue
                        p = got.get(f"top{n}")
                        if p is None:
                            continue
                        b = bucket_of(p)
                        if b is None:
                            continue
                        a = agg[(n, b)]
                        a[0] += p
                        a[1] += int(r["order"] <= n)
                        a[2] += 1

        d_rat = {d: drv.get(d, BASE) for d in field}
        order = {r["driver_id"]: r["order"] for r in results}
        drv.update(_pairwise(field, order, d_rat, K_DRIVER))
        best = {}
        for r in results:
            c = r.get("constructor")
            if c and (c not in best or r["order"] < best[c]):
                best[c] = r["order"]
        if len(best) > 1:
            c_rat = {c: con.get(c, BASE) for c in best}
            con.update(_pairwise(list(best), best, c_rat, K_CON))

    num = den = 0.0
    for (_n, _b), (psum, hits, cnt) in agg.items():
        if cnt < MIN_BUCKET:
            continue
        num += cnt * abs(psum / cnt - hits / cnt)
        den += cnt
    return (num / den if den else float("nan"),
            win_se / win_n if win_n else float("nan"), scored)


def run(series: str) -> None:
    races = load(series)
    if not races:
        print(f"\n=== {series}: no data ===")
        return
    split = int(len(races) * TRAIN_FRAC)
    cw = PARAMS[series]["con_w"]
    ship_gp = PARAMS[series]["grid_pts"]
    print(f"\n=== {series} === {len(races)} races, train<{split} test>={split}, con_w={cw}")

    base_cal, base_brier, _ = evaluate(races, ship_gp, cw, 0.0, 0, split)
    print(f"TRAIN baseline (grid_pts={ship_gp}, attrition=0): cal={base_cal:.4f} brier={base_brier:.5f}")

    rows = []
    for gp in GRID_PTS:
        for att in ATTRITION:
            cal, brier, _ = evaluate(races, gp, cw, att, 0, split)
            rows.append((gp, att, cal, brier))
            ok = "" if brier <= base_brier * WIN_BRIER_TOLERANCE else "  (win Brier fails)"
            print(f"   grid_pts={gp:5.1f} attrition={att:4.2f}  cal={cal:.4f} brier={brier:.5f}{ok}")

    feasible = [r for r in rows if r[3] <= base_brier * WIN_BRIER_TOLERANCE]
    if not feasible:
        print("   no candidate keeps win Brier within tolerance")
        return
    gp, att, cal, brier = min(feasible, key=lambda r: r[2])
    print(f"  -> TRAIN pick: grid_pts={gp}, attrition={att} (cal {base_cal:.4f} -> {cal:.4f})")

    t_base_cal, t_base_brier, n = evaluate(races, ship_gp, cw, 0.0, split)
    t_cal, t_brier, _ = evaluate(races, gp, cw, att, split)
    print(f"  HOLD-OUT ({n} races the fit never saw):")
    print(f"     shipped  grid_pts={ship_gp:5.1f} attrition=0.00  cal={t_base_cal:.4f} brier={t_base_brier:.5f}")
    print(f"     fitted   grid_pts={gp:5.1f} attrition={att:4.2f}  cal={t_cal:.4f} brier={t_brier:.5f}")
    verdict = "IMPROVES" if t_cal < t_base_cal and t_brier <= t_base_brier * WIN_BRIER_TOLERANCE else "DOES NOT HOLD UP"
    print(f"     -> {verdict} out of sample")


def main() -> None:
    for series in (sys.argv[1:] or ["nascar"]):
        run(series)


if __name__ == "__main__":
    main()
