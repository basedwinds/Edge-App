"""Phase 2 v1.5 upgrade, attempt 2: regularized logistic regression on
engineered differential features, instead of a GBM (which likely overfit --
~3400 games is thin for a tree ensemble with no regularization tuning).
Walk-forward by season (train on all prior seasons, predict the next).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

from app.ingestion import nfl_data
from app.ingestion.pbp_data import fetch_pbp_seasons, compute_team_week_epa, add_rolling_features
from app.models.baseline.elo import EloState, HOME_FIELD_ADV, win_prob, update_ratings
from app.models.calibration import brier_score, log_loss, moneyline_to_implied_prob, devig_two_way

PBP_SEASONS = list(range(2012, 2026))  # 2012-2025 inclusive
FIRST_TEST_SEASON = 2016  # leaves 2012-2015 as pure rolling-feature warmup + minimum training history


def build_dataset():
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] == "REG" and g["season"] in range(PBP_SEASONS[0], PBP_SEASONS[-1] + 1)]
    games.sort(key=lambda g: (g["season"], g["week"]))

    print("Fetching play-by-play for", len(PBP_SEASONS), "seasons (cached after first run)...")
    pbp = fetch_pbp_seasons(PBP_SEASONS)
    team_week = compute_team_week_epa(pbp)
    team_week = add_rolling_features(team_week)
    roll_lookup = {
        (row.season, row.week, row.team): (row.off_epa_roll, row.def_epa_roll) for row in team_week.itertuples()
    }

    elo_state = EloState()
    rows = []
    for g in games:
        elo_state.start_season_if_new(g["season"])
        home_r = elo_state.get(g["home_team"])
        away_r = elo_state.get(g["away_team"])
        p_home_elo = win_prob(home_r, away_r)

        has_result = g.get("home_score") is not None and g.get("away_score") is not None
        if has_result:
            update_ratings(elo_state, g["home_team"], g["away_team"], g["home_score"], g["away_score"])

        if not has_result or g["home_score"] == g["away_score"]:
            continue
        if g.get("home_moneyline") is None or g.get("away_moneyline") is None:
            continue

        home_roll = roll_lookup.get((g["season"], g["week"], g["home_team"]))
        away_roll = roll_lookup.get((g["season"], g["week"], g["away_team"]))
        if home_roll is None or away_roll is None:
            continue
        if any(v is None or pd.isna(v) for v in [*home_roll, *away_roll]):
            continue

        raw_home = moneyline_to_implied_prob(g["home_moneyline"])
        raw_away = moneyline_to_implied_prob(g["away_moneyline"])
        p_home_market, _ = devig_two_way(raw_home, raw_away)

        home_rest = g.get("home_rest")
        away_rest = g.get("away_rest")

        rows.append(
            {
                "season": g["season"],
                "week": g["week"],
                "off_epa_diff": home_roll[0] - away_roll[1],  # home offense vs away defense
                "def_epa_diff": away_roll[0] - home_roll[1],  # away offense vs home defense
                "elo_rating_diff": (home_r + HOME_FIELD_ADV) - away_r,
                "rest_diff": (home_rest - away_rest) if (home_rest is not None and away_rest is not None) else 0,
                "div_game": g.get("div_game") or 0,
                "elo_prob": p_home_elo,
                "market_prob": p_home_market,
                "home_win": 1 if g["home_score"] > g["away_score"] else 0,
            }
        )

    return pd.DataFrame(rows)


def main():
    df = build_dataset()
    print(f"Total scoreable games with features: {len(df)}")

    feature_cols = ["off_epa_diff", "def_epa_diff", "elo_rating_diff", "rest_diff", "div_game"]

    test_seasons = sorted(s for s in df["season"].unique() if s >= FIRST_TEST_SEASON)
    all_model_preds, all_elo_preds, all_market_preds, all_outcomes = [], [], [], []
    season_rows = []

    for season in test_seasons:
        train = df[df["season"] < season]
        test = df[df["season"] == season]
        if len(train) < 200 or len(test) == 0:
            continue

        model = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=1000))
        model.fit(train[feature_cols], train["home_win"])
        model_pred = model.predict_proba(test[feature_cols])[:, 1]

        all_model_preds.extend(model_pred.tolist())
        all_elo_preds.extend(test["elo_prob"].tolist())
        all_market_preds.extend(test["market_prob"].tolist())
        all_outcomes.extend(test["home_win"].tolist())

        season_rows.append(
            {
                "season": season,
                "n": len(test),
                "model_brier": brier_score(model_pred.tolist(), test["home_win"].tolist()),
                "elo_brier": brier_score(test["elo_prob"].tolist(), test["home_win"].tolist()),
                "market_brier": brier_score(test["market_prob"].tolist(), test["home_win"].tolist()),
            }
        )

    n = len(all_outcomes)
    print(f"\nWalk-forward test games ({FIRST_TEST_SEASON}-{max(test_seasons)}): {n}")
    print()
    print(f"{'Model':<24}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'EPA+Elo logistic reg':<24}{brier_score(all_model_preds, all_outcomes):>10.4f}{log_loss(all_model_preds, all_outcomes):>10.4f}")
    print(f"{'Elo baseline':<24}{brier_score(all_elo_preds, all_outcomes):>10.4f}{log_loss(all_elo_preds, all_outcomes):>10.4f}")
    print(f"{'Market (de-vigged)':<24}{brier_score(all_market_preds, all_outcomes):>10.4f}{log_loss(all_market_preds, all_outcomes):>10.4f}")

    model_b = brier_score(all_model_preds, all_outcomes)
    market_b = brier_score(all_market_preds, all_outcomes)
    print()
    print("=" * 55)
    if model_b < market_b:
        print(f"GO: model Brier ({model_b:.4f}) beats de-vigged market Brier ({market_b:.4f})")
    else:
        print(f"NO-GO: model Brier ({model_b:.4f}) does NOT beat de-vigged market Brier ({market_b:.4f})")
    print("=" * 55)

    print("\nSeason-by-season:")
    for r in season_rows:
        print(f"  {r['season']}: model={r['model_brier']:.4f}  elo={r['elo_brier']:.4f}  market={r['market_brier']:.4f}  n={r['n']}")


if __name__ == "__main__":
    main()
