"""Checks whether elo_soccer.py's independent-Poisson model (no Dixon-Coles
low-score correlation adjustment, see MatchGoalDistribution's own docstring)
shows a REAL calibration gap concentrated in low-scoring games -- the
specific condition that docstring says would justify adding the correction,
rather than adding it on general principle.

Dixon-Coles' own real finding (1997): real match data shows 0-0, 1-0, 0-1,
and 1-1 scorelines occur MORE often than independent Poisson predicts,
because a low-scoring match has a real, mild positive correlation between
the two teams' goal counts (cagey/defensive matches suppress BOTH sides'
scoring together) that assuming independence misses. Testable directly
against this app's own real 61,144-match cache: compare the model's own
predicted P(exact scoreline) against the REAL empirical frequency for each
of the 4 scorelines Dixon-Coles specifically corrects, walk-forward (so this
uses the same real predictions the backtest itself scores, not a refit)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import soccer_data  # noqa: E402
from app.models.baseline.elo_soccer import SoccerRatingState, predict_and_update  # noqa: E402


def main():
    matches = soccer_data.load_matches()
    print(f"Total matches: {len(matches)}")

    states: dict[str, SoccerRatingState] = {}
    # scoreline -> [sum of predicted P(scoreline), count of matches actually ending in it, total scored matches]
    scorelines_to_check = [(0, 0), (1, 0), (0, 1), (1, 1)]
    predicted_sum = {sl: 0.0 for sl in scorelines_to_check}
    actual_count = {sl: 0 for sl in scorelines_to_check}
    n_scored = 0

    for m in matches:
        league = m["league"]
        state = states.setdefault(league, SoccerRatingState())
        dist = predict_and_update(state, m)
        if dist is None:
            continue
        home_goals, away_goals = m.get("home_goals_ft"), m.get("away_goals_ft")
        if home_goals is None or away_goals is None:
            continue
        n_scored += 1
        for sl in scorelines_to_check:
            h, a = sl
            predicted_sum[sl] += dist.grid[h][a]
            if (home_goals, away_goals) == sl:
                actual_count[sl] += 1

    print(f"Scored matches: {n_scored}\n")
    print(f"{'Scoreline':<12}{'Predicted avg P':>18}{'Actual freq':>16}{'Ratio (actual/pred)':>22}")
    for sl in scorelines_to_check:
        pred_avg = predicted_sum[sl] / n_scored
        actual_freq = actual_count[sl] / n_scored
        ratio = actual_freq / pred_avg if pred_avg > 0 else float("nan")
        print(f"{str(sl):<12}{pred_avg:>18.4f}{actual_freq:>16.4f}{ratio:>22.3f}")

    print("\nDixon-Coles' own real finding: actual/predicted ratio for these 4 scorelines")
    print("should be systematically ABOVE 1.0 (low scores under-predicted by independent")
    print("Poisson) if the correction is worth adding. A ratio close to 1.0 across all four")
    print("means this app's own real data doesn't show the gap the correction fixes.")


if __name__ == "__main__":
    main()
