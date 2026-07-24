"""Grid-search SURFACE_MATCH_CAP/MAX_SURFACE_WEIGHT (elo_tennis.py) against
this app's own walk-forward backtest -- these two constants were borrowed
from the user's standalone tennis-model project (40 matches to saturate, 75%
max surface weight) as a reasonable starting point, never re-derived fresh
here (see elo_tennis.py's own docstring).

Efficient by construction, not by brute force: SURFACE_MATCH_CAP/
MAX_SURFACE_WEIGHT only affect blended_rating() at PREDICTION time -- they
never feed update_ratings(), so a player's overall/surface ratings evolve
identically no matter what these two constants are set to. Walking forward
through 500k+ matches once, capturing each match's raw (overall_r, surface_r,
surface_n) for both players BEFORE that match updates them, is enough to
re-score every grid point by just recomputing the blend + win_prob + Brier
afterward -- one slow pass instead of one slow pass per grid cell.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import tennis_data  # noqa: E402
from app.models.baseline.elo_tennis import (  # noqa: E402
    TennisEloState, dynamic_k, update_ratings, win_prob,
)
from app.models.calibration import brier_score, devig_two_way  # noqa: E402


def capture_raw_ingredients(matches: list[dict]) -> list[dict]:
    """One walk-forward pass. For each SCOREABLE match (real result,
    non-retirement, has 2-way market odds), records what blended_rating()
    would need for either player, at BOTH surface-aware and surface=None
    (pure overall) shape, so the grid search can recompute the blend for any
    (cap, max_weight) pair without re-running the Elo walk-forward."""
    state = TennisEloState()
    rows = []
    for m in matches:
        player_a_key, player_b_key = m["player_a_key"], m["player_b_key"]
        surface = m.get("surface")
        a_overall, _ = state.get_overall(player_a_key)
        b_overall, _ = state.get_overall(player_b_key)
        a_surf_r, a_surf_n = state.get_surface(player_a_key, surface)
        b_surf_r, b_surf_n = state.get_surface(player_b_key, surface)

        winner_key = m.get("winner_key")
        has_result = winner_key is not None and not m.get("is_retirement")

        if winner_key is not None and not m.get("is_retirement"):
            update_ratings(state, player_a_key, player_b_key, winner_key, surface)
        elif winner_key is None:
            pass  # not yet played, nothing to update
        # real retirement: predict-only, no update (matches predict_and_update's own rule)

        if not has_result:
            continue
        odds_a, odds_b = m.get("player_a_odds"), m.get("player_b_odds")
        if not odds_a or not odds_b or odds_a <= 1.0 or odds_b <= 1.0:
            continue
        raw_a, raw_b = 1.0 / odds_a, 1.0 / odds_b
        p_a_market, _ = devig_two_way(raw_a, raw_b)
        actual_a = 1.0 if winner_key == player_a_key else 0.0

        rows.append({
            "tier": m["tier"],
            "surface": surface,
            "a_overall": a_overall, "a_surf_r": a_surf_r, "a_surf_n": a_surf_n,
            "b_overall": b_overall, "b_surf_r": b_surf_r, "b_surf_n": b_surf_n,
            "market_p_a": p_a_market,
            "actual_a": actual_a,
        })
    return rows


def blend(overall_r: float, surf_r: float, surf_n: int, surface: str | None, cap: float, max_weight: float) -> float:
    if surface is None:
        return overall_r
    weight = min(surf_n / cap, 1.0) * max_weight
    return weight * surf_r + (1 - weight) * overall_r


def score_grid_point(rows: list[dict], cap: float, max_weight: float) -> float:
    """Returns Brier score for this (cap, max_weight) pair, pooled across
    all tiers -- mirrors backtest_moneyline_tennis.py's own pooled report."""
    model_p, actual = [], []
    for r in rows:
        a_r = blend(r["a_overall"], r["a_surf_r"], r["a_surf_n"], r["surface"], cap, max_weight)
        b_r = blend(r["b_overall"], r["b_surf_r"], r["b_surf_n"], r["surface"], cap, max_weight)
        model_p.append(win_prob(a_r, b_r))
        actual.append(r["actual_a"])
    return brier_score(model_p, actual)


def main():
    matches = tennis_data.load_matches()
    print(f"Total matches in merged cache: {len(matches)}")
    rows = capture_raw_ingredients(matches)
    print(f"Scored matches (real result, non-retirement, has 2-way market odds): {len(rows)}")

    # Current shipped values, for a clear before/after comparison.
    current_cap, current_weight = 40.0, 0.75
    current_brier = score_grid_point(rows, current_cap, current_weight)
    market_brier = brier_score([r["market_p_a"] for r in rows], [r["actual_a"] for r in rows])
    print(f"\nCurrent shipped (cap={current_cap}, max_weight={current_weight}): Brier={current_brier:.5f}  (n={len(rows)})")
    print(f"Market (de-vigged) Brier: {market_brier:.5f}  (reference -- the actual go/no-go bar)\n")

    CAP_GRID = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 80.0, 100.0, 150.0]
    WEIGHT_GRID = [0.0, 0.15, 0.25, 0.35, 0.5, 0.65, 0.75, 0.85, 1.0]

    print(f"{'cap':>6}{'max_w':>8}{'brier':>10}")
    best = None
    for cap in CAP_GRID:
        for weight in WEIGHT_GRID:
            if weight == 0.0 and cap != CAP_GRID[0]:
                continue  # weight=0 means surface never matters -- cap is irrelevant, only test it once
            b = score_grid_point(rows, cap, weight)
            print(f"{cap:>6.0f}{weight:>8.2f}{b:>10.5f}")
            if best is None or b < best[2]:
                best = (cap, weight, b)

    print(f"\nBest coarse grid point: cap={best[0]}, max_weight={best[1]}, Brier={best[2]:.5f}")

    # Fine refinement pass around the coarse optimum.
    FINE_CAP_GRID = [4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0]
    FINE_WEIGHT_GRID = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    print(f"\nFine refinement:\n{'cap':>6}{'max_w':>8}{'brier':>10}")
    for cap in FINE_CAP_GRID:
        for weight in FINE_WEIGHT_GRID:
            b = score_grid_point(rows, cap, weight)
            print(f"{cap:>6.0f}{weight:>8.2f}{b:>10.5f}")
            if b < best[2]:
                best = (cap, weight, b)

    print(f"\nBest overall: cap={best[0]}, max_weight={best[1]}, Brier={best[2]:.5f}")
    print(f"Current shipped: cap={current_cap}, max_weight={current_weight}, Brier={current_brier:.5f}")
    delta = current_brier - best[2]
    print(f"Improvement over current: {delta:.5f} Brier ({'real' if abs(delta) > 0.0002 else 'noise-level'})")

    print("\nPer-tier breakdown, current vs best:")
    for tier in ("tour", "challenger", "itf"):
        tier_rows = [r for r in rows if r["tier"] == tier]
        if not tier_rows:
            continue
        cur_b = score_grid_point(tier_rows, current_cap, current_weight)
        best_b = score_grid_point(tier_rows, best[0], best[1])
        mkt_b = brier_score([r["market_p_a"] for r in tier_rows], [r["actual_a"] for r in tier_rows])
        print(f"  {tier.upper():<12} n={len(tier_rows):>7}  current={cur_b:.5f}  best={best_b:.5f}  market={mkt_b:.5f}")


if __name__ == "__main__":
    main()
