"""Are Cup's fitted constants right for Xfinity and Truck? And how much of the
racing model is the GRID?

WHY THIS EXISTS. The lower NASCAR series went live 2026-08-07 pricing ~148
markets a weekend, and racing_ratings.PARAMS hands them Cup's fitted values
(grid_pts=90, con_w=0.5) because nobody had fitted their own. Same car formula
and tracks makes Cup a far better prior than F1's or IndyCar's, but "better
prior" is not "measured". Truck is the most suspect: shorter races and heavier
attrition are exactly what grid_pts encodes, so its true value is plausibly
lower than Cup's -- a bad grid slot matters less when the race is short and
chaotic.

IT ALSO ANSWERS #94. Before qualifying there IS no grid, so strength() drops the
grid term and prices the race on driver+constructor alone. The grid_pts=0 column
below IS pre-qualifying pricing. Comparing it to the fitted optimum measures
exactly what waiting for qualifying buys, on ~400 races rather than on the 44
settled bets that made #93 inconclusive.

MIRRORS PRODUCTION, not a strawman: walk-forward pairwise Elo at K_DRIVER=24 /
K_CON=24 with SEASON_REGRESSION=1/3 applied at each season boundary, constructor
rated on its best finisher per race, and strength = driver + con_w*(constructor
- BASE) - grid_pts*(grid - 1), all lifted from racing_ratings._compute_series and
strength(). Predictions are scored only after a warmup and only on races the
ratings have not already seen.

SCORED TWO WAYS because they answer different questions. Brier over every
(race, driver) observation is the calibration read. Winner-hit -- how often the
model's top pick actually won -- is what a race-winner bet cares about, and a
model can improve one while losing the other.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/fit_racing_params_per_series.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.baseline.racing_ratings import (  # noqa: E402
    BASE, K_CON, K_DRIVER, PARAMS, SEASON_REGRESSION, _logistic, _pairwise,
)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SERIES = ("nascar", "nascar_xfinity", "nascar_truck", "f1", "irl")
WARMUP = 15
GRID_PTS = (0.0, 30.0, 60.0, 90.0, 120.0, 150.0)
CON_W = (0.0, 0.25, 0.5, 0.75)


def load(series: str) -> list[dict]:
    path = DATA_DIR / f"racing_{series}.json"
    if not path.exists():
        return []
    races = list(json.loads(path.read_text(encoding="utf-8")).values())
    races.sort(key=lambda r: (r.get("date") or "", r["id"]))
    return races


def walk_forward(races: list[dict], grid_pts: float, con_w: float) -> tuple[list, list, int, int]:
    """(model probs, outcomes, winner-hits, scored races) for one parameter pair."""
    drv: dict[str, float] = {}
    con: dict[str, float] = {}
    cur_season = None
    preds: list[float] = []
    outs: list[float] = []
    hits = scored = 0

    for i, race in enumerate(races):
        if race.get("season") != cur_season:
            cur_season = race.get("season")
            for d in drv:
                drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)
            for c in con:
                con[c] = BASE + (1 - SEASON_REGRESSION) * (con[c] - BASE)
        results = race["results"]
        field = [r["driver_id"] for r in results]
        if len(field) < 2:
            continue

        if i >= WARMUP:
            # strength() as shipped. A missing start_order means no grid term,
            # which is exactly how a pre-qualifying race prices.
            strength = {}
            for r in results:
                d = r["driver_id"]
                s = drv.get(d, BASE)
                c = r.get("constructor")
                if c is not None:
                    s += con_w * (con.get(c, BASE) - BASE)
                g = r.get("start_order")
                if g is not None and grid_pts:
                    s -= grid_pts * (g - 1)
                strength[d] = s
            vs = {d: 10 ** (s / 400.0) for d, s in strength.items()}
            tot = sum(vs.values())
            if tot > 0:
                probs = {d: v / tot for d, v in vs.items()}
                for r in results:
                    preds.append(probs[r["driver_id"]])
                    outs.append(1.0 if r.get("winner") else 0.0)
                top = max(probs, key=probs.get)
                actual = next((r["driver_id"] for r in results if r.get("winner")), None)
                if actual is not None:
                    scored += 1
                    hits += int(top == actual)

        # ---- updates, mirroring _compute_series exactly ----------------------
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

    return preds, outs, hits, scored


def brier(preds: list[float], outs: list[float]) -> float:
    return sum((p - o) ** 2 for p, o in zip(preds, outs)) / len(preds)


def run() -> None:
    for series in SERIES:
        races = load(series)
        if len(races) <= WARMUP + 5:
            print(f"\n=== {series}: {len(races)} races -- too few to fit, skipped")
            continue
        shipped = PARAMS.get(series, {})
        grid_cov = sum(
            1 for r in races for x in r["results"] if x.get("start_order") is not None
        ) / max(1, sum(len(r["results"]) for r in races))
        print(f"\n=== {series}  ({len(races)} races, grid present on {grid_cov:.0%} of results)"
              f"   shipped: grid_pts={shipped.get('grid_pts')} con_w={shipped.get('con_w')}")
        print(f"{'grid_pts':>9}{'con_w':>7}{'Brier':>10}{'winner-hit':>12}")

        best_row = None
        for gp in GRID_PTS:
            for cw in CON_W:
                preds, outs, hits, scored = walk_forward(races, gp, cw)
                if not preds or not scored:
                    continue
                b = brier(preds, outs)
                hit = hits / scored
                mark = ""
                if gp == shipped.get("grid_pts") and cw == shipped.get("con_w"):
                    mark = "  <- SHIPPED"
                if gp == 0.0 and cw == shipped.get("con_w", 0.5):
                    mark += "  <- PRE-QUALIFYING"
                if best_row is None or b < best_row[0]:
                    best_row = (b, gp, cw, hit)
                print(f"{gp:>9.0f}{cw:>7.2f}{b:>10.5f}{hit:>11.1%}{mark}")

        if best_row:
            b, gp, cw, hit = best_row
            same = (gp == shipped.get("grid_pts") and cw == shipped.get("con_w"))
            print(f"  BEST by Brier: grid_pts={gp:.0f} con_w={cw:.2f} "
                  f"(Brier {b:.5f}, winner-hit {hit:.1%})"
                  f"{'  == shipped' if same else '  != SHIPPED'}")


if __name__ == "__main__":
    run()
