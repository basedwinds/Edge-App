"""Is racing top_n calibrated -- specifically for the LONGSHOTS -- once a grid
is known?

THE QUESTION. The pre-qualifying staking gate (racing_markets.PRE_QUALIFYING_NOTE)
was added because with no grid the field flattens and every backmarker inherits
a plausible-looking probability: Cody Ware priced at 31.0% against a 3.5% market,
an 8.9x ratio that slips under implausible_disagreement's 10x threshold. That
gate stops top_n staking until qualifying. But it was never established whether
the overpricing is CAUSED by the missing grid or merely amplified by it -- if the
model still overprices backmarkers with a real grid in hand, the gate only delays
the bad bet until Saturday afternoon rather than preventing it.

This settles that, and it matters on a timetable: the gate lifts the moment ESPN
publishes a grid, so any residual bias goes live on race weekend.

METHOD. Walk-forward, mirroring scripts/fit_racing_params_per_series.py exactly
(same pairwise Elo, K_DRIVER/K_CON=24, SEASON_REGRESSION=1/3 at each season
boundary, same WARMUP) so this measures the SHIPPED model, not a strawman. The
one deliberate difference: that script scores WIN probability in closed form,
while production's top_n comes from racing_sim.simulate()'s Monte Carlo. Top-N is
the thing under test, so this calls the real simulator.

Every scored race uses the race's own start_order as the grid -- i.e. the
post-qualifying state the gate now waits for. Ratings only ever see races BEFORE
the one being scored, so there is no leakage.

READING IT. Calibration, not winner-hit -- the model bets probability against
price and never picks a driver, so the question is whether "12%" happens 12% of
the time. The rows that matter are the LOW model-probability buckets. If those
show actual << model, the model systematically overprices no-hopers and the gate
is a delay, not a fix.
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
TRIALS = 3000
TOP_NS = (3, 5, 10, 20)

# Model-probability buckets. Deliberately fine at the bottom: the whole question
# is about longshots, and lumping everything under 20% into one bucket would hide
# exactly the band the Cody Ware case sits in.
BUCKETS = [(0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
           (0.20, 0.35), (0.35, 0.60), (0.60, 1.01)]


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


def run_series(series: str) -> None:
    races = load(series)
    if not races:
        print(f"{series}: no data")
        return
    gp = PARAMS[series]["grid_pts"]
    cw = PARAMS[series]["con_w"]

    drv: dict[str, float] = {}
    con: dict[str, float] = {}
    cur_season = None
    # (top_n, bucket) -> [sum_model_prob, hits, count]
    agg: dict = defaultdict(lambda: [0.0, 0, 0])
    scored = 0
    no_grid = 0

    for i, race in enumerate(races):
        if race.get("season") != cur_season:
            cur_season = race.get("season")
            for d in drv:
                drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)
            for c in con:
                con[c] = BASE + (1 - SEASON_REGRESSION) * (con[c] - BASE)

        results = race["results"]
        field = [r["driver_id"] for r in results]
        if len(field) >= 2 and i >= WARMUP:
            # Only score races where the grid is actually known -- a race with no
            # start_order is the pre-qualifying state the gate already blocks, so
            # including it would answer the question we are NOT asking.
            if all(r.get("start_order") is None for r in results):
                no_grid += 1
            else:
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
                sim = racing_sim.simulate(strength, trials=TRIALS, top_ns=TOP_NS, seed=i)
                if sim:
                    scored += 1
                    n_field = len(results)
                    for r in results:
                        d = r["driver_id"]
                        got = sim.get(d) or {}
                        for n in TOP_NS:
                            if n >= n_field:
                                continue  # degenerate: everyone finishes top-N
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

    print(f"\n=== {series}  (grid_pts={gp}, con_w={cw}) "
          f"scored {scored} races with a grid, skipped {no_grid} without ===")
    print(f"{'top-N':>6s} {'model band':>14s} {'n':>6s} {'avg model':>10s} {'actual':>8s} {'diff':>8s}")
    for n in TOP_NS:
        rows = [(b, agg[(n, b)]) for b in BUCKETS if agg.get((n, b)) and agg[(n, b)][2] >= 25]
        for (lo, hi), (psum, hits, cnt) in rows:
            avg = psum / cnt
            act = hits / cnt
            flag = "  <-- OVER" if avg - act > 0.05 else ("  <-- under" if act - avg > 0.05 else "")
            print(f"{n:6d} {f'{lo:.0%}-{hi:.0%}':>14s} {cnt:6d} {avg:10.3f} {act:8.3f} {avg-act:+8.3f}{flag}")


def main() -> None:
    for series in ("nascar", "f1", "irl", "nascar_xfinity", "nascar_truck"):
        run_series(series)


if __name__ == "__main__":
    main()
