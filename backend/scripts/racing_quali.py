"""Qualifying / pole model, parallel to the race model but trained on the
GRID (start_order) instead of the finish. The insight: a race's starting grid
IS its qualifying result, and we already cache start_order per driver -- so a
pairwise-Elo on "who out-qualified whom" predicts pole (KXF1POLE) and qualifying
top-N, WITHOUT using grid as an input (grid is the OUTPUT here). Constructor
matters even more in qualifying (single-lap car pace), so it's blended in too.

Validated walk-forward: does the top-rated qualifier actually take pole?
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
    return [r for r in races if all(x.get("start_order") for x in r["results"])]


def _logistic(d):
    return 1.0 / (1.0 + 10 ** (-d / 400.0))


def _pairwise(ids, order, ratings, k):
    n = len(ids)
    if n < 2:
        return ratings
    delta = {d: 0.0 for d in ids}
    for i in ids:
        for j in ids:
            if i != j:
                delta[i] += (1.0 if order[i] < order[j] else 0.0) - _logistic(ratings[i] - ratings[j])
    return {d: ratings[d] + (k / (n - 1)) * delta[d] for d in ids}


def run(races, k_q, k_con, con_w, warmup=15):
    qd, qc = {}, {}
    cur = None
    seen = 0
    hits = scored = 0
    brier_num = 0.0
    brier_n = 0
    for race in races:
        if race["season"] != cur:
            cur = race["season"]
            for d in qd: qd[d] = BASE + (1 - SEASON_REGRESSION) * (qd[d] - BASE)
            for c in qc: qc[c] = BASE + (1 - SEASON_REGRESSION) * (qc[c] - BASE)
        res = race["results"]
        field = [r["driver_id"] for r in res]
        qorder = {r["driver_id"]: r["start_order"] for r in res}   # qualifying result
        con_of = {r["driver_id"]: r.get("constructor") for r in res}
        d_rat = {d: qd.get(d, BASE) for d in field}

        if seen >= warmup:
            strength = {}
            for r in res:
                did = r["driver_id"]; c = con_of.get(did)
                cr = qc.get(c, BASE) if c else BASE
                strength[did] = d_rat[did] + con_w * (cr - BASE)
            vs = {d: 10 ** (s / 400.0) for d, s in strength.items()}
            tot = sum(vs.values())
            probs = {d: v / tot for d, v in vs.items()}
            top = max(probs, key=probs.get)
            pole = next(r["driver_id"] for r in res if r["start_order"] == 1)
            hits += (top == pole); scored += 1
            for r in res:
                o = 1.0 if r["start_order"] == 1 else 0.0
                brier_num += (probs[r["driver_id"]] - o) ** 2; brier_n += 1

        qd.update(_pairwise(field, qorder, d_rat, k_q))
        best = {}
        for r in res:
            c = r.get("constructor")
            if c and (c not in best or r["start_order"] < best[c]):
                best[c] = r["start_order"]
        if len(best) > 1:
            c_rat = {c: qc.get(c, BASE) for c in best}
            qc.update(_pairwise(list(best), best, c_rat, k_con))
        seen += 1
    return (hits / scored if scored else 0.0), (brier_num / brier_n if brier_n else float("nan")), scored


def main():
    for lg in ("f1", "irl", "nascar"):
        races = load(lg)
        best = None
        for con_w in (0.0, 0.5, 1.0, 1.5):
            hit, br, n = run(races, 24, 24, con_w)
            if best is None or hit > best[0]:
                best = (hit, br, con_w, n)
        print(f"{lg}: {len(races)} races | best pole-hit {best[0]:.1%} (Brier {best[1]:.4f}, con_w={best[2]}) over {best[3]} scored")


if __name__ == "__main__":
    main()
