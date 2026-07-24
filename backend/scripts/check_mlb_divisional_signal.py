"""Investigation script (not a registered backtest): does MLB have the same
"divisional squeeze" (moneyline, closer than a naive rating suggests) and/or
"divisional total suppression" (lower total runs) that NFL's real, validated
signals show (see game_lines.py/combine.py -- NFL's is measured at 1.5pts
lower total, 10% squeeze toward 50/50)? MLB's own moneyline model explicitly
calls `combine_probability(..., is_divisional=False)` with a comment flagging
this as never checked -- this closes that gap.

Two separate, real hypotheses, checked independently:
  1. SQUEEZE: for a fixed elo_diff bucket, do divisional games have a lower
     realized win-rate GAP (|actual win rate - 0.5|) than non-divisional
     games at the same elo_diff? (Mirrors "division rivals know each other,
     records even out" reasoning.)
  2. TOTAL SUPPRESSION: do divisional games score fewer total runs than the
     park-factor-adjusted expectation, on average?
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402

import numpy as np  # noqa: E402

from app.data.mlb_divisions import is_divisional  # noqa: E402
from app.models import game_lines_mlb as G  # noqa: E402
from app.models.baseline.elo_mlb import EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, predict_and_update, win_prob  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.pitcher_ratings_mlb import MIN_IP, pitcher_elo_adjustment  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
PITCHER_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_pitcher_snapshot_cache.json"


def _snapshot_for(pitcher_cache, season, game_date, pid):
    import datetime as dt
    best = None
    for date_str, snap in pitcher_cache.get(str(season), {}).items():
        snap_date = dt.date.fromisoformat(date_str)
        if snap_date >= game_date:
            continue
        if best is None or snap_date > best[0]:
            best = (snap_date, snap)
    return best[1].get(pid) if best else None


def main():
    import datetime as dt

    games = json.loads(SCHEDULE_PATH.read_text())
    pitcher_cache = json.loads(PITCHER_CACHE_PATH.read_text())
    all_games = [g for g in games if g["game_type"] == "R" and g["season"] < 2027]
    all_games.sort(key=lambda g: (g["season"], g["gameday"], g["game_number"], g["id"]))

    state = EloState()
    rows = []  # (elo_diff, p_elo, outcome, margin, actual_total, expected_total, divisional)
    for g in all_games:
        hfa = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
        hr = state.get(g["home_team"])
        ar = state.get(g["away_team"])
        padj = 0.0
        hp, ap = g.get("home_probable_pitcher_id"), g.get("away_probable_pitcher_id")
        if hp and ap:
            gd = dt.date.fromisoformat(g["gameday"])
            hs = _snapshot_for(pitcher_cache, g["season"], gd, str(hp))
            as_ = _snapshot_for(pitcher_cache, g["season"], gd, str(ap))
            if hs and as_ and hs["ip"] >= MIN_IP and as_["ip"] >= MIN_IP:
                padj = pitcher_elo_adjustment(hs["era"], as_["era"], hs["ip"], as_["ip"])
        elo_diff = (hr + padj + hfa) - ar
        p_elo = win_prob(hr + padj + hfa, ar, home_field_adv=0.0)
        div = is_divisional(g["home_team"], g["away_team"])

        predict_and_update(state, g)

        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        outcome = 1.0 if g["home_score"] > g["away_score"] else (0.0 if g["home_score"] < g["away_score"] else 0.5)
        actual_total = g["home_score"] + g["away_score"]
        expected_total = G.expected_total(g["home_team"])
        rows.append((elo_diff, p_elo, outcome, actual_total, expected_total, div))

    div_rows = [r for r in rows if r[5]]
    nondiv_rows = [r for r in rows if not r[5]]
    print(f"Total games: {len(rows)}  (divisional: {len(div_rows)}, {len(div_rows)/len(rows)*100:.1f}%)")
    print()

    # --- 1. SQUEEZE check ---
    print("=== Moneyline squeeze check ===")
    p_elo_div = np.array([r[1] for r in div_rows])
    outcomes_div = np.array([r[2] for r in div_rows])
    p_elo_nondiv = np.array([r[1] for r in nondiv_rows])
    outcomes_nondiv = np.array([r[2] for r in nondiv_rows])
    print(f"Elo-only Brier, divisional games (n={len(div_rows)}): {brier_score(list(p_elo_div), list(outcomes_div)):.4f}")
    print(f"Elo-only Brier, non-divisional games (n={len(nondiv_rows)}): {brier_score(list(p_elo_nondiv), list(outcomes_nondiv)):.4f}")

    # Bucket by |elo_diff| (mismatch magnitude) and compare realized favorite win-rate
    # divisional vs non-divisional at similar mismatch levels.
    print()
    print("Favorite win-rate by |elo_diff| bucket, divisional vs non-divisional:")
    for lo, hi in [(0, 20), (20, 40), (40, 70), (70, 200)]:
        d = [(abs(r[1] - 0.5), r[2] if r[1] >= 0.5 else 1 - r[2]) for r in div_rows if lo <= abs(r[0]) < hi]
        nd = [(abs(r[1] - 0.5), r[2] if r[1] >= 0.5 else 1 - r[2]) for r in nondiv_rows if lo <= abs(r[0]) < hi]
        if len(d) < 30 or len(nd) < 30:
            continue
        fav_rate_d = np.mean([o for _, o in d])
        fav_rate_nd = np.mean([o for _, o in nd])
        print(f"  |elo_diff| {lo}-{hi}: divisional favorite-win-rate={fav_rate_d:.3f} (n={len(d)})  "
              f"non-divisional={fav_rate_nd:.3f} (n={len(nd)})")

    # --- 2. TOTAL SUPPRESSION check ---
    print()
    print("=== Total-runs suppression check ===")
    resid_div = np.array([r[3] - r[4] for r in div_rows])
    resid_nondiv = np.array([r[3] - r[4] for r in nondiv_rows])
    print(f"Avg residual (actual - park-adjusted expected), divisional: {resid_div.mean():+.4f} runs (n={len(resid_div)})")
    print(f"Avg residual, non-divisional: {resid_nondiv.mean():+.4f} runs (n={len(resid_nondiv)})")
    print(f"Difference (divisional - non-divisional): {resid_div.mean() - resid_nondiv.mean():+.4f} runs")

    # Simple two-sample t-test-style check (not scipy.stats.ttest to avoid an
    # extra import -- manual z-score against pooled SE is enough for a sanity read)
    se = np.sqrt(resid_div.var() / len(resid_div) + resid_nondiv.var() / len(resid_nondiv))
    z = (resid_div.mean() - resid_nondiv.mean()) / se
    print(f"Difference / pooled SE (rough z-score, |z|>2 ~ real): {z:.2f}")


if __name__ == "__main__":
    main()
