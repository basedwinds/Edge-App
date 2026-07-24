"""Derives real constants for the F5 (first-5-innings, 3-way incl. tie) and
RFI (run-in-1st-inning, binary) probability models -- new MLB markets, not a
re-validation of an existing signal, so this both DERIVES the constants (F5
margin slope/std, mirroring game_lines_mlb.py's own MARGIN_SLOPE/MARGIN_STD
derivation) and CHECKS whether RFI has any real structural signal beyond a
flat league rate, in one script.

Walks the FULL 2016-2025 schedule (data/mlb_schedule_cache.json) for Elo
purposes (so ratings are properly warmed up, not starting fresh mid-history),
but only collects (elo_diff, f5_margin, rfi) rows for games with cached
linescore data (data/mlb_linescore_cache.json, 2021-2026 -- see
build_mlb_linescore_cache.py's own scoping note for why a smaller-but-real
window was used for these brand-new markets).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime as dt  # noqa: E402
import json  # noqa: E402

import numpy as np  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from app.models import game_lines_mlb as G  # noqa: E402
from app.models.baseline.elo_mlb import EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update  # noqa: E402
from app.models.pitcher_ratings_mlb import MIN_IP, pitcher_elo_adjustment  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
LINESCORE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_linescore_cache.json"
PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"


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
    linescores = json.loads(LINESCORE_CACHE_PATH.read_text())
    pitcher_cache = json.loads(PITCHER_CACHE_PATH.read_text())
    all_games = [g for g in games if g["game_type"] == "R" and g["season"] < 2027]
    all_games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))
    print(f"{len(all_games)} total REG games walked for Elo; {len(linescores)} have cached linescores")

    state = EloState()
    rows = []  # (elo_diff, f5_margin, rfi, expected_total, combined_era)

    for g in all_games:
        home_field_adv = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        home_r = state.get(g["home_team"])
        away_r = state.get(g["away_team"])

        pitcher_adj = 0.0
        combined_era = None
        home_pid, away_pid = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if home_pid and away_pid:
            game_date = dt.date.fromisoformat(g["gameday"])
            home_snap = _snapshot_for(pitcher_cache, g["season"], game_date, str(home_pid))
            away_snap = _snapshot_for(pitcher_cache, g["season"], game_date, str(away_pid))
            if home_snap and away_snap and home_snap["ip"] >= MIN_IP and away_snap["ip"] >= MIN_IP:
                pitcher_adj = pitcher_elo_adjustment(home_snap["era"], away_snap["era"], home_snap["ip"], away_snap["ip"])
                combined_era = (home_snap["era"] + away_snap["era"]) / 2.0

        elo_diff = (home_r + pitcher_adj + home_field_adv) - away_r
        predict_and_update(state, g)  # walk forward regardless

        ls = linescores.get(g["id"])
        if ls is not None:
            f5_margin = ls["home_f5_runs"] - ls["away_f5_runs"]
            expected_total = G.expected_total(g["home_team"])  # park-factor-adjusted, real signal already validated for full-game totals
            rows.append((elo_diff, f5_margin, ls["rfi"], expected_total, combined_era))

    print(f"Collected rows with real F5/RFI data: {len(rows)}")
    print()

    elo_diffs = np.array([r[0] for r in rows])
    f5_margins = np.array([r[1] for r in rows])
    rfis = np.array([1.0 if r[2] else 0.0 for r in rows])

    # --- F5 margin regression, through the origin, same convention as
    # game_lines_mlb.py's own MARGIN_SLOPE/MARGIN_STD ---
    f5_slope = float(np.sum(elo_diffs * f5_margins) / np.sum(elo_diffs * elo_diffs))
    resid = f5_margins - f5_slope * elo_diffs
    f5_std = float(np.std(resid))
    r = float(np.corrcoef(elo_diffs, f5_margins)[0, 1])
    print(f"F5_MARGIN_SLOPE = {f5_slope:.6f}")
    print(f"F5_MARGIN_STD = {f5_std:.4f}")
    print(f"Correlation elo_diff vs f5_margin: r={r:.4f}  (full-game MARGIN correlation was r=0.186, for comparison)")
    print()

    # Empirical F5 tie rate vs. what a Normal(mu, std) continuity-corrected
    # model would predict, as a sanity check on the modeling approach itself.
    real_tie_rate = float(np.mean(f5_margins == 0))
    from scipy.stats import norm
    model_tie_probs = norm.cdf(0.5, f5_slope * elo_diffs, f5_std) - norm.cdf(-0.5, f5_slope * elo_diffs, f5_std)
    print(f"Real F5 tie rate: {real_tie_rate:.4f}  |  Model's own average predicted tie prob: {model_tie_probs.mean():.4f}")
    print()

    # --- RFI: does overall SCORING LEVEL (not who wins) predict RFI? Two
    # real candidates, not |elo_diff| (a mismatch-magnitude signal that has
    # no principled reason to move 1st-inning scoring either direction):
    #   (a) park-factor-adjusted expected_total -- already a validated real
    #       signal for full-game totals, natural candidate for RFI too.
    #   (b) combined (avg) starting-pitcher ERA -- weaker starters, more
    #       likely to allow an early run before settling in.
    league_rfi_rate = float(np.mean(rfis))
    print(f"League-average RFI rate: {league_rfi_rate:.4f}  (n={len(rfis)})")

    expected_totals = np.array([r[3] for r in rows])
    print(f"Raw correlation, expected_total (park-adjusted) vs RFI: {np.corrcoef(expected_totals, rfis)[0, 1]:.4f}")
    Xs_total = StandardScaler().fit_transform(expected_totals.reshape(-1, 1))
    clf_total = LogisticRegression().fit(Xs_total, rfis)
    p_total = clf_total.predict_proba(Xs_total)[:, 1]
    print(f"Logistic coef, expected_total -> RFI: {clf_total.coef_[0][0]:.4f}  "
          f"(predicted RFI prob range: [{p_total.min():.4f}, {p_total.max():.4f}])")
    print()

    era_mask = np.array([r[4] is not None for r in rows])
    combined_eras = np.array([r[4] for r in rows if r[4] is not None])
    rfis_era = rfis[era_mask]
    print(f"Combined-ERA-available subset: n={era_mask.sum()} ({era_mask.sum() / len(rows) * 100:.1f}% of rows)")
    print(f"Raw correlation, combined_era vs RFI: {np.corrcoef(combined_eras, rfis_era)[0, 1]:.4f}")
    Xs_era = StandardScaler().fit_transform(combined_eras.reshape(-1, 1))
    clf_era = LogisticRegression().fit(Xs_era, rfis_era)
    p_era = clf_era.predict_proba(Xs_era)[:, 1]
    print(f"Logistic coef, combined_era -> RFI: {clf_era.coef_[0][0]:.4f}  "
          f"(predicted RFI prob range: [{p_era.min():.4f}, {p_era.max():.4f}])")
    print()

    # Half-season split sign-consistency check on the STRONGER of the two
    # candidates (same reasoning as the bullpen-fatigue check -- not enough
    # distinct seasons cached for a full per-season check).
    use_era = abs(np.corrcoef(combined_eras, rfis_era)[0, 1]) > abs(np.corrcoef(expected_totals, rfis)[0, 1])
    X_check, y_check, label = (Xs_era, rfis_era, "combined_era") if use_era else (Xs_total, rfis, "expected_total")
    n = len(y_check)
    mid = n // 2
    clf1 = LogisticRegression().fit(X_check[:mid], y_check[:mid])
    clf2 = LogisticRegression().fit(X_check[mid:], y_check[mid:])
    clf_full = LogisticRegression().fit(X_check, y_check)
    print(f"Stronger candidate: {label}")
    print(f"First-half coef: {clf1.coef_[0][0]:.4f}  |  Second-half coef: {clf2.coef_[0][0]:.4f}  |  "
          f"same sign: {(clf1.coef_[0][0] > 0) == (clf2.coef_[0][0] > 0) == (clf_full.coef_[0][0] > 0)}")


if __name__ == "__main__":
    main()
