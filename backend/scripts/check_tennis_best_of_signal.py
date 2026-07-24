"""Checks whether match FORMAT (best-of-3 vs best-of-5) explains residual
variance the Elo model's own win_prob curve currently ignores entirely --
elo_tennis.py uses one fixed logistic curve regardless of format, but a
longer format (more sets to win) mechanically reduces upset variance for a
fixed per-set skill gap (same reasoning as any best-of-N series shrinking
in a series' number of games). Best-of-5 is ATP Grand Slams only (WTA is
always best-of-3 at every level, Challenger/ITF are always best-of-3) --
scoped to ATP tour-level matches only, where both formats actually occur.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import tennis_data  # noqa: E402
from app.models.baseline.elo_tennis import TennisEloState, predict_and_update  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402


def main():
    matches = tennis_data.load_matches()
    state = TennisEloState()

    bo3_preds, bo3_actuals = [], []
    bo5_preds, bo5_actuals = [], []

    for m in matches:
        p_a = predict_and_update(state, m)
        if m.get("winner_key") is None or m.get("is_retirement") or p_a is None:
            continue
        if m["tour"] != "atp" or m["tier"] != "tour" or m.get("best_of") not in (3, 5):
            continue
        actual_a = 1.0 if m["winner_key"] == m["player_a_key"] else 0.0
        if m["best_of"] == 5:
            bo5_preds.append(p_a)
            bo5_actuals.append(actual_a)
        else:
            bo3_preds.append(p_a)
            bo3_actuals.append(actual_a)

    def favorite_acc(preds, actuals):
        return sum(1 for p, a in zip(preds, actuals) if (p >= 0.5) == (a == 1.0)) / len(actuals)

    print(f"ATP tour, BEST-OF-3 (n={len(bo3_actuals)}): Elo Brier={brier_score(bo3_preds, bo3_actuals):.4f}, "
          f"favorite-implied-by-Elo acc={favorite_acc(bo3_preds, bo3_actuals):.4f}, "
          f"mean predicted-for-actual-winner={sum(p if a==1 else 1-p for p,a in zip(bo3_preds,bo3_actuals))/len(bo3_actuals):.4f}")
    print(f"ATP tour, BEST-OF-5 (n={len(bo5_actuals)}): Elo Brier={brier_score(bo5_preds, bo5_actuals):.4f}, "
          f"favorite-implied-by-Elo acc={favorite_acc(bo5_preds, bo5_actuals):.4f}, "
          f"mean predicted-for-actual-winner={sum(p if a==1 else 1-p for p,a in zip(bo5_preds,bo5_actuals))/len(bo5_actuals):.4f}")


if __name__ == "__main__":
    main()
