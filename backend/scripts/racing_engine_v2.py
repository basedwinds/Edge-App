"""Racing engine v2 (2026-07-23): adds the two features the v1 driver-Elo model
was blind to -- STARTING GRID position and CONSTRUCTOR -- which is why v1
"lagged the car pecking order" and looked edgeless. Grid alone is enormously
predictive (measured: P(win|pole)=68% in F1). This blends all three into one
Bradley-Terry strength and grid-searches the blend against walk-forward Brier +
winner-hit% (winner-hit is the honest discriminator; Brier can be gamed by
under-confidence in a big field -- the IndyCar mirage lesson).

strength_i = driver_elo_i + CON_W*(constructor_elo_i - BASE) - GRID_PTS*(grid_i - 1)
P(win_i)   = 10**(strength_i/400) / sum_j 10**(strength_j/400)

Driver Elo + constructor Elo are both pairwise-updated from finishing order,
walk-forward (constructor = its best finisher per race, one result per
constructor). Grid is a pre-race known, applied only at prediction time.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
BASE = 1500.0
SEASON_REGRESSION = 1.0 / 3.0


def load(league):
    races = list(json.loads((DATA_DIR / f"racing_{league}.json").read_text(encoding="utf-8")).values())
    races.sort(key=lambda r: (r["date"] or "", r["id"]))
    return races


def _logistic(d):
    return 1.0 / (1.0 + 10 ** (-d / 400.0))


def _pairwise_delta(ids, order, ratings):
    delta = {d: 0.0 for d in ids}
    for i in ids:
        for j in ids:
            if i != j:
                s = 1.0 if order[i] < order[j] else 0.0
                delta[i] += s - _logistic(ratings[i] - ratings[j])
    return delta


def run(races, k_driver, k_con, con_w, grid_pts, warmup=15):
    """Walk-forward. Returns (model_probs, naive_probs, outs, winner_hit_rate)."""
    drv, con = {}, {}
    cur_season = None
    seen = 0
    model_obs, naive_obs, outs = [], [], []
    hits = scored = 0

    for race in races:
        if race["season"] != cur_season:
            cur_season = race["season"]
            for d in drv:
                drv[d] = BASE + (1 - SEASON_REGRESSION) * (drv[d] - BASE)
            for c in con:
                con[c] = BASE + (1 - SEASON_REGRESSION) * (con[c] - BASE)

        results = race["results"]
        field = [r["driver_id"] for r in results]
        n = len(field)
        d_rat = {d: drv.get(d, BASE) for d in field}

        # constructor of each driver + best (lowest order) per constructor this race
        con_of = {r["driver_id"]: r.get("constructor") for r in results}
        best_order = {}
        for r in results:
            c = r.get("constructor")
            if c is None:
                continue
            if c not in best_order or r["order"] < best_order[c]:
                best_order[c] = r["order"]
        c_rat = {c: con.get(c, BASE) for c in best_order}

        # ---- predict (post-warmup) ----
        if seen >= warmup and all(r.get("start_order") for r in results):
            strength = {}
            for r in results:
                did = r["driver_id"]
                c = con_of.get(did)
                cr = con.get(c, BASE) if c else BASE
                strength[did] = (
                    d_rat[did]
                    + con_w * (cr - BASE)
                    - grid_pts * (r["start_order"] - 1)
                )
            vs = {d: 10 ** (s / 400.0) for d, s in strength.items()}
            tot = sum(vs.values())
            probs = {d: v / tot for d, v in vs.items()}
            for r in results:
                model_obs.append(probs[r["driver_id"]])
                naive_obs.append(1.0 / n)
                outs.append(1.0 if r["winner"] else 0.0)
            top = max(probs, key=probs.get)
            won = next(r["driver_id"] for r in results if r["winner"])
            hits += (top == won); scored += 1

        # ---- update driver Elo ----
        order = {r["driver_id"]: r["order"] for r in results}
        d_delta = _pairwise_delta(field, order, d_rat)
        for d in field:
            drv[d] = d_rat[d] + (k_driver / (n - 1)) * d_delta[d]
        # ---- update constructor Elo (best finisher per constructor) ----
        cids = list(best_order)
        if len(cids) > 1:
            c_delta = _pairwise_delta(cids, best_order, c_rat)
            for c in cids:
                con[c] = c_rat[c] + (k_con / (len(cids) - 1)) * c_delta[c]
        seen += 1

    return model_obs, naive_obs, outs, (hits / scored if scored else 0.0)


def brier(preds, outs):
    return sum((p - o) ** 2 for p, o in zip(preds, outs)) / len(outs) if outs else float("nan")


def main():
    league = sys.argv[1] if len(sys.argv) > 1 else "f1"
    races = load(league)
    print(f"{league}: {len(races)} races ({races[0]['date'][:10]} -> {races[-1]['date'][:10]})\n")

    # v1 baseline: driver Elo only (con_w=0, grid_pts=0)
    mo, no, outs, hit = run(races, 24, 24, 0.0, 0.0)
    print(f"{'model':<34}{'Brier':>10}{'naive':>10}{'winner-hit%':>13}")
    print(f"{'v1 driver-Elo only':<34}{brier(mo,outs):>10.5f}{brier(no,outs):>10.5f}{hit:>12.1%}")

    # grid-only sanity (kill driver+con influence by K~0 not possible; use grid_pts only w/ flat elos)
    mo, no, outs, hit = run(races, 0.001, 0.001, 0.0, 120.0)
    print(f"{'grid-only (grid_pts=120)':<34}{brier(mo,outs):>10.5f}{'':>10}{hit:>12.1%}")

    print(f"\n{'grid_pts':>9}{'con_w':>7}  -> Brier / winner-hit  (driver+constructor+grid blend)")
    best = None
    for grid_pts in (60, 90, 120, 150, 200):
        for con_w in (0.0, 0.5, 1.0):
            mo, no, outs, hit = run(races, 24, 24, con_w, grid_pts)
            b = brier(mo, outs)
            tag = ""
            if best is None or hit > best[-1] or (hit == best[-1] and b < best[2]):
                best = (grid_pts, con_w, b, hit); tag = " <-- best"
            print(f"{grid_pts:>9}{con_w:>7}   Brier {b:.5f}  hit {hit:.1%}{tag}")
    print(f"\nBEST: grid_pts={best[0]} con_w={best[1]} -> Brier {best[2]:.5f}, winner-hit {best[3]:.1%}")


if __name__ == "__main__":
    main()
