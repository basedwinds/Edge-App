"""Phase 2 go/no-go backtest: does the Elo baseline beat the market's own
priced-in probability, using nflverse's historical closing moneylines as the
market baseline (Kalshi/Polymarket don't have deep enough price history --
Kalshi purges settled market-level data after ~9-10 weeks, confirmed in
Downloads/ufc-kalshi-polymarket/kalshi_pull.py's own findings -- so live
paper-trading against captured prices is the follow-up validation once this
baseline passes this historical check, per the approved build plan).

Walk-forward only: Elo predicts BEFORE seeing the result of each game, using
only ratings built from strictly earlier games.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import nfl_data
from app.models.baseline.elo import EloState, predict_and_update
from app.models.calibration import brier_score, log_loss, moneyline_to_implied_prob, devig_two_way


def main():
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["week"]))

    state = EloState()
    elo_preds, market_preds, favorite_preds, outcomes = [], [], [], []
    season_breakdown: dict[int, dict] = {}

    for g in games:
        p_home_elo = predict_and_update(state, g)

        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        if g["home_score"] == g["away_score"]:
            continue  # ties excluded from binary win-prob scoring, standard practice
        if g.get("home_moneyline") is None or g.get("away_moneyline") is None:
            continue  # no market line to compare against for this game

        actual_home_win = 1.0 if g["home_score"] > g["away_score"] else 0.0

        raw_home = moneyline_to_implied_prob(g["home_moneyline"])
        raw_away = moneyline_to_implied_prob(g["away_moneyline"])
        p_home_market, _ = devig_two_way(raw_home, raw_away)

        p_home_favorite = 1.0 if p_home_market >= 0.5 else 0.0
        # naive baseline needs a probability, not a hard pick, for Brier scoring;
        # score the favorite-pick baseline as accuracy separately (see below)

        elo_preds.append(p_home_elo)
        market_preds.append(p_home_market)
        favorite_preds.append(p_home_favorite)
        outcomes.append(actual_home_win)

        season_breakdown.setdefault(g["season"], {"elo": [], "market": [], "outcomes": []})
        season_breakdown[g["season"]]["elo"].append(p_home_elo)
        season_breakdown[g["season"]]["market"].append(p_home_market)
        season_breakdown[g["season"]]["outcomes"].append(actual_home_win)

    n = len(outcomes)
    print(f"Scored games (REG season, non-tie, has market moneyline): {n}")
    print()
    print(f"{'Model':<20}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Elo baseline':<20}{brier_score(elo_preds, outcomes):>10.4f}{log_loss(elo_preds, outcomes):>10.4f}")
    print(f"{'Market (de-vigged)':<20}{brier_score(market_preds, outcomes):>10.4f}{log_loss(market_preds, outcomes):>10.4f}")
    favorite_accuracy = sum(1 for p, o in zip(favorite_preds, outcomes) if p == o) / n
    print(f"{'Always-favorite acc':<20}{favorite_accuracy:>10.4f}")
    print()

    elo_b = brier_score(elo_preds, outcomes)
    market_b = brier_score(market_preds, outcomes)
    print("=" * 50)
    if elo_b < market_b:
        print(f"GO: Elo Brier ({elo_b:.4f}) beats de-vigged market Brier ({market_b:.4f})")
    else:
        print(f"NO-GO: Elo Brier ({elo_b:.4f}) does NOT beat de-vigged market Brier ({market_b:.4f})")
    print("=" * 50)
    print()
    print("Season-by-season Brier (Elo vs Market):")
    for season in sorted(season_breakdown):
        d = season_breakdown[season]
        eb = brier_score(d["elo"], d["outcomes"])
        mb = brier_score(d["market"], d["outcomes"])
        print(f"  {season}: elo={eb:.4f}  market={mb:.4f}  n={len(d['outcomes'])}")


if __name__ == "__main__":
    main()
