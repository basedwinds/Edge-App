"""Walk-forward backtest for the MLB run-line/margin model
(game_lines_mlb.py::prob_team_covers).

Same "no free historical MLB odds source" caveat as backtest_moneyline_mlb.py
-- this is a calibration check (synthetic candidate lines at fixed offsets
from the model's own predicted margin), not a market go/no-go, mirroring
backtest_spread_nba.py's methodology exactly. Uses the SAME point-in-time
pitcher-blended elo_diff as backtest_moneyline_mlb.py (walk-forward, no
leakage) so this checks the actual model the live app serves, not a
simplified team-Elo-only stand-in.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import json  # noqa: E402

from app.models import game_lines_mlb as G  # noqa: E402
from app.models.baseline.elo_mlb import EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402
from app.models.pitcher_ratings_mlb import MIN_IP, pitcher_elo_adjustment  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"
OFFSETS = [-6.0, -3.0, -1.5, 0.0, 1.5, 3.0, 6.0]  # runs -- matches real Kalshi run-line ladder rungs


def _snapshot_for(pitcher_cache: dict, season: int, game_date: dt.date, pitcher_id: str) -> dict | None:
    best = None
    for date_str, snap in pitcher_cache.get(str(season), {}).items():
        snap_date = dt.date.fromisoformat(date_str)
        if snap_date >= game_date:
            continue
        if best is None or snap_date > best[0]:
            best = (snap_date, snap)
    return best[1].get(pitcher_id) if best else None


def _prior_season_final(pitcher_cache: dict, season: int, pitcher_id: str) -> dict | None:
    snaps = pitcher_cache.get(str(season - 1), {})
    if not snaps:
        return None
    last_date = sorted(snaps.keys())[-1]
    return snaps[last_date].get(pitcher_id)


def _pitcher_stats_with_fallback(pitcher_cache: dict, season: int, game_date: dt.date, pitcher_id: str):
    """Returns (era, ip, is_prior_season) -- current-season snapshot if it
    clears MIN_IP, else the pitcher's final prior-season stats if THOSE
    clear MIN_IP, else (None, 0.0, False). Mirrors
    pitcher_ratings_mlb.py::PitcherRatingCache.get_adjustment's fallback
    logic exactly, since this script validates that same live behavior."""
    snap = _snapshot_for(pitcher_cache, season, game_date, pitcher_id)
    if snap and snap["ip"] >= MIN_IP:
        return snap["era"], snap["ip"], False
    prior = _prior_season_final(pitcher_cache, season, pitcher_id)
    if prior and prior["ip"] >= MIN_IP:
        return prior["era"], prior["ip"], True
    return None, 0.0, False


def main():
    games = json.loads(SCHEDULE_PATH.read_text())
    pitcher_cache = json.loads(PITCHER_CACHE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "R" and g["season"] < 2026]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))

    state = EloState()
    preds, outcomes = [], []
    buckets: dict[int, list] = defaultdict(list)

    for g in games:
        home_field_adv = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        home_r = state.get(g["home_team"])
        away_r = state.get(g["away_team"])

        pitcher_adj = 0.0
        home_pid, away_pid = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if home_pid and away_pid:
            game_date = dt.date.fromisoformat(g["gameday"])
            home_era, home_ip, home_prior = _pitcher_stats_with_fallback(pitcher_cache, g["season"], game_date, str(home_pid))
            away_era, away_ip, away_prior = _pitcher_stats_with_fallback(pitcher_cache, g["season"], game_date, str(away_pid))
            if home_era is not None and away_era is not None:
                pitcher_adj = pitcher_elo_adjustment(home_era, away_era, home_ip, away_ip, home_prior, away_prior)

        elo_diff = (home_r + pitcher_adj + home_field_adv) - away_r
        predict_and_update(state, g)  # walk forward regardless

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
    print(f"{'Elo+pitcher margin':<20}{brier_score(preds, outcomes):>10.4f}{log_loss(preds, outcomes):>10.4f}")
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
