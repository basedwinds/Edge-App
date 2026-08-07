"""Do the LoL h2h / player blends still help when they move the price A LOT?

LoL twin of check_valorant_blend_by_move_size.py. Written because the LoL drawer
now explains its blends but deliberately does NOT carry Valorant's "measurably
beat team Elo in backtest" claim -- that had only been measured for Valorant, and
asserting it for LoL without measuring would be claiming something unshown. This
is the measurement.

Same question and same shape: 20 of 63 live LoL matches have the blends moving the
price by 5pp or more (up to 17.3pp), and it is exactly those large moves that
generate edges big enough to clear the 10pp staking gate. An aggregate Brier
improvement is compatible with the blends helping on small nudges and hurting on
the big swings, so predictions are binned by how far the blend moved them and
blended vs Elo-only Brier is compared WITHIN each bin.

TWO REAL DIFFERENCES FROM THE VALORANT RUN, both of which matter:

  * H2H_PRIOR_WEIGHT is 48 here versus Valorant's 10, i.e. a far stronger prior
    on Elo, so head-to-head barely moves the number per meeting. Most of LoL's
    movement is the PLAYER blend.
  * Lineup coverage is thin and recent -- 0% before 2025 (gol.gg game ids only
    reach back to ~2025-07), rising to 68% in 2026, 16.4% overall. So the player
    stage applies to a minority of matches, and the bins that include it are
    correspondingly smaller. That is a real limit on what this can conclude, not
    a detail to bury.

Mirrors the shipped rules exactly: per-map team updates off the real
maps_won_a/maps_won_b split at K=24 with RATING_CLAMP, player ratings updating
once per series at K_PLAYER, and the production blend arithmetic from
elo_service_lol._blend_h2h / _blend_player. The (date, unordered team pair) join
and most-common-5 lineup selection are lifted from
test_lol_player_level_signal.py rather than re-invented.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/check_lol_blend_by_move_size.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.baseline.elo_lol import (  # noqa: E402
    BASE_RATING, K, K_PLAYER, PLAYER_BLEND_WEIGHT, RATING_CLAMP, map_win_prob, series_score_distribution,
)
from app.models.calibration import brier_score  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "lol_historical_match_cache.json"
LINEUP_CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"
H2H_PRIOR_WEIGHT = 48.0  # as shipped, elo_service_lol
WARMUP = 500
BINS = ((0.0, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 1.01))


def norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def load_matches() -> list[dict]:
    rows = [r for r in json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
            if r.get("best_of") and r.get("winner")]
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


def series_prob(a_r: float, b_r: float, best_of: int) -> float:
    dist = series_score_distribution(map_win_prob(a_r, b_r), best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run() -> None:
    matches = load_matches()
    idx = build_index()
    print(f"{len(matches)} matches | {sum(len(v) for v in idx.values())} lineup games indexed")

    team_r: dict[str, float] = {}
    player_r: dict[str, float] = {}
    h2h: dict[tuple, tuple[int, int]] = {}
    rows: list[tuple[float, float, float]] = []
    covered = 0

    def clamp(v: float) -> float:
        return max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, v))

    for m in matches:
        ta, tb, bo, w = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        y = 1.0 if w == "team_a" else 0.0
        ar, br = team_r.get(ta, BASE_RATING), team_r.get(tb, BASE_RATING)
        p_elo = series_prob(ar, br, bo)
        p = p_elo

        key = tuple(sorted((ta, tb)))
        wins_first, total = h2h.get(key, (0, 0))
        if total >= 1:
            wins_a = wins_first if ta == key[0] else (total - wins_first)
            p = (p * H2H_PRIOR_WEIGHT + wins_a) / (H2H_PRIOR_WEIGHT + total)

        la, lb = lineups_for(m, idx)
        if la and lb:
            covered += 1
            sa = sum(player_r.get(x, BASE_RATING) for x in la) / len(la)
            sb = sum(player_r.get(x, BASE_RATING) for x in lb) / len(lb)
            p = (1.0 - PLAYER_BLEND_WEIGHT) * p + PLAYER_BLEND_WEIGHT * series_prob(sa, sb, bo)

        rows.append((p_elo, p, y))

        # ---- updates, mirroring the shipped rules --------------------------
        ma, mb = m.get("maps_won_a"), m.get("maps_won_b")
        actuals = ([1.0] * int(ma) + [0.0] * int(mb)) if (ma is not None and mb is not None) else [y]
        for actual in actuals:
            a2, b2 = team_r.get(ta, BASE_RATING), team_r.get(tb, BASE_RATING)
            d = K * (actual - map_win_prob(a2, b2))
            team_r[ta], team_r[tb] = clamp(a2 + d), clamp(b2 - d)
        if la and lb:
            sa = sum(player_r.get(x, BASE_RATING) for x in la) / len(la)
            sb = sum(player_r.get(x, BASE_RATING) for x in lb) / len(lb)
            d = K_PLAYER * (y - map_win_prob(sa, sb))
            for x in la:
                player_r[x] = clamp(player_r.get(x, BASE_RATING) + d)
            for x in lb:
                player_r[x] = clamp(player_r.get(x, BASE_RATING) - d)
        h2h[key] = (wins_first + (1 if (w == "team_a") == (ta == key[0]) else 0), total + 1)

    rows = rows[WARMUP:]
    print(f"lineup coverage: {covered}/{len(matches)} matches ({covered / len(matches):.1%})")
    print(f"\nevaluated {len(rows)} predictions (post-warmup)\n")
    print(f"{'move size':>14}{'n':>8}{'Brier elo':>12}{'Brier blend':>13}{'delta':>10}{'95% CI':>22}   verdict")
    import random as _r
    for lo, hi in BINS:
        sub = [r for r in rows if lo <= abs(r[1] - r[0]) < hi]
        if len(sub) < 30:
            continue
        be = brier_score([x[0] for x in sub], [x[2] for x in sub])
        bb = brier_score([x[1] for x in sub], [x[2] for x in sub])
        rng = _r.Random(20260807)
        deltas = []
        for _ in range(2000):
            s = [sub[rng.randrange(len(sub))] for _ in range(len(sub))]
            deltas.append(brier_score([x[1] for x in s], [x[2] for x in s])
                          - brier_score([x[0] for x in s], [x[2] for x in s]))
        deltas.sort()
        lo_ci, hi_ci = deltas[50], deltas[1949]
        verdict = "blend HELPS" if hi_ci < 0 else ("blend HURTS" if lo_ci > 0 else "inconclusive")
        print(f"{f'{lo:.0%}-{hi:.0%}':>14}{len(sub):>8}{be:>12.5f}{bb:>13.5f}{bb - be:>+10.5f}"
              f"  [{lo_ci:+.5f},{hi_ci:+.5f}]   {verdict}")

    be = brier_score([x[0] for x in rows], [x[2] for x in rows])
    bb = brier_score([x[1] for x in rows], [x[2] for x in rows])
    print(f"\n{'ALL':>14}{len(rows):>8}{be:>12.5f}{bb:>13.5f}{bb - be:>+10.5f}")


if __name__ == "__main__":
    run()
