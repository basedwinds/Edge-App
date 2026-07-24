"""Grid-searches elo_cs2.py's K-factor against this app's own walk-forward
Brier score on the real historical match cache (8,843 matches, 86 S-Tier +
A-Tier tournaments, 2023-06-01 through 2026-07-18 -- see
scripts/build_cs2_match_cache.py; expanded 2026-07-20 from the original
6,283-match S-Tier-only crawl to grow the real market-odds backtest sample,
see elo_cs2.py's own docstring).
Same methodology as derive_mma_elo_constants.py, adapted for CS2's real
structural difference: the thing that actually needs scoring is the SERIES
(match) winner, not a raw per-map win_prob, since that's the real, live
market shape (KXCS2GAME -- see kalshi_cs2_client.py) -- so each walk-forward
step converts the per-map Elo diff to a full best-of-N series-win
probability via elo_cs2.py's own series_score_distribution, same technique
the live model actually uses in production, not a simplified proxy.

3 matches with no real best_of are skipped (can't build a series
distribution without it, same as elo_cs2.py's own predict_and_update).

REAL EXPERIMENT tried and REJECTED here (2026-07-20, user-requested
model-quality pass): updating per real MAP played (using the real
maps_won_a/maps_won_b score split, 1-5x more Elo nudges per series) instead
of once per series was tried, re-deriving K down to a K_GRID of 2-32 to
match the new update granularity. Measured against this exact dataset, the
best per-map K (K=6) gave Brier 0.23748 post-warmup -- WORSE than this
per-series script's own K=32 at 0.23368, a real regression, not an
improvement. Rejected for CS2 specifically -- the identical per-map change
measurably IMPROVED Brier for Valorant and LoL (see
derive_valorant_elo_constants.py's/derive_lol_elo_constants.py's own
docstrings), so this isn't a universal verdict on the technique, just a
real, title-specific finding for CS2's own data. Kept as a per-series update
here.

Run: backend/.venv/Scripts/python.exe scripts/derive_cs2_elo_constants.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score, log_loss  # noqa: E402
from app.models.baseline.elo_cs2 import BASE_RATING, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"

K_GRID = [8, 12, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64]

# First ~800 matches are almost all fresh/unrated teams (BASE_RATING vs
# BASE_RATING, p=0.5 uninformative) -- same "let ratings warm up before
# scoring" convention as derive_mma_elo_constants.py's own WARMUP, sized
# down from MMA's 1500 since this dataset is smaller (8,843 vs 17,560 rows).
WARMUP = 800


def load_matches() -> list[dict]:
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def prob_series_win_a(team_a_rating: float, team_b_rating: float, best_of: int) -> float:
    map_p = map_win_prob(team_a_rating, team_b_rating)
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run_walkforward(matches: list[dict], k: float) -> tuple[list[float], list[float]]:
    ratings: dict[str, float] = {}
    preds: list[float] = []
    outcomes: list[float] = []

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)

        preds.append(prob_series_win_a(a_r, b_r, best_of))
        outcomes.append(1.0 if winner == "team_a" else 0.0)

        map_p = map_win_prob(a_r, b_r)
        actual_a = 1.0 if winner == "team_a" else 0.0
        delta = k * (actual_a - map_p)
        ratings[team_a] = a_r + delta
        ratings[team_b] = b_r - delta

    return preds, outcomes


def main():
    matches = load_matches()
    print(f"Loaded {len(matches)} real historical matches (best_of + winner both known)")

    print(f"\n{'K':>6}  {'Brier (all)':>12}  {'Brier (post-warmup)':>20}  {'LogLoss':>10}")
    results = []
    for k in K_GRID:
        preds, outcomes = run_walkforward(matches, k)
        b_all = brier_score(preds, outcomes)
        b_post = brier_score(preds[WARMUP:], outcomes[WARMUP:])
        ll_post = log_loss(preds[WARMUP:], outcomes[WARMUP:])
        results.append((k, b_all, b_post, ll_post))
        print(f"{k:>6}  {b_all:>12.5f}  {b_post:>20.5f}  {ll_post:>10.5f}")

    best = min(results, key=lambda r: r[2])
    print(f"\nBest K by post-warmup Brier: K={best[0]} (Brier={best[2]:.5f})")

    preds_best, outcomes_best = run_walkforward(matches, best[0])
    naive_preds = [0.5] * len(outcomes_best[WARMUP:])
    naive_brier = brier_score(naive_preds, outcomes_best[WARMUP:])
    print(f"Naive 0.5 baseline Brier (post-warmup): {naive_brier:.5f}")

    accuracy = sum(
        1 for p, o in zip(preds_best[WARMUP:], outcomes_best[WARMUP:])
        if (p >= 0.5) == (o >= 0.5)
    ) / len(outcomes_best[WARMUP:])
    print(f"Accuracy at best K (post-warmup): {accuracy:.4f}")

    if 32 not in [r[0] for r in results]:
        preds32, outcomes32 = run_walkforward(matches, 32)
        b32 = brier_score(preds32[WARMUP:], outcomes32[WARMUP:])
        print(f"\nCurrently shipped K=32: Brier (post-warmup) = {b32:.5f}")


if __name__ == "__main__":
    main()
