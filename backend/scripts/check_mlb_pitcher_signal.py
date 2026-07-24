"""Investigation script (not a registered backtest): does starting-pitcher
quality differential carry real, non-redundant predictive signal beyond team
Elo -- checked on real data BEFORE committing to build the full blended
baseline, same "check before you build" discipline as the NFL EPA-mismatch
validation (per-season coefficient sign-consistency + correlation with Elo
itself, see [[project_unified_prediction_market_app]] memory).

Uses data/mlb_pitcher_snapshot_cache.json (point-in-time cumulative ERA,
snapshotted every 14 days, walk-forward-safe -- see
build_mlb_pitcher_snapshot_cache.py) joined against each game's own
probable-pitcher id (data/mlb_schedule_cache.json). Games are skipped (not
guessed) if: no prior in-season snapshot exists yet (first ~2 weeks of a
season), a probable-pitcher id is missing, or either starter has under
MIN_IP innings in that snapshot (too small a sample to trust an ERA).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import json  # noqa: E402

import numpy as np  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from app.models.baseline.elo_mlb import EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"
MIN_IP = 15.0
LEAGUE_AVG_ERA = 4.20  # rough modern-era MLB average, used only to CAP outlier small-sample ERAs, not as a rating


def _snapshot_for(pitcher_cache: dict, season: int, game_date: dt.date, pitcher_id: str) -> dict | None:
    season_snaps = pitcher_cache.get(str(season), {})
    best = None
    for date_str, snap in season_snaps.items():
        snap_date = dt.date.fromisoformat(date_str)
        if snap_date >= game_date:
            continue  # strictly before -- no leakage
        if best is None or snap_date > best[0]:
            best = (snap_date, snap)
    if best is None:
        return None
    return best[1].get(pitcher_id)


def main():
    games = json.loads(SCHEDULE_PATH.read_text())
    pitcher_cache = json.loads(PITCHER_CACHE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "R" and g["season"] < 2026]
    games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))

    state = EloState()
    rows = []  # (season, elo_diff, era_diff, outcome, run_margin)
    skipped_no_snapshot = skipped_low_ip = skipped_no_pid = 0

    for g in games:
        home_field_adv = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        home_r = state.get(g["home_team"])
        away_r = state.get(g["away_team"])
        elo_diff = (home_r + home_field_adv) - away_r
        predict_and_update(state, g)  # walk forward regardless of whether this game qualifies below

        if g.get("home_score") is None or g.get("away_score") is None or g["home_score"] == g["away_score"]:
            continue

        home_pid, away_pid = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if not home_pid or not away_pid:
            skipped_no_pid += 1
            continue

        game_date = dt.date.fromisoformat(g["gameday"])
        home_snap = _snapshot_for(pitcher_cache, g["season"], game_date, str(home_pid))
        away_snap = _snapshot_for(pitcher_cache, g["season"], game_date, str(away_pid))
        if home_snap is None or away_snap is None:
            skipped_no_snapshot += 1
            continue
        if home_snap["ip"] < MIN_IP or away_snap["ip"] < MIN_IP:
            skipped_low_ip += 1
            continue

        home_era = min(home_snap["era"], LEAGUE_AVG_ERA * 3)  # cap absurd small-sample outliers, don't discard
        away_era = min(away_snap["era"], LEAGUE_AVG_ERA * 3)
        era_diff = away_era - home_era  # positive = home starter has the BETTER (lower) ERA

        outcome = 1.0 if g["home_score"] > g["away_score"] else 0.0
        margin = g["home_score"] - g["away_score"]
        rows.append((g["season"], elo_diff, era_diff, outcome, margin))

    print(f"Qualifying games: {len(rows)}")
    print(f"Skipped -- no probable-pitcher id: {skipped_no_pid}")
    print(f"Skipped -- no prior in-season snapshot: {skipped_no_snapshot}")
    print(f"Skipped -- under {MIN_IP} IP for one/both starters: {skipped_low_ip}")
    print()

    seasons = sorted({r[0] for r in rows})
    elo_diffs = np.array([r[1] for r in rows])
    era_diffs = np.array([r[2] for r in rows])
    outcomes = np.array([r[3] for r in rows])
    margins = np.array([r[4] for r in rows])

    print(f"Raw correlation, era_diff vs outcome: {np.corrcoef(era_diffs, outcomes)[0, 1]:.4f}")
    print(f"Raw correlation, era_diff vs run margin: {np.corrcoef(era_diffs, margins)[0, 1]:.4f}")
    print(f"Raw correlation, era_diff vs elo_diff (redundancy check): {np.corrcoef(era_diffs, elo_diffs)[0, 1]:.4f}")
    print()

    print(f"{'Season':<8}{'coef_elo':>12}{'coef_era':>12}{'n':>8}")
    coefs_era = []
    for season in seasons:
        mask = np.array([r[0] == season for r in rows])
        X = np.column_stack([elo_diffs[mask], era_diffs[mask]])
        y = outcomes[mask]
        Xs = StandardScaler().fit_transform(X)
        clf = LogisticRegression()
        clf.fit(Xs, y)
        coefs_era.append(clf.coef_[0][1])
        print(f"{season:<8}{clf.coef_[0][0]:>12.4f}{clf.coef_[0][1]:>12.4f}{mask.sum():>8}")

    coefs_era = np.array(coefs_era)
    print()
    print(f"era_diff coef: mean={coefs_era.mean():.4f}  std={coefs_era.std():.4f}  "
          f"positive in {(coefs_era > 0).sum()}/{len(coefs_era)} seasons")


if __name__ == "__main__":
    main()
