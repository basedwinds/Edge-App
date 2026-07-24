"""Shared racing engine (core-5 expansion): a pairwise-Elo driver rating that
converts to field win probabilities via the Bradley-Terry / Plackett-Luce form
that is mathematically consistent with the pairwise updates. Used by F1,
IndyCar, and NASCAR (same code, different cache file).

Rating: each race is N drivers; every ordered pair (i finished ahead of j) is a
pairwise "win". Driver i's per-race delta = (K/(N-1)) * sum_j (S_ij - E_ij),
with E_ij = logistic((Ri-Rj)/400). This is the standard multi-competitor Elo.
Field win prob: v_i = 10**(Ri/400); P(win_i) = v_i / sum_j v_j (Bradley-Terry).

Validation is walk-forward: predicted P(win) for each driver in a race is scored
by Brier against did-win, and compared to the naive "uniform 1/N per driver"
baseline (the only sensible no-information prior for a field market).
"""
import json
import math
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


def field_win_probs(ratings):
    """Bradley-Terry win prob for each driver id in `ratings` (dict id->rating)."""
    vs = {d: 10 ** (r / 400.0) for d, r in ratings.items()}
    tot = sum(vs.values())
    return {d: v / tot for d, v in vs.items()}


def run(races, k, warmup_races=15):
    r = {}
    cur_season = None
    seen_races = 0
    # accumulate per (race, driver) predictions after warmup
    model_obs, naive_obs, outs = [], [], []
    for race in races:
        if race["season"] != cur_season:
            cur_season = race["season"]
            for d in r:
                r[d] = BASE + (1 - SEASON_REGRESSION) * (r[d] - BASE)
        field = [res["driver_id"] for res in race["results"]]
        n = len(field)
        ratings = {d: r.get(d, BASE) for d in field}
        if seen_races >= warmup_races:
            probs = field_win_probs(ratings)
            for res in race["results"]:
                model_obs.append(probs[res["driver_id"]])
                naive_obs.append(1.0 / n)
                outs.append(1.0 if res["winner"] else 0.0)
        # pairwise Elo update
        order = {res["driver_id"]: res["order"] for res in race["results"]}
        delta = {d: 0.0 for d in field}
        for i in field:
            for j in field:
                if i == j:
                    continue
                s = 1.0 if order[i] < order[j] else 0.0
                e = _logistic(ratings[i] - ratings[j])
                delta[i] += (s - e)
        for d in field:
            r[d] = ratings[d] + (k / (n - 1)) * delta[d]
        seen_races += 1
    return model_obs, naive_obs, outs


def brier(preds, outs):
    return sum((p - o) ** 2 for p, o in zip(preds, outs)) / len(outs)


def main():
    league = sys.argv[1] if len(sys.argv) > 1 else "f1"
    races = load(league)
    n_drivers = len({res["driver_id"] for race in races for res in race["results"]})
    print(f"{league}: {len(races)} races, {n_drivers} drivers ({races[0]['date'][:10]} -> {races[-1]['date'][:10]})")
    print(f"\n{'K':>5} {'model Brier':>12} {'naive Brier':>12} {'winner-hit%':>12}")
    best = None
    for k in (8, 12, 16, 20, 24, 32, 40, 56):
        mo, no, outs = run(races, k)
        mb, nb = brier(mo, outs), brier(no, outs)
        # winner-hit%: fraction of races where the model's top pick actually won
        hit = _winner_hit(races, k)
        print(f"{k:>5} {mb:>12.5f} {nb:>12.5f} {hit:>11.1%}")
        if best is None or mb < best[1]:
            best = (k, mb, nb, hit)
    print(f"\nbest K={best[0]}: model Brier {best[1]:.5f} vs naive {best[2]:.5f} "
          f"({(best[2]-best[1])/best[2]:+.1%} vs naive), top-pick wins {best[3]:.1%}")


def _winner_hit(races, k):
    r = {}; cur = None; hits = 0; scored = 0; seen = 0
    for race in races:
        if race["season"] != cur:
            cur = race["season"]
            for d in r:
                r[d] = BASE + (1 - SEASON_REGRESSION) * (r[d] - BASE)
        field = [res["driver_id"] for res in race["results"]]
        ratings = {d: r.get(d, BASE) for d in field}
        if seen >= 15:
            top = max(ratings, key=ratings.get)
            won = next(res["driver_id"] for res in race["results"] if res["winner"])
            hits += (top == won); scored += 1
        order = {res["driver_id"]: res["order"] for res in race["results"]}
        n = len(field)
        delta = {d: 0.0 for d in field}
        for i in field:
            for j in field:
                if i != j:
                    delta[i] += (1.0 if order[i] < order[j] else 0.0) - _logistic(ratings[i] - ratings[j])
        for d in field:
            r[d] = ratings[d] + (k / (n - 1)) * delta[d]
        seen += 1
    return hits / scored if scored else 0.0


if __name__ == "__main__":
    main()
