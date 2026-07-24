"""Phase 1 MLB moneyline walk-forward backtest.

Same status as backtest_moneyline_nba.py: NO free historical MLB betting-line
archive exists (confirmed live 2026-07-17 -- ESPN's site API embeds real
DraftKings odds, but only for CURRENT/upcoming games, never historical/
completed ones; sportsbookreviewsonline.com is dead, same domain-wide 404
already confirmed for NBA). So this is a calibration/skill check against the
dataset's own empirical home-win-rate (53.22%, matches
[[feedback_betting_model_baselines]]'s "never grade against a naive 50%
coin flip" rule), not a "beats the market" go/no-go gate. That question gets
answered once live CLV tracking is running against real Kalshi/Polymarket
prices, same role it plays for NFL/NBA.

Compares team-Elo-alone against team-Elo blended with the starting-pitcher
signal (pitcher_ratings_mlb.py) -- current-season ERA when available
(MIN_IP cleared), falling back to the pitcher's final PRIOR-season ERA
otherwise (added 2026-07-17 after auditing coverage: current-season-only
left 32.6% of games with no pitcher signal at all). Reports coverage at each
tier so the real size of the current-season vs. prior-season-fallback
contribution is visible, not just a single blended number.

K/regression/no-MOV in elo_mlb.py, and the pitcher-signal constants in
pitcher_ratings_mlb.py, were both validated against real data before being
trusted -- see check_mlb_pitcher_signal.py and each module's own docstring.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import json  # noqa: E402

from app.models.baseline.elo_mlb import (  # noqa: E402
    EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update, win_prob,
)
from app.models.calibration import brier_score, log_loss  # noqa: E402
from app.models.pitcher_ratings_mlb import MIN_IP, pitcher_elo_adjustment  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"
FLAT_HOME_WIN_RATE = 0.5322  # empirical, this exact dataset -- see elo_mlb.py's HOME_FIELD_ADV derivation


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


def main():
    games = json.loads(SCHEDULE_PATH.read_text())
    pitcher_cache = json.loads(PITCHER_CACHE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "R" and g["season"] < 2026]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))

    state = EloState()
    all_elo_preds, all_flat_preds, all_outcomes = [], [], []
    # "sub" = current-season-only pitcher data (both starters cleared MIN_IP this season)
    sub_elo_preds, sub_blend_preds, sub_flat_preds, sub_outcomes = [], [], [], []
    # "full" = current-season-only PLUS prior-season-fallback-rescued games
    full_elo_preds, full_blend_preds, full_outcomes = [], [], []
    rescued_count = 0

    for g in games:
        home_field_adv = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        home_r = state.get(g["home_team"])
        away_r = state.get(g["away_team"])
        p_home_elo = win_prob(home_r, away_r, home_field_adv)
        predict_and_update(state, g)  # walk forward regardless of downstream qualification

        if g.get("home_score") is None or g.get("away_score") is None or g["home_score"] == g["away_score"]:
            continue
        outcome = 1.0 if g["home_score"] > g["away_score"] else 0.0

        all_elo_preds.append(p_home_elo)
        all_flat_preds.append(FLAT_HOME_WIN_RATE)
        all_outcomes.append(outcome)

        home_pid, away_pid = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if not home_pid or not away_pid:
            continue
        game_date = dt.date.fromisoformat(g["gameday"])
        home_snap = _snapshot_for(pitcher_cache, g["season"], game_date, str(home_pid))
        away_snap = _snapshot_for(pitcher_cache, g["season"], game_date, str(away_pid))

        home_current_ok = home_snap is not None and home_snap["ip"] >= MIN_IP
        away_current_ok = away_snap is not None and away_snap["ip"] >= MIN_IP

        if home_current_ok and away_current_ok:
            pitcher_adj = pitcher_elo_adjustment(home_snap["era"], away_snap["era"], home_snap["ip"], away_snap["ip"])
            p_blend = win_prob(home_r + pitcher_adj, away_r, home_field_adv)
            sub_elo_preds.append(p_home_elo)
            sub_blend_preds.append(p_blend)
            sub_flat_preds.append(FLAT_HOME_WIN_RATE)
            sub_outcomes.append(outcome)
            full_elo_preds.append(p_home_elo)
            full_blend_preds.append(p_blend)
            full_outcomes.append(outcome)
            continue

        # Try prior-season fallback for whichever side lacks current-season data
        home_era, home_ip, home_prior = (home_snap["era"], home_snap["ip"], False) if home_current_ok else (None, 0.0, True)
        away_era, away_ip, away_prior = (away_snap["era"], away_snap["ip"], False) if away_current_ok else (None, 0.0, True)
        if home_prior:
            prior = _prior_season_final(pitcher_cache, g["season"], str(home_pid))
            if prior and prior["ip"] >= MIN_IP:
                home_era, home_ip = prior["era"], prior["ip"]
            else:
                home_prior = False  # no usable prior-season data either
        if away_prior:
            prior = _prior_season_final(pitcher_cache, g["season"], str(away_pid))
            if prior and prior["ip"] >= MIN_IP:
                away_era, away_ip = prior["era"], prior["ip"]
            else:
                away_prior = False

        if home_era is None or away_era is None or home_ip < MIN_IP or away_ip < MIN_IP:
            continue  # genuinely no usable pitcher data on one or both sides

        rescued_count += 1
        pitcher_adj = pitcher_elo_adjustment(home_era, away_era, home_ip, away_ip, home_prior, away_prior)
        p_blend = win_prob(home_r + pitcher_adj, away_r, home_field_adv)
        full_elo_preds.append(p_home_elo)
        full_blend_preds.append(p_blend)
        full_outcomes.append(outcome)

    print(f"Full dataset -- Elo vs flat baseline (n={len(all_outcomes)}):")
    print(f"  Elo Brier:  {brier_score(all_elo_preds, all_outcomes):.4f}  LogLoss: {log_loss(all_elo_preds, all_outcomes):.4f}")
    print(f"  Flat Brier: {brier_score(all_flat_preds, all_outcomes):.4f}  LogLoss: {log_loss(all_flat_preds, all_outcomes):.4f}")
    print()
    print(f"Current-season-only pitcher data (n={len(sub_outcomes)}) -- Elo-alone vs Elo+pitcher-blend vs flat:")
    print(f"  Elo-alone Brier:  {brier_score(sub_elo_preds, sub_outcomes):.4f}")
    print(f"  Elo+pitcher Brier:{brier_score(sub_blend_preds, sub_outcomes):.4f}")
    print(f"  Flat Brier:       {brier_score(sub_flat_preds, sub_outcomes):.4f}")
    print()
    print(f"+ prior-season fallback rescues {rescued_count} more games "
          f"({rescued_count / len(all_outcomes) * 100:.1f}% of all games)")
    print(f"Full pitcher-blend-eligible coverage (n={len(full_outcomes)}, "
          f"{len(full_outcomes) / len(all_outcomes) * 100:.1f}% of all games) -- Elo-alone vs Elo+pitcher-blend:")
    print(f"  Elo-alone Brier:  {brier_score(full_elo_preds, full_outcomes):.4f}")
    print(f"  Elo+pitcher Brier:{brier_score(full_blend_preds, full_outcomes):.4f}")
    print()
    blend_b, elo_sub_b = brier_score(full_blend_preds, full_outcomes), brier_score(full_elo_preds, full_outcomes)
    print("=" * 70)
    if blend_b < elo_sub_b:
        print(f"Pitcher blend ({blend_b:.4f}) beats team-Elo-alone ({elo_sub_b:.4f}) on the same games")
    else:
        print(f"Pitcher blend ({blend_b:.4f}) does NOT beat team-Elo-alone ({elo_sub_b:.4f}) on the same games")
    print("NOTE: calibration/skill check only, NOT a market go/no-go -- no free")
    print("historical MLB odds source was found (see this script's docstring).")
    print("=" * 70)


if __name__ == "__main__":
    main()
