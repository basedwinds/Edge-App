"""Walk-forward backtest for the NBA MARGIN/spread model
(game_lines_nba.py::prob_team_covers) -- Phase 6.

Same caveat as backtest_moneyline_nba.py: no free historical NBA odds source
was found (checked sportsbookreviewsonline.com -- domain dead -- and Covers'
Sports Odds History -- has per-game odds history for NFL but only futures/
award odds for NBA, confirmed live 2026-07-16), so this can't produce a
"model Brier vs real market Brier" go/no-go the way NFL's backtest_spread.py
does.

What this DOES do instead, mirroring backtest_team_total.py's (NFL)
methodology exactly since it solved the identical "no market data" problem:
generates synthetic candidate spread lines at fixed offsets from the model's
own predicted margin, then checks whether games the model predicts to cover
at ~70% actually cover ~70% of the time (a genuine out-of-sample calibration
check of the Normal-distribution assumption behind MARGIN_STD, which the
raw regression residual std alone doesn't guarantee -- real margins could
have fatter tails than a Normal implies).
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from app.models import game_lines_nba as G  # noqa: E402
from app.models.baseline.elo_nba import EloState, effective_home_court_adv, predict_and_update  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "nba_schedule_cache.json"
OFFSETS = [-10.5, -7.0, -3.5, 0.0, 3.5, 7.0, 10.5]


def main():
    games = json.loads(CACHE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    state = EloState()
    preds, outcomes = [], []
    buckets: dict[int, list] = defaultdict(list)

    for g in games:
        # predict_and_update ALSO updates ratings with the real result -- capture
        # elo_diff via a side call to avoid double-updating; re-derive elo_diff
        # from state BEFORE calling predict_and_update (which mutates state).
        state.start_season_if_new(g["season"])
        home_adv = effective_home_court_adv(g["home_team"], g.get("location"), g.get("home_rest"), g.get("away_rest"))
        home_r = state.get(g["home_team"])
        away_r = state.get(g["away_team"])
        elo_diff = (home_r + home_adv) - away_r

        p_home = predict_and_update(state, g)  # updates ratings for next iteration

        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        actual_margin = g["home_score"] - g["away_score"]
        mu = G.expected_margin(elo_diff)

        for offset in OFFSETS:
            line = mu + offset
            actual_covers = 1.0 if actual_margin > line else (0.0 if actual_margin < line else 0.5)
            p_cover = G.prob_team_covers(True, line, elo_diff)  # home perspective
            preds.append(p_cover)
            outcomes.append(actual_covers)
            buckets[min(int(p_cover * 10), 9)].append((p_cover, actual_covers))

    n = len(outcomes)
    print(f"Scored game x synthetic-line rows (REG, home perspective): {n}")
    print(f"({n // len(OFFSETS)} games x {len(OFFSETS)} synthetic offsets each -- NOT real market lines, see docstring)")
    print()
    print(f"{'Model':<20}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Elo margin model':<20}{brier_score(preds, outcomes):>10.4f}{log_loss(preds, outcomes):>10.4f}")
    print()
    print("NOTE: no real market to compare against for this market type (see script docstring).")
    print()
    print("Calibration (predicted P(cover) decile vs actual cover-rate):")
    for decile in sorted(buckets):
        rows = buckets[decile]
        pred_avg = sum(p for p, _ in rows) / len(rows)
        actual_avg = sum(o for _, o in rows) / len(rows)
        print(f"  {decile*10:>3}-{decile*10+10:<3}%  predicted_avg={pred_avg:.3f}  actual_rate={actual_avg:.3f}  n={len(rows)}")


if __name__ == "__main__":
    main()
