"""Throwaway experiment (not wired into production): tests whether a
PLAYER-level rating beats the shipped TEAM Elo for LoL, mirroring the CS2 and
Valorant pilots.

Lineups: gol.gg per-game scoreboards (data/lol_game_lineups_cache.json, see
scripts/build_lol_game_lineup_cache.py) -- the source that finally bypassed
Leaguepedia's rate limit. Joined to this app's own LoL match cache by
(date, unordered team pair); within a series the lineup is the most common 5
across that series' games (LoL teams rarely sub mid-series). Coverage is
16.4% overall but concentrated in the crawl's own window (0% pre-2025 since
game ids 70000-80000 only reach back to ~2025-07, rising to 68% in 2026),
which is the recent, bettable period.

Team-side training mirrors the shipped LoL rule exactly (PER-MAP updates off
the real maps_won_a/maps_won_b split, K=24 -- see elo_lol.py), so the
baseline is the real current model. Player ratings update once per real
series. Only matches with BOTH lineups AND enough player-warmup are scored,
and the team model is scored on the SAME subset -- apples to apples.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_lol import BASE_RATING, K, RATING_CLAMP, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "lol_historical_match_cache.json"
LINEUP_CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"
PLAYER_WARMUP = 150  # skip the first N covered matches so player ratings aren't ice-cold


def norm(x):
    return re.sub(r"[^a-z0-9]", "", x.lower())


def load_matches():
    rows = [r for r in json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8")) if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def build_index():
    good = [v for v in json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")).values() if v]
    idx = defaultdict(list)
    for v in good:
        idx[(v["date"], frozenset((norm(v["teams"][0]), norm(v["teams"][1]))))].append(v)
    return idx


def lineups_for(r, idx):
    games = idx.get((r["match_date"], frozenset((norm(r["team_a"]), norm(r["team_b"])))))
    if not games:
        return None, None

    def side(team):
        cnt = Counter()
        for g in games:
            for i in (0, 1):
                if norm(g["teams"][i]) == norm(team):
                    cnt[tuple(g["lineups"][i])] += 1
        return list(cnt.most_common(1)[0][0]) if cnt else None

    return side(r["team_a"]), side(r["team_b"])


def series_prob(map_p, best_of):
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run(matches, idx, k_player):
    team_r, player_r, pg = {}, {}, {}
    tps, pps, outs = [], [], []
    covered = 0

    def apply_map(ta, tb, a):
        ar, br = team_r.get(ta, BASE_RATING), team_r.get(tb, BASE_RATING)
        d = K * (a - map_win_prob(ar, br))
        team_r[ta] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, ar + d))
        team_r[tb] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, br - d))

    for m in matches:
        ta, tb, bo, w = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        ma, mb = m.get("maps_won_a"), m.get("maps_won_b")
        la, lb = lineups_for(m, idx)
        ar, br = team_r.get(ta, BASE_RATING), team_r.get(tb, BASE_RATING)
        act = 1.0 if w == "team_a" else 0.0

        if la and lb:
            covered += 1
            if covered > PLAYER_WARMUP:
                tps.append(series_prob(map_win_prob(ar, br), bo))
                a_str = sum(player_r.get(p, BASE_RATING) for p in la) / len(la)
                b_str = sum(player_r.get(p, BASE_RATING) for p in lb) / len(lb)
                pps.append(series_prob(map_win_prob(a_str, b_str), bo))
                outs.append(act)

        if ma is not None and mb is not None and (ma + mb) > 0:
            for _ in range(ma):
                apply_map(ta, tb, 1.0)
            for _ in range(mb):
                apply_map(ta, tb, 0.0)
        else:
            apply_map(ta, tb, act)

        if la and lb:
            a_str = sum(player_r.get(p, BASE_RATING) for p in la) / len(la)
            b_str = sum(player_r.get(p, BASE_RATING) for p in lb) / len(lb)
            d = k_player * (act - map_win_prob(a_str, b_str))
            for p in la:
                player_r[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, player_r.get(p, BASE_RATING) + d)); pg[p] = pg.get(p, 0) + 1
            for p in lb:
                player_r[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, player_r.get(p, BASE_RATING) - d)); pg[p] = pg.get(p, 0) + 1

    return tps, pps, outs, pg


def main():
    matches = load_matches()
    idx = build_index()
    tp, pp, out, pg = run(matches, idx, k_player=24.0)
    import statistics
    print(f"{len(out)} evaluated matches | {len(pg)} players rated | median obs/player {statistics.median(pg.values()):.0f}")
    if not out:
        print("no evaluable matches")
        return
    base = brier_score(tp, out)
    print(f"  TEAM model Brier: {base:.5f}\n")
    print(f"{'k_player':>9}  {'pure player':>12}  {'best w':>7}  {'blend':>10}  {'vs team':>10}")
    for k_p in (8, 12, 16, 24, 32, 40):
        tp2, pp2, out2, _ = run(matches, idx, k_player=k_p)
        pure = brier_score(pp2, out2)
        best_w, best_b = None, None
        for w in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            bb = brier_score([(1 - w) * a + w * b for a, b in zip(tp2, pp2)], out2)
            if best_b is None or bb < best_b:
                best_b, best_w = bb, w
        print(f"{k_p:>9}  {pure:>12.5f}  {best_w:>7}  {best_b:>10.5f}  {best_b - base:>+10.5f}")


if __name__ == "__main__":
    main()
