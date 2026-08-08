"""Fit racing_sim's `attrition` rate per series.

WHY. racing_sim's strength spread was fitted on P(1st) only, so nothing ever
constrained the deep tail of the finishing order. Measured walk-forward with real
grids (check_racing_topn_calibration.py), top-N is badly miscalibrated and the
error grows with N: NASCAR top-20 said 92% where the truth was 69%, top-10 said
3% where the truth was 15%. A top-20 market in a 40-car field is mostly a bet on
whether the car survives, and survival was not simulated.

WHAT IS FITTED. One number per series: the per-race probability a driver suffers
a race-ruining event and is classified behind everyone who finished.

OBJECTIVE. Mean absolute calibration error over top-N buckets -- "when the model
says 12%, does it happen 12%?" -- NOT winner-hit, for the reason the rest of this
codebase already settled: the app bets probability against price and never picks
a driver. Buckets are weighted by how many predictions land in them, so a band
with 2,000 predictions counts more than one with 30.

THE GUARD THAT MATTERS. Attrition necessarily lowers a favourite's win
probability, and race_winner is currently the one racing market that IS well
fitted. So this also reports win Brier at every candidate rate. A rate that fixes
top-N by wrecking the winner model is not an improvement, and the chosen rate has
to leave win Brier essentially unchanged.

Walk-forward throughout: ratings only ever see races before the one scored, and
each race is priced off its own real starting grid.
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
TRIALS = 1500
TOP_NS = (3, 5, 10, 20)
RATES = (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35)
BUCKETS = [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
           (0.20, 0.35), (0.35, 0.60), (0.60, 1.01)]
MIN_BUCKET = 25


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


def evaluate(series: str, races: list[dict], rate: float) -> tuple[float, float, int]:
    """(weighted mean abs calibration error on top-N, win Brier, n scored)."""
    gp = PARAMS[series]["grid_pts"]
    cw = PARAMS[series]["con_w"]
    drv: dict[str, float] = {}
    con: dict[str, float] = {}
    cur_season = None
    agg: dict = defaultdict(lambda: [0.0, 0, 0])
    win_se = 0.0
    win_n = 0
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
        if len(field) >= 2 and i >= WARMUP and any(r.get("start_order") is not None for r in results):
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
                                      seed=i, attrition=rate)
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
        best: dict[str, int] = {}
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
    cal = num / den if den else float("nan")
    return cal, (win_se / win_n if win_n else float("nan")), scored


def main() -> None:
    for series in ("nascar", "f1", "irl", "nascar_xfinity", "nascar_truck"):
        races = load(series)
        if not races:
            print(f"\n=== {series}: no data ===")
            continue
        print(f"\n=== {series} ===")
        print(f"{'attrition':>10s} {'top-N cal err':>14s} {'win Brier':>11s} {'races':>6s}")
        base_brier = None
        rows = []
        for rate in RATES:
            cal, brier, n = evaluate(series, races, rate)
            if rate == 0.0:
                base_brier = brier
            rows.append((rate, cal, brier))
            print(f"{rate:10.2f} {cal:14.4f} {brier:11.5f} {n:6d}")
        ok = [r for r in rows if base_brier is None or r[2] <= base_brier * 1.02]
        best = min(ok or rows, key=lambda r: r[1])
        print(f"  -> best rate {best[0]:.2f} (cal {best[1]:.4f}, win Brier {best[2]:.5f}; "
              f"baseline cal {rows[0][1]:.4f}, baseline Brier {rows[0][2]:.5f})")
        print(f"     [only rates keeping win Brier within 2% of baseline were eligible]")


if __name__ == "__main__":
    main()
