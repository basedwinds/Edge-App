"""Phase 1 NBA moneyline walk-forward backtest.

Unlike backtest_moneyline.py (NFL), there is NO historical closing-odds
baseline available yet -- confirmed live 2026-07-16 that the free archive
this project would otherwise reach for (sportsbookreviewsonline.com) is
dead (whole domain 404s behind Cloudflare), and no other free bulk NBA odds
source was found. So this is NOT a "beats the market" go/no-go gate the way
every NFL market backtest is -- it's a calibration/skill check against the
best real baseline available without market data: a FLAT baseline using the
dataset's own empirical home-win-rate for every game (56.74%, matches this
project's [[feedback_betting_model_baselines]] rule to never grade against a
naive 50% coin flip). The real "beats the market" question gets answered
once Phase 2 live ingestion + CLV tracking is running against real Kalshi/
Polymarket prices, same role CLV tracking already plays for NFL.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from app.models.baseline.elo_nba import EloState, predict_and_update  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "nba_schedule_cache.json"
FLAT_HOME_WIN_RATE = 0.5674  # empirical, this exact dataset -- see elo_nba.py's HOME_COURT_ADV derivation


def main():
    games = json.loads(CACHE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    state = EloState()
    elo_preds, flat_preds, favorite_preds, outcomes = [], [], [], []
    season_breakdown: dict[int, dict] = {}

    for g in games:
        p_home_elo = predict_and_update(state, g)

        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        if g["home_score"] == g["away_score"]:
            continue  # no ties in the NBA, defensive only

        actual_home_win = 1.0 if g["home_score"] > g["away_score"] else 0.0
        p_home_favorite = 1.0 if p_home_elo >= 0.5 else 0.0

        elo_preds.append(p_home_elo)
        flat_preds.append(FLAT_HOME_WIN_RATE)
        favorite_preds.append(p_home_favorite)
        outcomes.append(actual_home_win)

        season_breakdown.setdefault(g["season"], {"elo": [], "flat": [], "outcomes": []})
        season_breakdown[g["season"]]["elo"].append(p_home_elo)
        season_breakdown[g["season"]]["flat"].append(FLAT_HOME_WIN_RATE)
        season_breakdown[g["season"]]["outcomes"].append(actual_home_win)

    n = len(outcomes)
    print(f"Scored games (REG season, non-tie, final score): {n}")
    print()
    print(f"{'Model':<28}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Elo baseline':<28}{brier_score(elo_preds, outcomes):>10.4f}{log_loss(elo_preds, outcomes):>10.4f}")
    print(f"{'Flat home-win-rate':<28}{brier_score(flat_preds, outcomes):>10.4f}{log_loss(flat_preds, outcomes):>10.4f}")
    favorite_accuracy = sum(1 for p, o in zip(favorite_preds, outcomes) if p == o) / n
    print(f"{'Elo-favorite pick acc':<28}{favorite_accuracy:>10.4f}")
    print()

    elo_b = brier_score(elo_preds, outcomes)
    flat_b = brier_score(flat_preds, outcomes)
    print("=" * 60)
    if elo_b < flat_b:
        print(f"Elo Brier ({elo_b:.4f}) beats the flat home-win-rate baseline ({flat_b:.4f})")
    else:
        print(f"Elo Brier ({elo_b:.4f}) does NOT beat the flat home-win-rate baseline ({flat_b:.4f})")
    print("NOTE: this is a calibration/skill check only, NOT a market go/no-go --")
    print("no free historical NBA odds source was found (see this script's docstring).")
    print("=" * 60)
    print()
    print("Season-by-season Brier (Elo vs flat baseline):")
    for season in sorted(season_breakdown):
        d = season_breakdown[season]
        eb = brier_score(d["elo"], d["outcomes"])
        fb = brier_score(d["flat"], d["outcomes"])
        print(f"  {season}: elo={eb:.4f}  flat={fb:.4f}  n={len(d['outcomes'])}")


if __name__ == "__main__":
    main()
