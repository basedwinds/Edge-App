"""Grid search for elo_soccer.py's borrowed starting-point constants
(HOME_ADVANTAGE_LOG, K_ATTACK/K_DEFENSE) -- flagged explicitly in that
module's own docstring as NOT yet validated against this app's own
backtest, same "borrowed, needs a real grid search" category as Tennis's
own SURFACE_MATCH_CAP/MAX_SURFACE_WEIGHT before scripts/grid_search_tennis_
surface_weight.py ran.

UNLIKE Tennis's surface-weight search (which only affected the prediction
BLEND, so one walk-forward pass could be cheaply re-scored per grid cell),
HOME_ADVANTAGE_LOG/K_ATTACK/K_DEFENSE affect the WALK-FORWARD TRAINING
DYNAMICS ITSELF -- every grid cell needs its own full re-run of
predict_and_update across the whole cache, no shortcut available. Module-
level constants are monkey-patched between runs (Python resolves bare names
in elo_soccer.py's own functions via that module's global namespace at CALL
time, not at import time, so this works without any refactor) rather than
threading a parameter through predict_match/update_ratings, which would
touch this app's own live-serving code path for a script-only need.

Metric: pooled 3-way multinomial Brier across all 5 football-data.co.uk
leagues' moneyline matches (same metric backtest_moneyline_soccer.py's own
GO/NO-GO check uses) -- lower is better. Reports the full grid, not just
the argmin, so a genuinely flat/noisy region is visible rather than
silently trusting one cell (same "smooth basin over noisy spike" bar
elo_tennis.py's own grid search applied)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models.baseline.elo_soccer as es  # noqa: E402
from app.ingestion import soccer_data  # noqa: E402
from app.models.calibration import decimal_odds_to_implied_prob, devig_three_way  # noqa: E402


def _multinomial_brier(model_probs_list, actual_idxs) -> float:
    total = 0.0
    for probs, actual in zip(model_probs_list, actual_idxs):
        one_hot = [1.0 if i == actual else 0.0 for i in range(3)]
        total += sum((p - a) ** 2 for p, a in zip(probs, one_hot))
    return total / len(model_probs_list)


def run_one_pass(matches) -> float:
    """Full walk-forward retrain + score under elo_soccer's CURRENT
    module-level constants -- returns pooled 3-way Brier across all 5
    leagues' matches with real market odds."""
    states: dict[str, es.SoccerRatingState] = {}
    model_probs, actual_idxs = [], []
    for m in matches:
        league = m["league"]
        state = states.setdefault(league, es.SoccerRatingState())
        dist = es.predict_and_update(state, m)
        if dist is None or m.get("result_ft") not in ("H", "D", "A"):
            continue
        odds_h, odds_d, odds_a = m.get("home_odds"), m.get("draw_odds"), m.get("away_odds")
        if not odds_h or not odds_d or not odds_a:
            continue
        model_probs.append((dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win()))
        actual_idxs.append({"H": 0, "D": 1, "A": 2}[m["result_ft"]])
    return _multinomial_brier(model_probs, actual_idxs)


def main():
    matches = soccer_data.load_matches()
    print(f"Total matches: {len(matches)}")

    baseline_home_adv = es.HOME_ADVANTAGE_LOG
    baseline_k = es.K_ATTACK
    print(f"Baseline (shipped) constants: HOME_ADVANTAGE_LOG={baseline_home_adv}, K_ATTACK=K_DEFENSE={baseline_k}")
    baseline_brier = run_one_pass(matches)
    print(f"Baseline pooled 3-way Brier: {baseline_brier:.4f}\n")

    print("=" * 60)
    print("STAGE 1: HOME_ADVANTAGE_LOG grid (K fixed at baseline)")
    print("=" * 60)
    es.K_ATTACK = baseline_k
    es.K_DEFENSE = baseline_k
    home_adv_grid = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45]
    home_adv_results = {}
    for val in home_adv_grid:
        es.HOME_ADVANTAGE_LOG = val
        brier = run_one_pass(matches)
        home_adv_results[val] = brier
        print(f"  HOME_ADVANTAGE_LOG={val:.2f}: Brier={brier:.4f}")
    best_home_adv = min(home_adv_results, key=home_adv_results.get)
    print(f"Best: HOME_ADVANTAGE_LOG={best_home_adv} (Brier={home_adv_results[best_home_adv]:.4f})\n")

    print("=" * 60)
    print("STAGE 2: K_ATTACK=K_DEFENSE grid (HOME_ADVANTAGE_LOG fixed at stage-1 best)")
    print("=" * 60)
    es.HOME_ADVANTAGE_LOG = best_home_adv
    k_grid = [0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.18, 0.25]
    k_results = {}
    for val in k_grid:
        es.K_ATTACK = val
        es.K_DEFENSE = val
        brier = run_one_pass(matches)
        k_results[val] = brier
        print(f"  K={val:.3f}: Brier={brier:.4f}")
    best_k = min(k_results, key=k_results.get)
    print(f"Best: K_ATTACK=K_DEFENSE={best_k} (Brier={k_results[best_k]:.4f})\n")

    print("=" * 60)
    print("STAGE 3: K_ATTACK vs K_DEFENSE independently (HOME_ADVANTAGE_LOG + K fixed at best so far)")
    print("=" * 60)
    es.K_ATTACK = best_k
    es.K_DEFENSE = best_k
    independent_grid = [0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.18]
    best_pair, best_pair_brier = (best_k, best_k), k_results[best_k]
    for k_att in independent_grid:
        for k_def in independent_grid:
            es.K_ATTACK = k_att
            es.K_DEFENSE = k_def
            brier = run_one_pass(matches)
            if brier < best_pair_brier:
                best_pair_brier = brier
                best_pair = (k_att, k_def)
    print(f"Best independent pair: K_ATTACK={best_pair[0]}, K_DEFENSE={best_pair[1]} (Brier={best_pair_brier:.4f})\n")

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Shipped constants:  HOME_ADVANTAGE_LOG={baseline_home_adv}, K_ATTACK=K_DEFENSE={baseline_k} -> Brier={baseline_brier:.4f}")
    print(f"Grid-searched best: HOME_ADVANTAGE_LOG={best_home_adv}, K_ATTACK={best_pair[0]}, K_DEFENSE={best_pair[1]} -> Brier={best_pair_brier:.4f}")
    improvement = baseline_brier - best_pair_brier
    print(f"Improvement: {improvement:.4f} ({'real' if improvement > 0.0005 else 'noise-level, not worth changing'})")


if __name__ == "__main__":
    main()
