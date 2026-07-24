"""Walk-forward backtest for the TEAM-TOTAL model (game_lines.py::prob_team_over)
-- Phase 7, first check for this model.

IMPORTANT CAVEAT, unlike every other backtest script in this project:
nflverse's historical games.csv has NO team-total market line or odds (that
market -- Kalshi's KXNFLTEAMTOTAL -- doesn't exist historically to compare
against; confirmed absent from the CSV header). So this can NOT produce a
"model Brier vs de-vigged market Brier" go/no-go verdict the way
backtest_spread.py/backtest_moneyline.py/backtest_totals.py do -- there is no
real market to score against.

What this DOES do instead: a genuine out-of-sample CALIBRATION check. For
each team-game with enough rolling scoring history, generates several
synthetic candidate lines at fixed point-offsets from the model's own
predicted mean (-10.5, -7, -3.5, 0, +3.5, +7, +10.5), computes the model's
predicted P(over) at each, and checks whether games predicted at ~70% actually
went over ~70% of the time, etc. (same reliability-curve idea as this app's
live /calibration page). Also ablates the scoring BLEND (this team's trailing
offense blended with opponent's trailing defense) against a naive
league-mean-only baseline, to check the blend is actually adding value, not
just adding false confidence via a tighter (but wrong) sigma.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import nfl_data
from app.models import game_lines
from app.models.calibration import brier_score, log_loss

ROLLING_WINDOW = 8
MIN_GAMES_FOR_RATING = 3
OFFSETS = [-10.5, -7.0, -3.5, 0.0, 3.5, 7.0, 10.5]


def main():
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["week"]))

    history: dict[tuple[int, str], list[tuple[int, int]]] = {}

    blend_preds, naive_preds, outcomes = [], [], []
    buckets: dict[int, list] = defaultdict(list)  # decile -> [(pred, outcome), ...]

    for g in games:
        season = g["season"]
        home, away = g["home_team"], g["away_team"]
        has_result = g.get("home_score") is not None and g.get("away_score") is not None

        for team, opp, team_is_home in ((home, away, True), (away, home, False)):
            team_hist = history.get((season, team), [])[-ROLLING_WINDOW:]
            opp_hist = history.get((season, opp), [])[-ROLLING_WINDOW:]
            if not has_result or len(team_hist) < MIN_GAMES_FOR_RATING or len(opp_hist) < MIN_GAMES_FOR_RATING:
                continue

            team_scoring = {
                "points_scored": sum(h[0] for h in team_hist) / len(team_hist),
                "points_allowed": sum(h[1] for h in team_hist) / len(team_hist),
            }
            opp_scoring = {
                "points_scored": sum(h[0] for h in opp_hist) / len(opp_hist),
                "points_allowed": sum(h[1] for h in opp_hist) / len(opp_hist),
            }
            actual_points = g["home_score"] if team_is_home else g["away_score"]
            mu = game_lines.expected_team_points(team_scoring, opp_scoring)

            for offset in OFFSETS:
                line = mu + offset
                actual_over = 1.0 if actual_points > line else (0.0 if actual_points < line else 0.5)
                p_blend = game_lines.prob_team_over(line, team_scoring, opp_scoring)
                # prob_team_over falls back to the league-mean/naive-std constants
                # (LEAGUE_AVG_TEAM_POINTS/TEAM_NAIVE_STD) when scoring is None --
                # reuses that existing fallback path as the naive baseline instead
                # of duplicating the constants here.
                p_naive = game_lines.prob_team_over(line, None, None)

                blend_preds.append(p_blend)
                naive_preds.append(p_naive)
                outcomes.append(actual_over)
                buckets[min(int(p_blend * 10), 9)].append((p_blend, actual_over))

        if has_result:
            history.setdefault((season, home), []).append((g["home_score"], g["away_score"]))
            history.setdefault((season, away), []).append((g["away_score"], g["home_score"]))

    n = len(outcomes)
    print(f"Scored team-game x synthetic-line rows (REG, both teams have >= {MIN_GAMES_FOR_RATING} games this season): {n}")
    print(f"({n // len(OFFSETS)} team-games x {len(OFFSETS)} synthetic offsets each -- NOT real market lines, see docstring)")
    print()
    print(f"{'Model':<32}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Blend (team+opponent)':<32}{brier_score(blend_preds, outcomes):>10.4f}{log_loss(blend_preds, outcomes):>10.4f}")
    print(f"{'Naive (league-mean only)':<32}{brier_score(naive_preds, outcomes):>10.4f}{log_loss(naive_preds, outcomes):>10.4f}")
    print()

    blend_b = brier_score(blend_preds, outcomes)
    naive_b = brier_score(naive_preds, outcomes)
    print("=" * 60)
    if blend_b < naive_b:
        print(f"Blend HELPS vs naive league-mean: {blend_b:.4f} (blend) < {naive_b:.4f} (naive)")
    else:
        print(f"Blend DOES NOT HELP vs naive league-mean: {blend_b:.4f} (blend) >= {naive_b:.4f} (naive)")
    print("(No real market to compare against for this market type -- see module docstring.)")
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
