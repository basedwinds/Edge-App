"""Grid-searches elo_lol.py's K-factor against this app's own walk-forward
Brier score on the real historical match cache (5,604 matches with a real
best_of + winner, Leaguepedia's own "Primary" tournament tier -- LCK/LPL/
LEC/LCS-LTA/Worlds/MSI, 2023-mid 2026 -- see scripts/build_lol_match_cache.py).
Scores the SERIES (match) winner via elo_lol.py's own
series_score_distribution, the same real technique the live model uses for
the real KXLOLGAME/KXLOLMAP/series_total markets, not a simplified proxy.

REAL METHODOLOGY CHANGE (2026-07-20, user-requested model-quality pass):
re-derives K under elo_lol.py's own new PER-MAP update rule (one Elo update
per real map played, using the real maps_won_a/maps_won_b score split, not
one lump update per series -- see elo_lol.py::update_ratings's own
docstring). Measured against this exact dataset: best per-map K gives a
real IMPROVEMENT over the old per-series K=36 -- unlike CS2, where the
identical change measurably regressed Brier (see
derive_cs2_elo_constants.py's own docstring) and was rejected there. Shipped
here since it's the real, validated result for THIS title's own data.

Also reports Brier score broken out by "both teams have >= N real map
observations" buckets -- real data informing elo_service_lol.py's own
minimum-games confidence threshold (the same threshold that, before this
pass, was catching 19/22 (86%) of LoL's currently-active matches as
genuinely unrated -- see elo_service_lol.py's own docstring).

Run: backend/.venv/Scripts/python.exe scripts/derive_lol_elo_constants.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score, log_loss  # noqa: E402
from app.models.baseline.elo_lol import BASE_RATING, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "lol_historical_match_cache.json"

K_GRID = [4, 6, 8, 12, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64]
WARMUP = 800  # sized to this dataset's real volume, same "let ratings warm up" convention as the other 2 esports titles' own derivations


def load_matches() -> list[dict]:
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def prob_series_win_a(team_a_rating: float, team_b_rating: float, best_of: int) -> float:
    map_p = map_win_prob(team_a_rating, team_b_rating)
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run_walkforward(matches: list[dict], k: float) -> tuple[list[float], list[float], list[int]]:
    """Mirrors elo_lol.py::update_ratings exactly: per-map updates when a
    real maps_won_a/maps_won_b score is known, a single series-level update
    otherwise."""
    ratings: dict[str, float] = {}
    games: dict[str, int] = {}
    preds: list[float] = []
    outcomes: list[float] = []
    min_games_before: list[int] = []

    def apply_one_map(team_a, team_b, actual_a):
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        p_a = map_win_prob(a_r, b_r)
        delta = k * (actual_a - p_a)
        ratings[team_a] = a_r + delta
        ratings[team_b] = b_r - delta
        games[team_a] = games.get(team_a, 0) + 1
        games[team_b] = games.get(team_b, 0) + 1

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        maps_won_a, maps_won_b = m.get("maps_won_a"), m.get("maps_won_b")
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)

        preds.append(prob_series_win_a(a_r, b_r, best_of))
        outcomes.append(1.0 if winner == "team_a" else 0.0)
        min_games_before.append(min(games.get(team_a, 0), games.get(team_b, 0)))

        actual_a = 1.0 if winner == "team_a" else 0.0
        if maps_won_a is not None and maps_won_b is not None and (maps_won_a + maps_won_b) > 0:
            for _ in range(maps_won_a):
                apply_one_map(team_a, team_b, 1.0)
            for _ in range(maps_won_b):
                apply_one_map(team_a, team_b, 0.0)
        else:
            apply_one_map(team_a, team_b, actual_a)

    return preds, outcomes, min_games_before


def main():
    matches = load_matches()
    print(f"Loaded {len(matches)} real historical matches (best_of + winner both known)")

    print(f"\n{'K':>6}  {'Brier (all)':>12}  {'Brier (post-warmup)':>20}  {'LogLoss':>10}")
    results = []
    for k in K_GRID:
        preds, outcomes, _ = run_walkforward(matches, k)
        b_all = brier_score(preds, outcomes)
        b_post = brier_score(preds[WARMUP:], outcomes[WARMUP:])
        ll_post = log_loss(preds[WARMUP:], outcomes[WARMUP:])
        results.append((k, b_all, b_post, ll_post))
        print(f"{k:>6}  {b_all:>12.5f}  {b_post:>20.5f}  {ll_post:>10.5f}")

    best = min(results, key=lambda r: r[2])
    print(f"\nBest K by post-warmup Brier: K={best[0]} (Brier={best[2]:.5f})")

    preds_best, outcomes_best, min_games_best = run_walkforward(matches, best[0])
    naive_preds = [0.5] * len(outcomes_best[WARMUP:])
    naive_brier = brier_score(naive_preds, outcomes_best[WARMUP:])
    print(f"Naive 0.5 baseline Brier (post-warmup): {naive_brier:.5f}")

    accuracy = sum(
        1 for p, o in zip(preds_best[WARMUP:], outcomes_best[WARMUP:])
        if (p >= 0.5) == (o >= 0.5)
    ) / len(outcomes_best[WARMUP:])
    print(f"Accuracy at best K (post-warmup): {accuracy:.4f}")

    print(f"\n{'min games (both teams)':>24}  {'n predictions':>14}  {'Brier':>10}")
    for min_g in (0, 1, 2, 3, 5, 10, 20):
        idx = [i for i in range(WARMUP, len(matches)) if min_games_best[i] >= min_g]
        if not idx:
            continue
        p_sub = [preds_best[i] for i in idx]
        o_sub = [outcomes_best[i] for i in idx]
        print(f"{min_g:>24}  {len(idx):>14}  {brier_score(p_sub, o_sub):>10.5f}")

    if 36 not in [r[0] for r in results]:
        preds36, outcomes36, _ = run_walkforward(matches, 36)
        b36 = brier_score(preds36[WARMUP:], outcomes36[WARMUP:])
        print(f"\nOld per-series K=36 under the NEW per-map rule: Brier (post-warmup) = {b36:.5f}")


if __name__ == "__main__":
    main()
