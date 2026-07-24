"""Decision gate for LoL tier expansion (task #32): does adding gol.gg
lower-tier game results to the team-Elo training POLLUTE the strong
Primary-tier model?

The existing LoL model trains on Leaguepedia's Primary tier only (5,604
matches, the best base model of the 3 esports titles at 0.20757 Brier).
gol.gg gives ~2,444 additional series across lower tiers (task #32 analysis),
which would make 308 new teams -- 51 of them Kalshi-listed -- priceable.

The RISK, learned from Valorant: adding a large, noisier pool can drag the
ratings of shared opponents and degrade prediction on the matches we
actually bet. So this measures walk-forward Brier on the SAME Primary-tier
matches, two ways -- trained on Primary only, vs trained on Primary + the
gol.gg lower-tier series interleaved chronologically. If Primary-tier Brier
is not worse, expansion doesn't pollute and is safe to ship for the coverage
it buys.

gol.gg series are reconstructed from per-game results by aggregating games
on the same date between the same pair. Known approximation: two distinct
series between the same teams on one day (e.g. a group Bo1 then a playoff
Bo3) merge into one -- rare, and low-impact as Elo training noise. Series
already in the Primary cache (same date+pair) are DROPPED here, since the
Primary cache has authoritative maps_won for them.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_lol import BASE_RATING, K, RATING_CLAMP, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "lol_historical_match_cache.json"
LINEUP_CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"
WARMUP = 800
MIN_GAMES = 5


def norm(x):
    return re.sub(r"[^a-z0-9]", "", x.lower())


def load_primary():
    rows = [r for r in json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8")) if r.get("best_of") and r.get("winner")]
    for r in rows:
        r["origin"] = "primary"
    return rows


def build_golgg_series(primary_pairs):
    """gol.gg games -> series rows in the Primary-cache shape, EXCLUDING any
    (date, pair) already in the Primary cache."""
    games = [v for v in json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")).values() if v]
    agg = defaultdict(lambda: {"a": 0, "b": 0, "names": None})
    for g in games:
        t0, t1 = g["teams"]
        pair = tuple(sorted((norm(t0), norm(t1))))
        key = (g["date"],) + pair
        s = agg[key]
        # orient to sorted order: team_a = alphabetically-first normalized
        a_is_t0 = norm(t0) == pair[0]
        if s["names"] is None:
            s["names"] = (t0, t1) if a_is_t0 else (t1, t0)
        blue_won = g["blue_won"]
        a_won = blue_won if a_is_t0 else (not blue_won)
        s["a"] += 1 if a_won else 0
        s["b"] += 0 if a_won else 1
    rows = []
    for (date, na, nb), s in agg.items():
        if (date, na, nb) in primary_pairs:
            continue  # authoritative version already in Primary cache
        aw, bw = s["a"], s["b"]
        if aw == bw:
            continue  # can't call a winner (rare -- unfinished/odd aggregation)
        total = aw + bw
        best_of = 1 if total == 1 else 2 * max(aw, bw) - 1
        rows.append({
            "team_a": s["names"][0], "team_b": s["names"][1],
            "maps_won_a": aw, "maps_won_b": bw,
            "winner": "team_a" if aw > bw else "team_b",
            "best_of": best_of, "match_date": date,
            "sort_key": date, "origin": "golgg",
        })
    return rows


def walk(matches):
    """Shipped per-map LoL update. Returns preds/outcomes ONLY for
    Primary-origin matches (the pollution target), plus per-team game counts."""
    r, games = {}, {}
    preds, outs = [], []

    def apply_map(ta, tb, a):
        ar, br = r.get(ta, BASE_RATING), r.get(tb, BASE_RATING)
        d = K * (a - map_win_prob(ar, br))
        r[ta] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, ar + d))
        r[tb] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, br - d))

    for i, m in enumerate(matches):
        ta, tb, bo, w = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        ma, mb = m.get("maps_won_a"), m.get("maps_won_b")
        if m["origin"] == "primary" and i >= WARMUP and games.get(ta, 0) >= MIN_GAMES and games.get(tb, 0) >= MIN_GAMES:
            preds.append(series_prob(map_win_prob(r.get(ta, BASE_RATING), r.get(tb, BASE_RATING)), bo))
            outs.append(1.0 if w == "team_a" else 0.0)
        act = 1.0 if w == "team_a" else 0.0
        if ma is not None and mb is not None and (ma + mb) > 0:
            for _ in range(ma):
                apply_map(ta, tb, 1.0)
            for _ in range(mb):
                apply_map(ta, tb, 0.0)
        else:
            apply_map(ta, tb, act)
        games[ta] = games.get(ta, 0) + 1
        games[tb] = games.get(tb, 0) + 1
    return preds, outs, games


def series_prob(map_p, best_of):
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def main():
    primary = load_primary()
    primary_pairs = {(r["match_date"],) + tuple(sorted((norm(r["team_a"]), norm(r["team_b"])))) for r in primary}
    golgg = build_golgg_series(primary_pairs)
    print(f"Primary matches: {len(primary)} | gol.gg NEW series added: {len(golgg)}")

    primary_sorted = sorted(primary, key=lambda m: m.get("estimated_start_time") or m["match_date"])
    combined = sorted(primary + golgg, key=lambda m: m.get("estimated_start_time") or m.get("sort_key") or m["match_date"])

    p1, o1, g1 = walk(primary_sorted)
    p2, o2, g2 = walk(combined)
    print(f"\nPrimary-tier walk-forward Brier (the pollution target, {len(o1)} vs {len(o2)} scored):")
    print(f"  Primary-only training     : {brier_score(p1, o1):.5f}")
    print(f"  Primary + gol.gg training : {brier_score(p2, o2):.5f}")
    d = brier_score(p2, o2) - brier_score(p1, o1)
    print(f"  delta: {d:+.5f}  -> {'DEGRADES Primary (pollution)' if d > 0.0005 else 'no meaningful pollution' if abs(d) <= 0.0005 else 'IMPROVES Primary'}")

    # coverage win: how many gol.gg-only teams reach MIN_GAMES
    prim_teams = {norm(r["team_a"]) for r in primary} | {norm(r["team_b"]) for r in primary}
    new_rated = sum(1 for t, n in g2.items() if norm(t) not in prim_teams and n >= MIN_GAMES)
    print(f"\nCoverage: {new_rated} NEW (non-Primary) teams now have >= {MIN_GAMES} games (priceable past the gate)")


if __name__ == "__main__":
    main()
