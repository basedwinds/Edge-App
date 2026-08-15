"""Derive the K-BB% -> Elo conversion for the MLB moneyline pitcher blend (#163).

WHY SWAP AT ALL. check_mlb_pitcher_metric already settled the metric question on
15,352 walk-forward games: K-BB% carries more signal than the incumbent ERA and,
crucially, generalises better out of sample --

    correlation vs outcome    era 0.0890   fip 0.1047   kbb 0.1096
    walk-forward log-loss     elo-only 0.67877
                              +era 0.67800  (beats elo-only in 6/9 seasons)
                              +fip 0.67674  (7/9)
                              +kbb 0.67657  (8/9)

That is the "prefer the metric closest to what the competitor controls" frame
that has now paid three times in this app (ERA->K-BB% for totals in #199,
goals->xG for soccer in #167, and here).

WHAT WAS ACTUALLY MISSING was never the evidence -- it was the CONSTANT. The
shipped ERA path converts a metric difference into Elo points via
ERA_DIFF_TO_ELO_POINTS = 9.73, and K-BB% is on a completely different scale
(0-0.35 rather than 0-9) and opposite sign convention. Shipping the swap without
its own conversion would silently rescale the entire pitcher adjustment.

A DOCUMENTATION DRIFT FOUND ON THE WAY, worth recording rather than papering
over: ERA_DIFF_TO_ELO_POINTS' comment cites "raw, non-standardized units -- see
check_mlb_pitcher_signal.py", but that script (and check_mlb_pitcher_metric)
both fit on StandardScaler-transformed features and per-season rather than
pooled. So 9.73 is NOT reproducible from either script as they stand. Rather
than guess at the original basis, this derives BOTH metrics in ONE harness and
ships K-BB% SCALED OFF THE INCUMBENT:

    KBB_DIFF_TO_ELO_POINTS = ERA_DIFF_TO_ELO_POINTS * (ratio_kbb / ratio_era)

That is robust to whatever basis produced 9.73, because any constant factor in
the harness cancels in the ratio. It also preserves the incumbent's calibration
-- if 9.73 was conservative or aggressive, the swap inherits the same stance
instead of silently changing it.

ERA IS RE-DERIVED HERE AS A CONTROL, not quoted. Its raw-units ratio is printed
next to 9.73 so the reader can see how far the harness sits from the shipped
basis before trusting the K-BB number that comes out of the same harness.

Run: backend/.venv/Scripts/python.exe scripts/derive_mlb_pitcher_kbb_constant.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.pitcher_ratings_mlb import (  # noqa: E402
    ERA_DIFF_TO_ELO_POINTS, ERA_DIFF_TO_ELO_POINTS_PRIOR_SEASON,
    MAX_PITCHER_ELO_POINTS,
)
from scripts.check_mlb_pitcher_metric import build_rows  # noqa: E402


def pooled_raw_ratio(elo, metric, y):
    """coef_metric / coef_elo from ONE pooled logistic in RAW units.

    Raw, not standardised: the conversion has to answer "how many Elo points is
    one unit of this metric worth", and a standardised coefficient answers a
    different question (how many SDs). Pooled, not per-season, because the
    constant is a single number applied to every game."""
    X = np.column_stack([elo, metric])
    clf = LogisticRegression(max_iter=2000, C=1e6).fit(X, y)   # C large => ~unpenalised
    c_elo, c_metric = clf.coef_[0]
    return c_metric / c_elo, c_elo, c_metric


def main() -> None:
    rows, _mean_fip, _mean_era, skipped = build_rows()
    arr = {n: np.array([r[i] for r in rows]) for i, n in enumerate(
        ["season", "elo", "era", "fip", "kbb", "outcome", "margin"])}
    y = arr["outcome"]
    print(f"games: {len(rows)}   skipped: {skipped}")
    print("(identical qualification/snapshot/diff conventions as "
          "check_mlb_pitcher_metric -- build_rows is shared, not reimplemented)")

    print(f"\nPOOLED RAW-UNITS LOGISTIC  outcome ~ elo_diff + metric_diff")
    print(f"{'metric':<8}{'coef_elo':>12}{'coef_metric':>14}{'Elo pts per unit':>19}")
    out = {}
    for m in ("era", "kbb"):
        ratio, c_elo, c_m = pooled_raw_ratio(arr["elo"], arr[m], y)
        out[m] = ratio
        print(f"{m:<8}{c_elo:>12.6f}{c_m:>14.6f}{ratio:>19.4f}")

    print(f"\nCONTROL -- does the harness reproduce the shipped ERA constant?")
    print(f"  shipped ERA_DIFF_TO_ELO_POINTS = {ERA_DIFF_TO_ELO_POINTS}")
    print(f"  this harness, ERA              = {out['era']:.4f}")
    drift = out["era"] / ERA_DIFF_TO_ELO_POINTS
    print(f"  harness / shipped              = {drift:.4f}")
    if 0.7 <= drift <= 1.4:
        print("  Close enough to treat the harness as the same basis.")
    else:
        print("  NOT the same basis -- which is exactly why the K-BB constant is")
        print("  derived as a RATIO to ERA below rather than used absolutely.")

    scaled = ERA_DIFF_TO_ELO_POINTS * (out["kbb"] / out["era"])
    scaled_prior = ERA_DIFF_TO_ELO_POINTS_PRIOR_SEASON * (out["kbb"] / out["era"])
    print(f"\nSCALED OFF THE INCUMBENT (basis-independent):")
    print(f"  ratio_kbb / ratio_era = {out['kbb'] / out['era']:.4f}")
    print(f"  KBB_DIFF_TO_ELO_POINTS               = {scaled:.2f}")
    print(f"  KBB_DIFF_TO_ELO_POINTS_PRIOR_SEASON  = {scaled_prior:.2f}")

    # What the swap does to a REAL matchup, so the magnitude is legible.
    print(f"\nSANITY -- Elo swing on realistic matchups (cap {MAX_PITCHER_ELO_POINTS}):")
    kbb_sd = float(np.std(arr["kbb"]))
    era_sd = float(np.std(arr["era"]))
    print(f"  1 sd of kbb_diff = {kbb_sd:.4f} -> {kbb_sd * scaled:+.1f} Elo pts")
    print(f"  1 sd of era_diff = {era_sd:.4f} -> {era_sd * ERA_DIFF_TO_ELO_POINTS:+.1f} Elo pts (incumbent)")
    print("  These should be COMPARABLE. A swap that changes the typical swing")
    print("  is rescaling the adjustment, not changing the metric -- and the")
    print("  out-of-sample win came from the metric.")
    for pct in (90, 99):
        k = float(np.percentile(np.abs(arr["kbb"]), pct))
        e = float(np.percentile(np.abs(arr["era"]), pct))
        print(f"  p{pct} |diff|: kbb {k * scaled:+.1f} pts   era {e * ERA_DIFF_TO_ELO_POINTS:+.1f} pts")


if __name__ == "__main__":
    main()
