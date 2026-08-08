"""Is the racing CHAMPIONSHIP model calibrated?

WHY. It prices 152 live markets (F1 drivers + constructors, IndyCar drivers) and
is the only futures family in this app never checked against outcomes. Every
season sim -- NFL, NBA, WNBA, CFB, and MLB as of yesterday -- has been calibration
tested and four of them needed a correction. This one has not been looked at.

MLB is the cautionary case: its season sim was overconfident by 10 points,
one-directional, across 726 markets, and nobody knew because nobody measured.
The failure mode to look for is the same -- favourites too high, longshots too
low, because a point estimate of strength is treated as certain.

METHOD, mirroring check_mlb_season_sim_calibration. Walk forward through each
season's races building the production pairwise Elo. At several CHECKPOINTS
per season (after 25/50/75% of the calendar) freeze the standings, run the
shipped championship sim over the races that remain, and record each driver's
predicted title probability against whether they actually went on to win it.

THE SAMPLE IS SMALL AND CORRELATED, and that is stated rather than hidden. F1
in this app's cache spans ~5 seasons, so there are only ~5 real championship
outcomes; the checkpoints multiply the row count but not the independent
information (three views of the same season share one answer). This can detect a
gross bias like MLB's 16pp. It cannot certify a small one, and no amount of
resampling changes that -- only more seasons would.

Points tables and the DNF rate come from the shipped module, so this measures
what actually prices the markets rather than a reimplementation.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import racing_championship_sim as CS
from app.models.baseline.racing_ratings import (
    BASE, K_CON, K_DRIVER, PARAMS, SEASON_REGRESSION, _pairwise,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
TRIALS = 4000
CHECKPOINTS = (0.25, 0.50, 0.75)
BUCKETS = [(0.0, 0.05), (0.05, 0.15), (0.15, 0.35), (0.35, 0.65), (0.65, 1.01)]
MIN_BUCKET = 6


def load(series: str) -> list[dict]:
    p = DATA_DIR / f"racing_{series}.json"
    if not p.exists():
        return []
    r = list(json.loads(p.read_text(encoding="utf-8")).values())
    r.sort(key=lambda x: (x.get("date") or "", x["id"]))
    return r


def bucket_of(p):
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def points_for(series: str):
    if series == "irl":
        return CS.INDYCAR_POINTS, CS.INDYCAR_TAIL_POINTS
    return CS.F1_POINTS, 0.0


def run(series: str) -> None:
    races = load(series)
    if not races:
        print(f"\n=== {series}: no data ===")
        return
    by_season = defaultdict(list)
    for r in races:
        by_season[r.get("season")].append(r)
    seasons = sorted(s for s in by_season if s)
    table, tail = points_for(series)
    cw = PARAMS[series]["con_w"]

    drv: dict[str, float] = {}
    con: dict[str, float] = {}
    agg = defaultdict(lambda: [0.0, 0, 0])
    checks = 0

    for si, season in enumerate(seasons):
        rows = by_season[season]
        # season regression, exactly as production
        for d in drv:
            drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)
        for c in con:
            con[c] = BASE + (1 - SEASON_REGRESSION) * (con[c] - BASE)

        # who ACTUALLY won this season's title (full-season points)
        season_pts: dict[str, float] = defaultdict(float)
        for r in rows:
            for res in r["results"]:
                o = res.get("order")
                if o is None:
                    continue
                season_pts[res["driver_id"]] += (table[o - 1] if o <= len(table) else tail)
        champion = max(season_pts, key=season_pts.get) if season_pts else None

        pts: dict[str, float] = defaultdict(float)
        for i, race in enumerate(rows):
            frac = (i + 1) / len(rows)
            # ---- checkpoint BEFORE this race's result is added -------------
            if si >= 1 and champion and any(abs(frac - c) < 0.5 / len(rows) for c in CHECKPOINTS):
                field = sorted({res["driver_id"] for r2 in rows for res in r2["results"]})
                strengths = {}
                for d in field:
                    s = drv.get(d, BASE)
                    strengths[d] = s
                probs = CS.simulate_driver_championship(
                    field, {d: pts.get(d, 0.0) for d in field}, strengths,
                    remaining_races=len(rows) - i, trials=TRIALS,
                    points_table=table, tail_points=tail,
                )
                if probs:
                    checks += 1
                    for d, p in probs.items():
                        b = bucket_of(p)
                        if b is None:
                            continue
                        a = agg[b]
                        a[0] += p
                        a[1] += int(d == champion)
                        a[2] += 1

            # ---- apply the race, then update ratings ----------------------
            for res in race["results"]:
                o = res.get("order")
                if o is not None:
                    pts[res["driver_id"]] += (table[o - 1] if o <= len(table) else tail)
            f = [res["driver_id"] for res in race["results"]]
            order = {res["driver_id"]: res["order"] for res in race["results"]}
            drv.update(_pairwise(f, order, {d: drv.get(d, BASE) for d in f}, K_DRIVER))
            best = {}
            for res in race["results"]:
                c = res.get("constructor")
                if c and (c not in best or res["order"] < best[c]):
                    best[c] = res["order"]
            if len(best) > 1:
                con.update(_pairwise(list(best), best, {c: con.get(c, BASE) for c in best}, K_CON))

    print(f"\n=== {series} === {len(seasons)} seasons, {checks} checkpoints "
          f"({len(seasons)-1} independent title outcomes scored)")
    print(f"{'model says':>14s} {'drivers':>8s} {'avg model':>10s} {'actual':>8s} {'diff':>8s}")
    num = den = 0.0
    for b in BUCKETS:
        psum, hits, n = agg[b]
        if n < MIN_BUCKET:
            continue
        avg, act = psum / n, hits / n
        flag = "  <-- OVER" if avg - act > 0.10 else ("  <-- under" if act - avg > 0.10 else "")
        print(f"{f'{b[0]:.0%}-{b[1]:.0%}':>14s} {n:8d} {avg:10.3f} {act:8.3f} {avg-act:+8.3f}{flag}")
        num += n * abs(avg - act); den += n
    print(f"weighted calibration error: {num/den:.4f}" if den else "no scorable buckets")


def main() -> None:
    for s in (sys.argv[1:] or ["f1", "irl"]):
        run(s)


if __name__ == "__main__":
    main()
