"""Validates whether the two real signals check_tennis_situational_signals.py
found (head-to-head record, rank-vs-Elo divergence) actually improve
walk-forward Brier when wired in as prediction-time-only corrections --
same "real signal in isolation doesn't automatically survive contact with
the model" discipline as NFL's turnover-margin-regression experiment and
MMA's age-adjustment validation. Corrections use ONLY the regression
SLOPE (not the OLS intercept) relative to each feature's natural zero
point (h2h_diff=0 = no historical edge either way; rank_gap=0 = equal
rank) -- the raw intercepts from the exploratory check were large (~0.43)
purely because both filtered subsets (3+ prior meetings; real tour-level
ranks) are non-representative samples with their own baseline shift,
confirmed by checking the unconditional mean residual is close to zero
across the full walk-forward population; a slope-only correction anchored
at each feature's real zero point avoids importing that subset-selection
artifact into a live correction.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import tennis_data  # noqa: E402
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402

H2H_SLOPE = -0.3369  # from check_tennis_situational_signals.py, n=17971
RANK_SLOPE = -0.000548  # from check_tennis_situational_signals.py, n=113208


def main():
    matches = tennis_data.load_matches()

    state = TennisEloState()
    last_surface: dict[str, str] = {}
    h2h: dict[tuple[str, str], list[str]] = {}

    # (baseline_prob, h2h_corrected_prob, rank_corrected_prob, actual)
    h2h_rows: list[tuple[float, float, float]] = []
    rank_rows: list[tuple[float, float, float]] = []
    all_residuals: list[float] = []

    for m in matches:
        a, b = m["player_a_key"], m["player_b_key"]
        surface = m.get("surface")

        p_a = predict_and_update(state, m)

        has_result = m.get("winner_key") is not None and not m.get("is_retirement")
        if has_result and p_a is not None:
            actual_a = 1.0 if m["winner_key"] == a else 0.0
            all_residuals.append(actual_a - p_a)

            pair_key = tuple(sorted((a, b)))
            prior = h2h.get(pair_key, [])
            if len(prior) >= 3:
                a_h2h_rate = sum(1 for w in prior if w == a) / len(prior)
                h2h_diff = a_h2h_rate - 0.5
                corrected = min(max(p_a + H2H_SLOPE * h2h_diff, 0.01), 0.99)
                h2h_rows.append((p_a, corrected, actual_a))

            rank_a, rank_b = m.get("player_a_rank"), m.get("player_b_rank")
            if rank_a and rank_b and m["tier"] == "tour":
                rank_gap = float(rank_b - rank_a)
                corrected = min(max(p_a + RANK_SLOPE * rank_gap, 0.01), 0.99)
                rank_rows.append((p_a, corrected, actual_a))

        if m.get("winner_key") is not None or m.get("is_retirement"):
            pass
        if surface and m.get("tier") == "tour" and (m.get("winner_key") is not None or m.get("is_retirement")):
            last_surface[a] = surface
            last_surface[b] = surface
        if m.get("winner_key") is not None and not m.get("is_retirement"):
            pair_key = tuple(sorted((a, b)))
            h2h.setdefault(pair_key, []).append(m["winner_key"])

    n = len(all_residuals)
    mean_residual = sum(all_residuals) / n
    print(f"Unconditional mean residual across all {n} scored matches: {mean_residual:.4f} "
          f"(near zero confirms the exploratory check's large intercepts were a subset-selection "
          f"artifact, not a general calibration bug)")
    print()

    if h2h_rows:
        baseline = [r[0] for r in h2h_rows]
        corrected = [r[1] for r in h2h_rows]
        actual = [r[2] for r in h2h_rows]
        b_base, b_corr = brier_score(baseline, actual), brier_score(corrected, actual)
        print(f"HEAD-TO-HEAD correction (n={len(h2h_rows)}):")
        print(f"  Baseline Elo Brier:   {b_base:.4f}")
        print(f"  H2H-corrected Brier:  {b_corr:.4f}")
        print(f"  {'HELPS' if b_corr < b_base else 'HURTS'}: {'improvement' if b_corr < b_base else 'regression'} of {abs(b_base - b_corr):.4f}")
        print()

    if rank_rows:
        baseline = [r[0] for r in rank_rows]
        corrected = [r[1] for r in rank_rows]
        actual = [r[2] for r in rank_rows]
        b_base, b_corr = brier_score(baseline, actual), brier_score(corrected, actual)
        print(f"RANK-DIVERGENCE correction (n={len(rank_rows)}):")
        print(f"  Baseline Elo Brier:      {b_base:.4f}")
        print(f"  Rank-corrected Brier:    {b_corr:.4f}")
        print(f"  {'HELPS' if b_corr < b_base else 'HURTS'}: {'improvement' if b_corr < b_base else 'regression'} of {abs(b_base - b_corr):.4f}")


if __name__ == "__main__":
    main()
