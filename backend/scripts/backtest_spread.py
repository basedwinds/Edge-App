"""Walk-forward backtest for the SPREAD model (game_lines.py::prob_team_covers)
-- Phase 7, first check for the spread model, which has never been validated
against real historical outcomes despite being live in the app with real
suggested stakes attached.

Market baseline: nflverse's own spread_line + home_spread_odds/away_spread_odds,
de-vigged the same way backtest_moneyline.py de-vigs moneylines. Sign
convention confirmed against real 2024 data before writing this: spread_line
is HOME team's expected margin (positive = home favored by that many points),
so "home covers" means actual home margin (home_score - away_score) >
spread_line -- exactly the threshold game_lines.prob_team_covers already
expects, no sign flip needed.

Elo ratings are walked forward exactly like backtest_moneyline.py (no
leakage -- elo_diff for each game is captured from state BEFORE that game's
result updates it). MARGIN_SLOPE/MARGIN_STD themselves, however, were fit
using the FULL 2012-2025 dataset (see game_lines.py's module docstring) --
this backtest is honestly NOT a fully out-of-sample test of those two
constants (same caveat that would apply to re-deriving them walk-forward
season-by-season, which isn't done here). It IS a genuine walk-forward test
of the Elo ratings feeding into them, which is where most of the game-to-game
variation actually comes from.

Does NOT include the news/situational layer (blended into spread via
elo.py::implied_elo_diff at request time) -- those modules run against LIVE
current data (injuries, weather, playoff standings), not historical
snapshots, so there is no way to replay them walk-forward against past
seasons. Same limitation backtest_totals.py already documents for weather.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import nfl_data
from app.models import game_lines
from app.models.baseline.elo import EloState, effective_home_field_adv, update_ratings
from app.models.calibration import brier_score, log_loss, moneyline_to_implied_prob, devig_two_way


def main():
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["week"]))

    state = EloState()
    model_preds, market_preds, outcomes = [], [], []
    pushes = 0
    season_breakdown: dict[int, dict] = {}

    for g in games:
        season = g["season"]
        home, away = g["home_team"], g["away_team"]
        state.start_season_if_new(season)
        home_field_adv = effective_home_field_adv(home, g.get("location"))
        home_r = state.get(home)
        away_r = state.get(away)
        elo_diff = (home_r + home_field_adv) - away_r

        has_result = g.get("home_score") is not None and g.get("away_score") is not None
        scoreable = (
            has_result
            and g.get("spread_line") is not None
            and g.get("home_spread_odds") not in (None, "")
            and g.get("away_spread_odds") not in (None, "")
        )

        if scoreable:
            spread_line = g["spread_line"]
            home_margin = g["home_score"] - g["away_score"]

            if home_margin > spread_line:
                actual_cover = 1.0
            elif home_margin < spread_line:
                actual_cover = 0.0
            else:
                actual_cover = 0.5
                pushes += 1

            p_home_covers = game_lines.prob_team_covers(True, spread_line, elo_diff)

            raw_home = moneyline_to_implied_prob(int(g["home_spread_odds"]))
            raw_away = moneyline_to_implied_prob(int(g["away_spread_odds"]))
            p_home_market, _ = devig_two_way(raw_home, raw_away)

            model_preds.append(p_home_covers)
            market_preds.append(p_home_market)
            outcomes.append(actual_cover)

            season_breakdown.setdefault(season, {"model": [], "market": [], "outcomes": []})
            season_breakdown[season]["model"].append(p_home_covers)
            season_breakdown[season]["market"].append(p_home_market)
            season_breakdown[season]["outcomes"].append(actual_cover)

        if has_result:
            update_ratings(state, home, away, g["home_score"], g["away_score"], home_field_adv)

    n = len(outcomes)
    print(f"Scored games (REG, has spread_line + both-side odds): {n} ({pushes} pushes)")
    print()
    print(f"{'Model':<24}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'Elo margin model':<24}{brier_score(model_preds, outcomes):>10.4f}{log_loss(model_preds, outcomes):>10.4f}")
    print(f"{'Market (de-vigged)':<24}{brier_score(market_preds, outcomes):>10.4f}{log_loss(market_preds, outcomes):>10.4f}")
    print()

    model_b = brier_score(model_preds, outcomes)
    market_b = brier_score(market_preds, outcomes)
    print("=" * 55)
    if model_b < market_b:
        print(f"GO: model Brier ({model_b:.4f}) beats de-vigged market Brier ({market_b:.4f})")
    else:
        print(f"NO-GO: model Brier ({model_b:.4f}) does NOT beat de-vigged market Brier ({market_b:.4f})")
    print("=" * 55)
    print()

    print("Season-by-season Brier (model vs market):")
    for season in sorted(season_breakdown):
        d = season_breakdown[season]
        mb = brier_score(d["model"], d["outcomes"])
        mkb = brier_score(d["market"], d["outcomes"])
        print(f"  {season}: model={mb:.4f}  market={mkb:.4f}  n={len(d['outcomes'])}")


if __name__ == "__main__":
    main()
