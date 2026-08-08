"""Track-aware racing parameters: does deriving grid weight and attrition from
each TRACK's own history beat one value per series?

WHY. Measured per track with no hardcoded classification, NASCAR spans a 12x
range in how well the starting grid predicts the finish and a 6x range in how
often front-runners end up at the back:

    Daytona       grid->finish corr 0.049   48% of top-5 starters wrecked
    Talladega                     0.067   42%
    Watkins Glen                  0.550    8%
    Martinsville                  0.609   11%

and the ordering sorts itself into the real categories (superspeedways at the
bottom, short tracks and road courses at the top). A single per-series pair is
an average across two different kinds of racing -- roughly right for a generic
race, wrong for any specific one.

NO HARDCODED TRACK LIST, deliberately. Categories would need maintaining, would
mis-handle a repaved or reconfigured circuit, and would silently mis-classify
any new venue. Each track's parameters are estimated from its OWN past races.

SHRINKAGE, because tracks are thin. 42 distinct tracks over 157 NASCAR races is
~3.7 races each, so a raw per-track estimate is mostly noise. Each track's
estimate is blended toward the series-wide value with weight n/(n+SHRINK_K), so
a track with one race is priced almost entirely on the series prior and only
earns its own parameters as evidence accumulates. A brand-new venue therefore
falls back to the series value automatically, with no special case.

STRICTLY WALK-FORWARD. A race's parameters come only from EARLIER races at that
track. Estimating a track's attrition from the race being priced would leak the
outcome into the price, which is the whole failure mode this work exists to
avoid.

COMPARED AGAINST BOTH BASELINES: the shipped per-series values, and the
per-series values fitted in fit_racing_joint_holdout.py. Beating the shipped
model is not interesting on its own -- the question is whether track-awareness
buys anything ON TOP of the per-series fit that already validated.
"""
from __future__ import annotations

import json
import re
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
SHRINK_K = 6.0          # races before a track is weighted half on its own history
BACK_FRAC = 0.60        # "finished in the back" = beyond this fraction of the field
FRONT_START = 5         # a "front-runner" for the attrition estimate

# Per-series values fitted in fit_racing_joint_holdout.py, hold-out validated.
FITTED = {
    "nascar":         (10.0, 0.20),
    "f1":             (40.0, 0.10),
    "irl":            (30.0, 0.30),
    "nascar_xfinity": (20.0, 0.30),
    "nascar_truck":   (20.0, 0.20),
}

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


def track_of(name: str) -> str:
    """Track identity from the race name -- ESPN exposes no venue field on the
    racing scoreboard, so the name is the only handle. "NASCAR Cup Series at
    Sonoma" -> "Sonoma"; the Daytona 500 and the Daytona road course are
    deliberately NOT merged (different layouts, different racing)."""
    n = (name or "").strip()
    n = re.sub(r"^(NASCAR|Formula 1|IndyCar)[^a-z]*? at ", "", n, flags=re.IGNORECASE)
    n = re.sub(r"\s+\d+$", "", n)
    if "DAYTONA 500" in n.upper():
        return "Daytona"
    return n.strip() or "?"


def bucket_of(p: float):
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def _corr(pairs) -> float | None:
    if len(pairs) < 20:
        return None
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    return num / den if den else None


def evaluate(series, races, mode, score_from=0, score_to=None):
    """mode: "shipped" | "fitted" | "track". Returns (cal_err, n_scored)."""
    if score_to is None:
        score_to = len(races)
    ship_gp = PARAMS[series]["grid_pts"]
    cw = PARAMS[series]["con_w"]
    fit_gp, fit_att = FITTED[series]

    drv, con = {}, {}
    cur_season = None
    agg = defaultdict(lambda: [0.0, 0, 0])
    scored = 0

    # per-track running history, EARLIER races only
    t_pairs = defaultdict(list)      # track -> [(start, finish)]
    t_front = defaultdict(lambda: [0, 0])  # track -> [wrecked, total front-runners]
    t_races = defaultdict(int)
    all_pairs: list = []
    all_front = [0, 0]

    for i, race in enumerate(races):
        if race.get("season") != cur_season:
            cur_season = race.get("season")
            for d in drv:
                drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)
            for c in con:
                con[c] = BASE + (1 - SEASON_REGRESSION) * (con[c] - BASE)

        results = race["results"]
        field = [r["driver_id"] for r in results]
        tk = track_of(race.get("name"))

        if len(field) >= 2 and i >= WARMUP and score_from <= i < score_to \
                and any(r.get("start_order") is not None for r in results):
            if mode == "shipped":
                gp, att = ship_gp, 0.0
            elif mode == "fitted":
                gp, att = fit_gp, fit_att
            else:
                n = t_races[tk]
                w = n / (n + SHRINK_K)
                gp, att = fit_gp, fit_att
                c_all = _corr(all_pairs)
                c_trk = _corr(t_pairs[tk])
                if w > 0 and c_trk is not None and c_all and c_all > 0.05:
                    # Scale the grid term by how much THIS track's grid actually
                    # predicts its finishing order, relative to the series norm.
                    ratio = max(0.15, min(2.5, c_trk / c_all))
                    gp = fit_gp * ((1 - w) + w * ratio)
                if w > 0 and t_front[tk][1] >= 8 and all_front[1] >= 40:
                    a_trk = t_front[tk][0] / t_front[tk][1]
                    a_all = all_front[0] / all_front[1]
                    if a_all > 0:
                        ratio = max(0.25, min(2.5, a_trk / a_all))
                        att = min(0.6, fit_att * ((1 - w) + w * ratio))

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
            sim = racing_sim.simulate(strength, trials=TRIALS, top_ns=TOP_NS, seed=i, attrition=att)
            if sim:
                scored += 1
                nf = len(results)
                for r in results:
                    got = sim.get(r["driver_id"]) or {}
                    for n_ in TOP_NS:
                        if n_ >= nf:
                            continue
                        p = got.get(f"top{n_}")
                        if p is None:
                            continue
                        b = bucket_of(p)
                        if b is None:
                            continue
                        a = agg[(n_, b)]
                        a[0] += p
                        a[1] += int(r["order"] <= n_)
                        a[2] += 1

        # ---- update history AFTER scoring (walk-forward) --------------------
        nf = len(results)
        for r in results:
            s_, o_ = r.get("start_order"), r.get("order")
            if s_ is None or o_ is None:
                continue
            t_pairs[tk].append((s_, o_))
            all_pairs.append((s_, o_))
            if s_ <= FRONT_START:
                bad = int(o_ > nf * BACK_FRAC)
                t_front[tk][0] += bad; t_front[tk][1] += 1
                all_front[0] += bad; all_front[1] += 1
        t_races[tk] += 1

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
    return (num / den if den else float("nan")), scored


def main() -> None:
    for series in (sys.argv[1:] or ["nascar"]):
        races = load(series)
        if not races or series not in FITTED:
            print(f"{series}: skipped")
            continue
        split = int(len(races) * TRAIN_FRAC)
        print(f"\n=== {series} === hold-out only, races >= {split}")
        for mode in ("shipped", "fitted", "track"):
            cal, n = evaluate(series, races, mode, split)
            print(f"   {mode:8s} cal={cal:.4f}  ({n} races)")


if __name__ == "__main__":
    main()
