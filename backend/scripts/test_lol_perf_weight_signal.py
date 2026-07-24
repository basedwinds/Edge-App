"""Feasibility test (task #33): does weighting player Elo updates by
individual in-game performance beat shared-credit?

Design that avoids double-counting the result: a team's TOTAL delta per series
is UNCHANGED (= shared credit at team level, K_PLAYER*(actual-p) per player x
5). Only the ALLOCATION among the 5 players changes -- by within-team relative
performance. A `strength` knob interpolates: strength=0 is exactly
shared-credit (the baseline); strength=1 is full reallocation:
    weight_i = 1 + strength * (5 * score_i / sum(team scores) - 1)
so team weights always sum to 5 (mean 1), keeping the team-total delta fixed.

Runs entirely within gol.gg data: player ratings warm up (shared credit) over
all series before the stat window, then within the window the weighted rule is
applied and predictions on the later window series are scored. If even this
in-window application shows no gain, performance-weighting is rejected without
the full ~8k-game re-scrape.

Performance score is parameterised so a couple of role-fairness choices can be
compared (KDA is assist-inclusive / support-fairer; the composite favours
carries). All are within-team-relative, so a stomp doesn't just inflate
everyone.
"""
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_lol import BASE_RATING, RATING_CLAMP, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
LINEUP_CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"
STATS_PROBE_PATH = DATA_DIR / "lol_player_stats_probe.json"
K_PLAYER = 32.0
EVAL_WARMUP = 400  # window series before we start scoring, so ratings have diverged


def norm(x):
    return re.sub(r"[^a-z0-9]", "", x.lower())


def kda_score(p):
    return (p["k"] + p["a"]) / max(p["d"], 1)


def composite_score(p, game_means):
    # z-ish vs the game's 10-player mean, summed over KDA/DPM/CSM (carry-favoring)
    z = 0.0
    for key, val in (("kda", kda_score(p)), ("dpm", p["dpm"]), ("csm", p["csm"])):
        mu, sd = game_means[key]
        z += (val - mu) / sd if sd > 0 else 0.0
    return z


