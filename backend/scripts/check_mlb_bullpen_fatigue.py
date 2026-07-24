"""Investigation script (not a registered backtest): does recent bullpen
workload carry real, non-redundant predictive signal beyond team Elo (+
starting-pitcher blend) -- checked on real data BEFORE committing to build
it, same "check before you build" discipline as check_mlb_pitcher_signal.py.
Flagged in this project's own notes as "the most promising remaining
MLB-native candidate" but never checked because it needs box-score-level
pitching lines -- see build_mlb_boxscore_cache.py.

Feature: for each team-game, TRAILING_DAYS calendar days immediately before
this game (not including today), sum that team's own RELIEF pitch count
(every pitcher after the starter in boxscore's own appearance-order list) --
a rough "how much bullpen arm was used recently" workload proxy. Higher
workload -> more fatigued -> expected to hurt that team's own performance
today, so the test is whether (away_workload - home_workload), i.e. the
home team's REST ADVANTAGE, has a positive coefficient predicting home wins.

Only ONE season (2024) of box-score data was cached (see
build_mlb_boxscore_cache.py's own scoping note) -- too little for a
per-season sign-consistency check like the pitcher-signal validation used,
so this instead splits the season in half (first vs second half by date) as
a same-season out-of-sample consistency check, alongside the usual
redundancy-with-Elo correlation check and a walk-forward Brier comparison.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import json  # noqa: E402
from collections import defaultdict  # noqa: E402

import numpy as np  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from app.models.baseline.elo_mlb import EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.pitcher_ratings_mlb import MIN_IP, pitcher_elo_adjustment  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
BOXSCORE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_boxscore_cache.json"
PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"
TARGET_SEASON = 2024
TRAILING_DAYS = 2


def _snapshot_for(pitcher_cache: dict, season: int, game_date: dt.date, pitcher_id: str) -> dict | None:
    best = None
    for date_str, snap in pitcher_cache.get(str(season), {}).items():
        snap_date = dt.date.fromisoformat(date_str)
        if snap_date >= game_date:
            continue
        if best is None or snap_date > best[0]:
            best = (snap_date, snap)
    return best[1].get(pitcher_id) if best else None


def main():
    games = json.loads(SCHEDULE_PATH.read_text())
    boxscores = json.loads(BOXSCORE_CACHE_PATH.read_text())
    pitcher_cache = json.loads(PITCHER_CACHE_PATH.read_text())
    season_games = [g for g in games if g["game_type"] == "R" and g["season"] == TARGET_SEASON]
    season_games.sort(key=lambda g: (g["gameday"], g["game_number"], g["id"]))
    print(f"{len(season_games)} {TARGET_SEASON} REG games, {len(boxscores)} box scores cached")

    # Build per-team relief-pitch-count-by-date history from the box score cache.
    relief_by_team_date: dict[str, dict[dt.date, float]] = defaultdict(dict)
    for gid, box in boxscores.items():
        d = dt.date.fromisoformat(box["gameday"])
        relief_by_team_date[box["home_team"]][d] = box["home"]["relief_pitches"]
        relief_by_team_date[box["away_team"]][d] = box["away"]["relief_pitches"]

    def trailing_workload(team: str, game_date: dt.date) -> float:
        history = relief_by_team_date.get(team, {})
        return sum(
            pitches for d, pitches in history.items()
            if 0 < (game_date - d).days <= TRAILING_DAYS
        )

    # Walk-forward Elo+pitcher-blend state, same as backtest_moneyline_mlb.py, so
    # this checks the feature against the SAME baseline the live app actually uses.
    state = EloState()
    rows = []  # (gameday, elo_diff, workload_advantage, outcome, margin)
    skipped_no_data = 0

    for g in season_games:
        home_field_adv = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        home_r = state.get(g["home_team"])
        away_r = state.get(g["away_team"])

        pitcher_adj = 0.0
        home_pid, away_pid = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if home_pid and away_pid:
            game_date_p = dt.date.fromisoformat(g["gameday"])
            home_snap = _snapshot_for(pitcher_cache, g["season"], game_date_p, str(home_pid))
            away_snap = _snapshot_for(pitcher_cache, g["season"], game_date_p, str(away_pid))
            if home_snap and away_snap and home_snap["ip"] >= MIN_IP and away_snap["ip"] >= MIN_IP:
                pitcher_adj = pitcher_elo_adjustment(home_snap["era"], away_snap["era"], home_snap["ip"], away_snap["ip"])

        elo_diff = (home_r + pitcher_adj + home_field_adv) - away_r
        predict_and_update(state, g)  # walk forward regardless of downstream qualification

        if g.get("home_score") is None or g.get("away_score") is None or g["home_score"] == g["away_score"]:
            continue
        if g["id"] not in boxscores:
            skipped_no_data += 1
            continue

        game_date = dt.date.fromisoformat(g["gameday"])
        home_workload = trailing_workload(g["home_team"], game_date)
        away_workload = trailing_workload(g["away_team"], game_date)
        # positive = home team's bullpen is MORE RESTED than away's (away threw more recently)
        rest_advantage = away_workload - home_workload

        outcome = 1.0 if g["home_score"] > g["away_score"] else 0.0
        margin = g["home_score"] - g["away_score"]
        rows.append((g["gameday"], elo_diff, rest_advantage, outcome, margin))

    print(f"Qualifying games: {len(rows)}  (skipped, no box score cached: {skipped_no_data})")
    print()

    gamedays = [r[0] for r in rows]
    elo_diffs = np.array([r[1] for r in rows])
    rest_adv = np.array([r[2] for r in rows])
    outcomes = np.array([r[3] for r in rows])
    margins = np.array([r[4] for r in rows])

    print(f"rest_advantage: mean={rest_adv.mean():.1f} pitches, std={rest_adv.std():.1f}, "
          f"range=[{rest_adv.min():.0f}, {rest_adv.max():.0f}]")
    print(f"Raw correlation, rest_advantage vs outcome: {np.corrcoef(rest_adv, outcomes)[0, 1]:.4f}")
    print(f"Raw correlation, rest_advantage vs run margin: {np.corrcoef(rest_adv, margins)[0, 1]:.4f}")
    print(f"Raw correlation, rest_advantage vs elo_diff (redundancy check): {np.corrcoef(rest_adv, elo_diffs)[0, 1]:.4f}")
    print()

    mid = sorted(gamedays)[len(gamedays) // 2]
    halves = {"first_half": np.array([d < mid for d in gamedays]), "second_half": np.array([d >= mid for d in gamedays])}

    print(f"{'Half':<14}{'coef_elo':>12}{'coef_rest':>12}{'n':>8}")
    coefs = {}
    for name, mask in halves.items():
        X = np.column_stack([elo_diffs[mask], rest_adv[mask]])
        y = outcomes[mask]
        Xs = StandardScaler().fit_transform(X)
        clf = LogisticRegression()
        clf.fit(Xs, y)
        coefs[name] = clf.coef_[0][1]
        print(f"{name:<14}{clf.coef_[0][0]:>12.4f}{clf.coef_[0][1]:>12.4f}{mask.sum():>8}")

    X_full = np.column_stack([elo_diffs, rest_adv])
    Xs_full = StandardScaler().fit_transform(X_full)
    clf_full = LogisticRegression()
    clf_full.fit(Xs_full, outcomes)
    print()
    print(f"Full-season coef: elo={clf_full.coef_[0][0]:.4f}  rest_advantage={clf_full.coef_[0][1]:.4f}  n={len(rows)}")
    same_sign = (coefs["first_half"] > 0) == (coefs["second_half"] > 0) == (clf_full.coef_[0][1] > 0)
    print(f"Sign-consistent across first half / second half / full season: {same_sign}")

    # Brier comparison: elo-only vs elo+rest_advantage, both as plain logistic fits
    # on the SAME data (not the live model's own calibrated win_prob -- this is a
    # skill-ceiling check on whether the feature helps at all, same role
    # check_mlb_pitcher_signal.py's correlation checks played before that signal
    # was trusted enough to fold into the live model).
    clf_elo_only = LogisticRegression()
    Xs_elo_only = StandardScaler().fit_transform(elo_diffs.reshape(-1, 1))
    clf_elo_only.fit(Xs_elo_only, outcomes)
    p_elo_only = clf_elo_only.predict_proba(Xs_elo_only)[:, 1]
    p_blend = clf_full.predict_proba(Xs_full)[:, 1]
    print()
    print(f"In-sample Brier -- elo-only: {brier_score(list(p_elo_only), list(outcomes)):.4f}  "
          f"elo+rest_advantage: {brier_score(list(p_blend), list(outcomes)):.4f}")
    print("(in-sample, not walk-forward -- a quick skill-ceiling check, not a final validated number)")


if __name__ == "__main__":
    main()
