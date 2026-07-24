"""Walk-forward backtest for the TOTALS model (game_lines.py) -- the totals
equivalent of backtest_moneyline.py, which only ever covered moneyline.
Never run before this session added the divisional/dome/turf structural
shifts across multiple rounds; this is the first end-to-end check of
whether those shifts actually help against a real market baseline, or (like
the reverted turnover-margin signal) look real in isolation but don't
survive contact with how the model actually uses them.

Each team's trailing-8-game scoring average is computed WALK-FORWARD (only
games strictly before the one being scored, reset each season) -- mirrors
scoring_ratings.py's live logic exactly, just replayed historically instead
of against a live snapshot.

Market baseline: nflverse's own total_line + over_odds/under_odds, de-vigged
the same way backtest_moneyline.py de-vigs moneylines -- gives a genuine
market-implied P(actual total > total_line) to score Brier against, not
just a bare line with no probability attached.

Not covered here: weather (no historical weather archive cached, live-only
Open-Meteo forecast). Isolates the STRUCTURAL shifts (divisional, dome,
turf) and, as of 2026-07-16, the EPA-mismatch signal, via a 3-way ablation:
no shift / structural-only / structural+EPA-mismatch, each vs. the real
market.

EPA-mismatch rolling features are computed WALK-FORWARD from cached PBP
(same pbp_data.py machinery backtest_moneyline_gbm.py already uses for its
own walk-forward EPA features) -- NOT the live get_current_epa_ratings()
snapshot, which would leak future-season data into early test years.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion import nfl_data
from app.ingestion.pbp_data import fetch_pbp_seasons, compute_team_week_epa, add_rolling_features
from app.models import game_lines
from app.models.calibration import brier_score, log_loss, moneyline_to_implied_prob, devig_two_way
from app.models.news_adjustment.weather_rules import is_dome_game, is_turf_game

ROLLING_WINDOW = 8
MIN_GAMES_FOR_RATING = 3
PBP_SEASONS = list(range(2012, 2026))


def main():
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] == "REG"]
    games.sort(key=lambda g: (g["season"], g["week"]))

    print("Fetching play-by-play for walk-forward EPA features (cached after first run)...")
    pbp = fetch_pbp_seasons(PBP_SEASONS)
    team_week = compute_team_week_epa(pbp)
    team_week = add_rolling_features(team_week)
    roll_lookup = {
        (row.season, row.week, row.team): (row.off_epa_roll, row.def_epa_roll) for row in team_week.itertuples()
    }

    history: dict[tuple[int, str], list[tuple[int, int]]] = {}  # (season, team) -> [(scored, allowed), ...]

    no_shift_preds, struct_preds, epa_preds, market_preds, outcomes = [], [], [], [], []
    season_breakdown: dict[int, dict] = {}

    for g in games:
        season = g["season"]
        home, away = g["home_team"], g["away_team"]

        home_hist = history.get((season, home), [])[-ROLLING_WINDOW:]
        away_hist = history.get((season, away), [])[-ROLLING_WINDOW:]

        has_result = g.get("home_score") is not None and g.get("away_score") is not None
        scoreable = (
            has_result
            and g.get("total_line") is not None
            and g.get("over_odds") not in (None, "")
            and g.get("under_odds") not in (None, "")
            and len(home_hist) >= MIN_GAMES_FOR_RATING
            and len(away_hist) >= MIN_GAMES_FOR_RATING
        )

        if scoreable:
            home_scoring = {
                "points_scored": sum(h[0] for h in home_hist) / len(home_hist),
                "points_allowed": sum(h[1] for h in home_hist) / len(home_hist),
            }
            away_scoring = {
                "points_scored": sum(h[0] for h in away_hist) / len(away_hist),
                "points_allowed": sum(h[1] for h in away_hist) / len(away_hist),
            }

            total_line = g["total_line"]
            actual_total = g["home_score"] + g["away_score"]
            actual_over = 1.0 if actual_total > total_line else (0.0 if actual_total < total_line else 0.5)

            p_over_no_shift = game_lines.prob_over(total_line, home_scoring, away_scoring, points_shift=0.0)

            structural_shift = game_lines.structural_total_shift(
                is_divisional=bool(g.get("div_game")),
                is_dome=is_dome_game(home, g.get("roof")),
                is_turf=is_turf_game(g.get("surface")),
            )
            p_over_struct = game_lines.prob_over(total_line, home_scoring, away_scoring, points_shift=structural_shift)

            home_roll = roll_lookup.get((season, g["week"], home))
            away_roll = roll_lookup.get((season, g["week"], away))
            epa_shift = 0.0
            if home_roll is not None and away_roll is not None:
                home_off, home_def = home_roll
                away_off, away_def = away_roll
                epa_shift = game_lines.epa_mismatch_total_shift(home_off, home_def, away_off, away_def)
            p_over_epa = game_lines.prob_over(
                total_line, home_scoring, away_scoring, points_shift=structural_shift + epa_shift
            )

            raw_over = moneyline_to_implied_prob(int(g["over_odds"]))
            raw_under = moneyline_to_implied_prob(int(g["under_odds"]))
            p_over_market, _ = devig_two_way(raw_over, raw_under)

            no_shift_preds.append(p_over_no_shift)
            struct_preds.append(p_over_struct)
            epa_preds.append(p_over_epa)
            market_preds.append(p_over_market)
            outcomes.append(actual_over)

            season_breakdown.setdefault(season, {"no_shift": [], "struct": [], "epa": [], "market": [], "outcomes": []})
            season_breakdown[season]["no_shift"].append(p_over_no_shift)
            season_breakdown[season]["struct"].append(p_over_struct)
            season_breakdown[season]["epa"].append(p_over_epa)
            season_breakdown[season]["market"].append(p_over_market)
            season_breakdown[season]["outcomes"].append(actual_over)

        if has_result:
            history.setdefault((season, home), []).append((g["home_score"], g["away_score"]))
            history.setdefault((season, away), []).append((g["away_score"], g["home_score"]))

    n = len(outcomes)
    print(f"\nScored games (REG, has total_line+odds, both teams have >= {MIN_GAMES_FOR_RATING} games this season): {n}")
    print()
    print(f"{'Model':<32}{'Brier':>10}{'LogLoss':>10}")
    print(f"{'No shift (baseline)':<32}{brier_score(no_shift_preds, outcomes):>10.4f}{log_loss(no_shift_preds, outcomes):>10.4f}")
    print(f"{'+ divisional/dome/turf':<32}{brier_score(struct_preds, outcomes):>10.4f}{log_loss(struct_preds, outcomes):>10.4f}")
    print(f"{'+ EPA-mismatch (new)':<32}{brier_score(epa_preds, outcomes):>10.4f}{log_loss(epa_preds, outcomes):>10.4f}")
    print(f"{'Market (de-vigged)':<32}{brier_score(market_preds, outcomes):>10.4f}{log_loss(market_preds, outcomes):>10.4f}")
    print()

    no_shift_b = brier_score(no_shift_preds, outcomes)
    struct_b = brier_score(struct_preds, outcomes)
    epa_b = brier_score(epa_preds, outcomes)
    market_b = brier_score(market_preds, outcomes)

    print("=" * 65)
    if struct_b < no_shift_b:
        print(f"Structural shifts HELP: {struct_b:.4f} (with) < {no_shift_b:.4f} (without)")
    else:
        print(f"Structural shifts DO NOT HELP: {struct_b:.4f} (with) >= {no_shift_b:.4f} (without)")
    if epa_b < struct_b:
        print(f"EPA-mismatch HELPS on top of structural: {epa_b:.4f} (with) < {struct_b:.4f} (without)")
    else:
        print(f"EPA-mismatch DOES NOT HELP on top of structural: {epa_b:.4f} (with) >= {struct_b:.4f} (without)")
    if epa_b < market_b:
        print(f"GO: full model Brier ({epa_b:.4f}) beats de-vigged market Brier ({market_b:.4f})")
    else:
        print(f"NO-GO: full model Brier ({epa_b:.4f}) does NOT beat de-vigged market Brier ({market_b:.4f})")
    print("=" * 65)
    print()

    print("Season-by-season Brier (no-shift vs +structural vs +EPA-mismatch vs market):")
    for season in sorted(season_breakdown):
        d = season_breakdown[season]
        nb = brier_score(d["no_shift"], d["outcomes"])
        sb = brier_score(d["struct"], d["outcomes"])
        eb = brier_score(d["epa"], d["outcomes"])
        mb = brier_score(d["market"], d["outcomes"])
        print(f"  {season}: no_shift={nb:.4f}  struct={sb:.4f}  epa={eb:.4f}  market={mb:.4f}  n={len(d['outcomes'])}")


if __name__ == "__main__":
    main()
