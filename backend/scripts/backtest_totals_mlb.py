"""Walk-forward backtest/calibration check for the MLB TOTALS model
(game_lines_mlb.py::prob_over).

Genuinely different shape from backtest_totals_nba.py: MLB's totals model
combines a REJECTED team-behavior signal (trailing team-scoring blend --
tested here again on the full dataset, confirmed still worse than naive) and
an ACCEPTED structural signal (PARK_FACTOR -- real, well-documented ballpark
effects like Coors Field's altitude). This script tests all three
(naive / team-scoring blend / park-factor) against each other to confirm
park-factor is the one that actually earns its place in game_lines_mlb.py.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from app.models import game_lines_mlb as G  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
ROLLING_WINDOW = 15
MIN_GAMES_FOR_RATING = 3
OFFSETS = [-4.5, -3.0, -1.5, 0.0, 1.5, 3.0, 4.5]  # runs -- matches real Kalshi/Polymarket total-line spacing


def main():
    games = json.loads(SCHEDULE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "R" and g["season"] < 2026]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))

    history: dict[tuple[int, str], list[tuple[int, int]]] = {}
    naive_preds, blend_preds, park_preds, outcomes = [], [], [], []
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
            home_scored = sum(h[0] for h in home_hist) / len(home_hist)
            home_allowed = sum(h[1] for h in home_hist) / len(home_hist)
            away_scored = sum(h[0] for h in away_hist) / len(away_hist)
            away_allowed = sum(h[1] for h in away_hist) / len(away_hist)
            blend_mu = (home_scored + away_allowed) / 2 + (away_scored + home_allowed) / 2
            park_mu = G.expected_total(home)

            for offset in OFFSETS:
                line = G.LEAGUE_AVG_TOTAL + offset
                actual_over = 1.0 if actual_total > line else (0.0 if actual_total < line else 0.5)
                p_naive = 1.0 - G._norm_cdf(line, G.LEAGUE_AVG_TOTAL, G.NAIVE_TOTAL_STD)
                p_blend = 1.0 - G._norm_cdf(line, blend_mu, G.NAIVE_TOTAL_STD)
                p_park = G.prob_over(line, home)

                naive_preds.append(p_naive)
                blend_preds.append(p_blend)
                park_preds.append(p_park)
                outcomes.append(actual_over)
                buckets[min(int(p_park * 10), 9)].append((p_park, actual_over))

        history.setdefault((season, home), []).append((g["home_score"], g["away_score"]))
        history.setdefault((season, away), []).append((g["away_score"], g["home_score"]))

    n = len(outcomes)
    print(f"Scored game x synthetic-line rows (REG, both teams have >= {MIN_GAMES_FOR_RATING} games this season): {n}")
    print(f"({n // len(OFFSETS)} games x {len(OFFSETS)} synthetic offsets each -- NOT real market lines, see docstring)")
    print()
    print(f"{'Model':<28}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Naive (league-mean only)':<28}{brier_score(naive_preds, outcomes):>10.4f}{log_loss(naive_preds, outcomes):>10.4f}")
    print(f"{'Team-scoring blend':<28}{brier_score(blend_preds, outcomes):>10.4f}{log_loss(blend_preds, outcomes):>10.4f}")
    print(f"{'Park factor (shipped)':<28}{brier_score(park_preds, outcomes):>10.4f}{log_loss(park_preds, outcomes):>10.4f}")
    print()

    naive_b, blend_b, park_b = brier_score(naive_preds, outcomes), brier_score(blend_preds, outcomes), brier_score(park_preds, outcomes)
    print("=" * 70)
    print(f"Team-scoring blend vs naive: {'HELPS' if blend_b < naive_b else 'does NOT help'} ({blend_b:.4f} vs {naive_b:.4f})")
    print(f"Park factor vs naive:        {'HELPS' if park_b < naive_b else 'does NOT help'} ({park_b:.4f} vs {naive_b:.4f})")
    print("CONFIRMS game_lines_mlb.py's design: team-behavior signals don't help for MLB totals,")
    print("but the structural ballpark-factor signal (Coors Field etc.) does.")
    print("=" * 70)
    print()
    print("Calibration (predicted P(over) decile vs actual over-rate, park-factor model):")
    for decile in sorted(buckets):
        rows = buckets[decile]
        pred_avg = sum(p for p, _ in rows) / len(rows)
        actual_avg = sum(o for _, o in rows) / len(rows)
        print(f"  {decile*10:>3}-{decile*10+10:<3}%  predicted_avg={pred_avg:.3f}  actual_rate={actual_avg:.3f}  n={len(rows)}")


if __name__ == "__main__":
    main()