def build_series():
    """gol.gg series (date+pair) with, where available, aggregated per-player
    stats for each side."""
    lineups = {k: v for k, v in json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")).items() if v}
    stats = {k: v for k, v in json.loads(STATS_PROBE_PATH.read_text(encoding="utf-8")).items() if v} if STATS_PROBE_PATH.exists() else {}

    series = defaultdict(lambda: {"a": 0, "b": 0, "names": None, "stat_games": []})
    for gid, g in lineups.items():
        t0, t1 = g["teams"]
        pair = tuple(sorted((norm(t0), norm(t1))))
        key = (g["date"],) + pair
        s = series[key]
        a_is_t0 = norm(t0) == pair[0]
        if s["names"] is None:
            s["names"] = (t0, t1) if a_is_t0 else (t1, t0)
        a_won = g["blue_won"] if a_is_t0 else (not g["blue_won"])
        s["a"] += 1 if a_won else 0
        s["b"] += 0 if a_won else 1
        st = stats.get(gid)
        if st:
            # st[0] is blue-side stats, st[1] red-side; orient to team_a/team_b
            s["stat_games"].append((st[0], st[1]) if a_is_t0 else (st[1], st[0]))

    rows = []
    for (date, na, nb), s in series.items():
        if s["a"] == s["b"]:
            continue
        rows.append({
            "date": date, "team_a": s["names"][0], "team_b": s["names"][1],
            "best_of": 1 if (s["a"] + s["b"]) == 1 else 2 * max(s["a"], s["b"]) - 1,
            "winner_a": s["a"] > s["b"],
            "lineup_a": None, "lineup_b": None,  # filled below from stat games if present
            "stats_a": _agg_side(s["stat_games"], 0), "stats_b": _agg_side(s["stat_games"], 1),
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def _agg_side(stat_games, side):
    """Aggregate a side's per-game stats across a series into per-player totals."""
    if not stat_games:
        return None
    agg = {}
    for game in stat_games:
        for p in game[side]:
            a = agg.setdefault(p["name"], {"k": 0, "d": 0, "a": 0, "csm": [], "dpm": [], "wpm": [], "n": 0})
            a["k"] += p["k"]; a["d"] += p["d"]; a["a"] += p["a"]
            a["csm"].append(p["csm"]); a["dpm"].append(p["dpm"]); a["wpm"].append(p["wpm"]); a["n"] += 1
    out = []
    for name, a in agg.items():
        out.append({"name": name, "k": a["k"], "d": a["d"], "a": a["a"],
                    "csm": statistics.mean(a["csm"]), "dpm": statistics.mean(a["dpm"]), "wpm": statistics.mean(a["wpm"])})
    return out if len(out) == 5 else None


def series_prob(map_p, bo):
    return sum(p for (a, b), p in series_score_distribution(map_p, bo).items() if a > b)


def weights(side_stats, strength, metric):
    if strength == 0 or not side_stats:
        return None
    if metric == "kda":
        scores = [max(kda_score(p), 0.01) for p in side_stats]
    else:
        allp = side_stats
        gm = {}
        for key, f in (("kda", kda_score), ("dpm", lambda p: p["dpm"]), ("csm", lambda p: p["csm"])):
            vals = [f(p) for p in allp]
            gm[key] = (statistics.mean(vals), statistics.pstdev(vals))
        raw = [composite_score(p, gm) for p in allp]
        lo = min(raw)
        scores = [r - lo + 0.1 for r in raw]  # shift positive
    tot = sum(scores)
    return [1 + strength * (5 * sc / tot - 1) for sc in scores]


def run(rows, strength, metric):
    pr = {}
    preds, outs = [], []
    window_seen = 0
    for r in rows:
        la = [p["name"] for p in r["stats_a"]] if r["stats_a"] else None
        lb = [p["name"] for p in r["stats_b"]] if r["stats_b"] else None
        has_stats = la is not None and lb is not None
        if has_stats:
            window_seen += 1
            a_str = sum(pr.get(p, BASE_RATING) for p in la) / 5
            b_str = sum(pr.get(p, BASE_RATING) for p in lb) / 5
            if window_seen > EVAL_WARMUP:
                preds.append(series_prob(map_win_prob(a_str, b_str), r["best_of"]))
                outs.append(1.0 if r["winner_a"] else 0.0)
        # update: needs a lineup either way; use stat lineup when present
        if not has_stats:
            continue
        act = 1.0 if r["winner_a"] else 0.0
        a_str = sum(pr.get(p, BASE_RATING) for p in la) / 5
        b_str = sum(pr.get(p, BASE_RATING) for p in lb) / 5
        delta = K_PLAYER * (act - map_win_prob(a_str, b_str))
        wa = weights(r["stats_a"], strength, metric) or [1] * 5
        wb = weights(r["stats_b"], strength, metric) or [1] * 5
        for p, w in zip(la, wa):
            pr[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, pr.get(p, BASE_RATING) + delta * w))
        for p, w in zip(lb, wb):
            pr[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, pr.get(p, BASE_RATING) - delta * w))
    return preds, outs


def main():
    rows = build_series()
    with_stats = sum(1 for r in rows if r["stats_a"] and r["stats_b"])
    print(f"{len(rows)} series; {with_stats} with full per-player stats")
    if with_stats < EVAL_WARMUP + 150:
        print(f"(scrape still in progress -- need > {EVAL_WARMUP + 150} stat-series for a read; have {with_stats})")
        return
    for metric in ("kda", "composite"):
        base_p, base_o = run(rows, 0.0, metric)
        base = brier_score(base_p, base_o)
        print(f"\nmetric={metric}: shared-credit baseline Brier = {base:.5f} ({len(base_o)} scored)")
        for s in (0.25, 0.5, 0.75, 1.0):
            p, o = run(rows, s, metric)
            b = brier_score(p, o)
            print(f"  strength={s:<4} Brier={b:.5f}  vs baseline {b - base:+.5f}")


if __name__ == "__main__":
    main()
