"""Walk-forward backtest for the NBA TOTALS model
(game_lines_nba.py::prob_over, scoring_ratings_nba.py's blend) -- Phase 6.

Same "no free historical NBA odds source" caveat as backtest_spread_nba.py
-- this is a calibration + blend-vs-naive ablation check, not a market
go/no-go, mirroring backtest_team_total.py's (NFL) methodology exactly.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from app.models import game_lines_nba as G  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "nba_schedule_cache.json"
ROLLING_WINDOW = 15  # matches scoring_ratings_nba.py
MIN_GAMES_FOR_RATING = 3
OFFSETS = [-15.0, -10.0, -5.0, 0.0, 5.0, 10.0, 15.0]


def main():
    games = json.loads(CACHE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    history: dict[tuple[int, str], list[tuple[int, int]]] = {}
    blend_preds, naive_preds, outcomes = [], [], []
    buckets: dict[int, list] = defaultdict(list)

    for g in games:
        season = g["season"]
        home, away = g["home_team"], g["away_team"]
        if g.get("home_score") is None or g.get("away_score") is None:
            continue

        home_hist = history.get((season, home), [])[-ROLLING_WINDOW:]
        away_hist = history.get((season, away), [])[-ROLLING_WINDOW:]
        actual_total = g["home_score"] + g["away_score"]

        if len(home_hist) >= MIN_GAMES_FOR_RATING and len(away_hist) >= MIN_GAMES_FOR_RATING:
            home_scoring = {
                "points_scored": sum(h[0] for h in home_hist) / len(home_hist),
                "points_allowed": sum(h[1] for h in home_hist) / len(home_hist),
            }
            away_scoring = {
                "points_scored": sum(h[0] for h in away_hist) / len(away_hist),
                "points_allowed": sum(h[1] for h in away_hist) / len(away_hist),
            }
            mu, _ = G.expected_total(home_scoring, away_scoring)

            for offset in OFFSETS:
                line = mu + offset
                actual_over = 1.0 if actual_total > line else (0.0 if actual_total < line else 0.5)
                p_blend = G.prob_over(line, home_scoring, away_scoring)
                p_naive = G.prob_over(line, None, None)

                blend_preds.append(p_blend)
                naive_preds.append(p_naive)
                outcomes.append(actual_over)
                buckets[min(int(p_blend * 10), 9)].append((p_blend, actual_over))

        history.setdefault((season, home), []).append((g["home_score"], g["away_score"]))
        history.setdefault((season, away), []).append((g["away_score"], g["home_score"]))

    n = len(outcomes)
    print(f"Scored game x synthetic-line rows (REG, both teams have >= {MIN_GAMES_FOR_RATING} games this season): {n}")
    print(f"({n // len(OFFSETS)} games x {len(OFFSETS)} synthetic offsets each -- NOT real market lines, see docstring)")
    print()
    print(f"{'Model':<28}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Blend (team scoring)':<28}{brier_score(blend_preds, outcomes):>10.4f}{log_loss(blend_preds, outcomes):>10.4f}")
    print(f"{'Naive (league-mean only)':<28}{brier_score(naive_preds, outcomes):>10.4f}{log_loss(naive_preds, outcomes):>10.4f}")
    print()

    blend_b = brier_score(blend_preds, outcomes)
    naive_b = brier_score(naive_preds, outcomes)
    print("=" * 60)
    if blend_b < naive_b:
        print(f"Blend HELPS vs naive league-mean: {blend_b:.4f} (blend) < {naive_b:.4f} (naive)")
    else:
        print(f"Blend DOES NOT HELP vs naive league-mean: {blend_b:.4f} (blend) >= {naive_b:.4f} (naive)")
    print("(No real market to compare against for this market type -- see script docstring.)")
    print("=" * 60)
    print()
    print("Calibration (predicted P(over) decile vs actual over-rate, blend model):")
    for decile in sorted(buckets):
        rows = buckets[decile]
        pred_avg = sum(p for p, _ in rows) / len(rows)
        actual_avg = sum(o for _, o in rows) / len(rows)
        print(f"  {decile*10:>3}-{decile*10+10:<3}%  predicted_avg={pred_avg:.3f}  actual_rate={actual_avg:.3f}  n={len(rows)}")


if __name__ == "__main__":
    main()
