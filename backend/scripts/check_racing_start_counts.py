"""Is the racing model OVERCONFIDENT about drivers with few starts?

THE HUNCH THIS TESTS. On the 2026-08-14 Truck race at Richmond, several staked
bets sat on drivers whose rating was within a point of BASE -- so the "rating"
carried no driver information and the GRID term did all the work. That looks
like fabricated edge, and the obvious fix is a minimum-starts gate.

WHY IT IS MEASURED RATHER THAN ASSUMED. The identical hunch was raised against
CS2's MIN_GAMES and REJECTED by measurement: thin ratings were the BEST
calibrated bucket there (3 games claimed .799 and delivered .784, while 50+
claimed .843 and delivered .755). The mechanism was that a near-default rating
rarely lets the model get confident, so it only speaks up when the opponent is
genuinely extreme.

RACING MAY GENUINELY DIFFER, which is the whole reason to run it: here the GRID
term can produce a confident prediction on its own no matter how thin the driver
rating is. CS2 has no equivalent -- there, thin rating means quiet model. So the
CS2 result cannot simply be inherited.

METHOD. Walk each series' race history in order. Before each race, every driver
carries the start count and rating earned from PRIOR races only -- no leakage.
Price the field through the PRODUCTION path (racing_ratings.topn_strength with
the real grid, then racing_sim.simulate with that series' fitted attrition), then
bucket every driver-race by the starts that driver had at that moment and compare
the mean predicted probability against what actually happened.

Top-10 is the scored question because that is where the staking actually happens
(5 of the 7 bets on the race that prompted this were top_n).

Reads the same data/racing_*.json caches production rates off, so a bucket here
is the same bucket the live model would be pricing.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from app.models import racing_sim  # noqa: E402
from app.models.baseline import racing_ratings as rr  # noqa: E402

BUCKETS = [(1, 1), (2, 3), (4, 6), (7, 12), (13, 25), (26, 50), (51, 10_000)]
TOP_N = 10
TRIALS = 4000


def _label(lo, hi):
    return f"{lo}" if lo == hi else (f"{lo}+" if hi > 9999 else f"{lo}-{hi}")


def run(series: str):
    path = rr._DATA_DIR / f"racing_{series}.json"
    if not path.exists():
        print(f"{series}: no cache")
        return None
    races = sorted(json.loads(path.read_text(encoding="utf-8")).values(),
                   key=lambda r: (r["date"] or "", r["id"]))
    drv, con, starts = {}, {}, {}
    cur_season = None
    rows = []
    for race in races:
        if race["season"] != cur_season:
            cur_season = race["season"]
            for d in drv:
                drv[d] = rr.BASE + (1 - rr.SEASON_REGRESSION) * (drv[d] - rr.BASE)
            for c in con:
                con[c] = rr.BASE + (1 - rr.SEASON_REGRESSION) * (con[c] - rr.BASE)
        results = race["results"]
        field = [r["driver_id"] for r in results]
        if len(field) < 8 or not all(r.get("start_order") for r in results):
            # No grid -> not the situation being tested.
            pass
        else:
            # PRE-RACE state only.
            ratings = {}
            for r in results:
                d = r["driver_id"]
                saved = drv.get(d)
                drv_backup = drv.get(d, rr.BASE)
                s = drv_backup + rr.PARAMS[series]["con_w"] * (
                    con.get(r.get("constructor"), rr.BASE) - rr.BASE)
                s -= rr.TOPN_PARAMS[series]["grid_pts"] * (r["start_order"] - 1)
                ratings[d] = s
                _ = saved
            sim = racing_sim.simulate(ratings, trials=TRIALS, top_ns=(TOP_N,),
                                      seed=7, attrition=rr.TOPN_PARAMS[series]["attrition"])
            for r in results:
                d = r["driver_id"]
                p = (sim.get(d) or {}).get(f"top{TOP_N}")
                if p is None:
                    continue
                rows.append((starts.get(d, 0), p, 1.0 if r["order"] <= TOP_N else 0.0))
        # ---- update state AFTER scoring (walk-forward) ----
        for d in field:
            starts[d] = starts.get(d, 0) + 1
        d_rat = {d: drv.get(d, rr.BASE) for d in field}
        order = {r["driver_id"]: r["order"] for r in results}
        drv.update(rr._pairwise(field, order, d_rat, rr.K_DRIVER))
        best = {}
        for r in results:
            c = r.get("constructor")
            if c and (c not in best or r["order"] < best[c]):
                best[c] = r["order"]
        if len(best) > 1:
            c_rat = {c: con.get(c, rr.BASE) for c in best}
            con.update(rr._pairwise(list(best), best, c_rat, rr.K_CON))
    return rows


def report(series, rows):
    print(f"\n=== {series}  ({len(rows)} driver-races with a grid) ===")
    print(f"{'starts':>8}{'n':>7}{'claimed':>10}{'actual':>9}{'gap':>9}{'overstate':>11}")
    agg = defaultdict(list)
    for st, p, y in rows:
        for lo, hi in BUCKETS:
            if lo <= st <= hi:
                agg[(lo, hi)].append((p, y))
                break
    for lo, hi in BUCKETS:
        v = agg.get((lo, hi)) or []
        if len(v) < 30:
            continue
        claimed = sum(p for p, _ in v) / len(v)
        actual = sum(y for _, y in v) / len(v)
        ratio = claimed / actual if actual else float("inf")
        print(f"{_label(lo, hi):>8}{len(v):>7}{claimed:>10.3f}{actual:>9.3f}"
              f"{claimed - actual:>+9.3f}{ratio:>10.2f}x")


if __name__ == "__main__":
    for s in ("nascar", "nascar_xfinity", "nascar_truck", "irl", "f1"):
        r = run(s)
        if r:
            report(s, r)
    print("\nA gate is justified only if the LOW-start buckets are materially")
    print("more overstated than the high-start ones. If they are not, the CS2")
    print("result carries over and a minimum-starts gate would delete real bets.")
