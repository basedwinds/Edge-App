"""Validates a principled best-of-5 correction for elo_tennis.py's win_prob:
the model's ratings/K are calibrated in aggregate against a MIXED dataset
that's ~81% best-of-3 (WTA/Challenger/ITF are always Bo3; only ATP Grand
Slams are Bo5), so its raw logistic output is implicitly a Bo3-calibrated
match probability. Real found signal (check_tennis_best_of_signal.py):
Elo's own favorite-pick accuracy is 64.90% in Bo3 vs 72.39% in Bo5 -- a
longer format mechanically reduces upset variance for the same per-set
skill gap (standard best-of-N series math), and the current model applies
NO format-aware adjustment at all.

Correction (standard approach, same one 538-style tennis models use):
invert the Bo3 match-win formula P = p^2*(3-2p) to recover an implied
per-SET probability p, then re-derive the Bo5 match probability from that
SAME per-set p via the real Bo5 binomial formula
P5 = p^3 + 3p^3(1-p) + 6p^3(1-p)^2 -- not an ad-hoc rescaling, an exact
combinatorial re-derivation assuming i.i.d. per-set outcomes (a standard,
if simplified, assumption in published tennis forecasting).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import tennis_data  # noqa: E402
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402


def set_prob_from_bo3_match_prob(p_match: float, tol: float = 1e-6) -> float:
    """Inverts P = p^2*(3-2p) for p via bisection (monotonic increasing on
    [0,1], confirmed: derivative 6p(1-p) >= 0)."""
    p_match = min(max(p_match, 1e-6), 1 - 1e-6)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        val = mid * mid * (3 - 2 * mid)
        if val < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bo5_match_prob_from_set_prob(p: float) -> float:
    return p**3 + 3 * p**3 * (1 - p) + 6 * p**3 * (1 - p) ** 2


def corrected_bo5_prob(p_match_bo3_style: float) -> float:
    p_set = set_prob_from_bo3_match_prob(p_match_bo3_style)
    return bo5_match_prob_from_set_prob(p_set)


def main():
    matches = tennis_data.load_matches()
    state = TennisEloState()

    baseline_preds, corrected_preds, actuals = [], [], []

    for m in matches:
        p_a = predict_and_update(state, m)
        if m.get("winner_key") is None or m.get("is_retirement") or p_a is None:
            continue
        if m["tour"] != "atp" or m["tier"] != "tour" or m.get("best_of") != 5:
            continue
        actual_a = 1.0 if m["winner_key"] == m["player_a_key"] else 0.0
        baseline_preds.append(p_a)
        corrected_preds.append(corrected_bo5_prob(p_a))
        actuals.append(actual_a)

    n = len(actuals)
    b_base = brier_score(baseline_preds, actuals)
    b_corr = brier_score(corrected_preds, actuals)
    print(f"ATP Grand Slam (best-of-5) matches, n={n}")
    print(f"  Baseline Elo (uncorrected) Brier:  {b_base:.4f}")
    print(f"  Bo5-corrected Brier:               {b_corr:.4f}")
    print(f"  {'HELPS' if b_corr < b_base else 'HURTS'}: {'improvement' if b_corr < b_base else 'regression'} of {abs(b_base - b_corr):.4f}")

    # Sanity check the transform itself: a coin-flip match should stay a
    # coin-flip regardless of format, and a lopsided match should become
    # even MORE lopsided in Bo5 (matches the real-world intuition, not just
    # the abstract math).
    print()
    print("Sanity checks on the transform itself:")
    for p in (0.5, 0.6, 0.7, 0.8, 0.9):
        print(f"  Bo3-style p={p:.2f} -> implied per-set p={set_prob_from_bo3_match_prob(p):.4f} -> Bo5 match p={corrected_bo5_prob(p):.4f}")


if __name__ == "__main__":
    main()
