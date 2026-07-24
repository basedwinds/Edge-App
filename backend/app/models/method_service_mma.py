"""In-process cache of the UFC method-of-finish model (KO/TKO vs
Submission vs Decision) -- parallel to distance_service_mma.py. Extends
the went-the-distance work with method-specific finish/loss rates.

**Real, validated signal**: Brier beats a naive base-rate baseline in
17/17 yearly walk-forward folds (accuracy 50.5% vs. 48.4%, weaker but still
positive on 12/17 folds -- Brier is the more complete metric for a 3-way
probabilistic prediction, since it credits the full distribution, not just
the top pick) -- see scripts/backtest_mma_method.py's docstring for the
full validation. Still model_validated: false per the standing rule --
"beats an internal accuracy baseline" is not the same claim as "beats the
market," untested here (no historical UFC method-of-finish odds archive
exists).

**weight_class added 2026-07-18** (heavier divisions finish more often, a
well-documented power effect) after scripts/check_mma_round2_signals.py
validated it as a REAL, separate improvement: Brier 0.6048 -> 0.6001, 17/17
yearly folds. Uses a `ColumnTransformer` (numeric StandardScaler +
`OneHotEncoder(handle_unknown="ignore")` for weight_class, over
mma_features.py's `normalize_weight_class()` -- collapses ufcstats' 124
raw fragmented weight-class strings, mostly "UFC "/"Interim " prefix
duplicates of the same real division, into 13 clean categories) rather
than `pd.get_dummies` -- see scripts/backtest_mma_method.py's docstring
for the real walk-forward instability the naive global-dummy approach
caused before this fix. Does NOT transfer to the rounds model (checked
separately, made it slightly worse there, not added).

Fits ONE final multinomial pipeline on ALL available historical data each
time refresh_model() runs, same pattern as distance_service_mma.py.
"""
import logging

import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.ingestion import ufc_data
from app.models import mma_features

log = logging.getLogger("method_service_mma")

_cache: dict = {"pipeline": None, "medians": None, "classes": None, "snapshots": None}


def refresh_model():
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    feature_rows = mma_features.build_feature_rows(fights, raw, bios)

    rows = []
    for r in feature_rows:
        if r["method_bucket"] is None:
            continue
        row = mma_features.to_symmetric_method_features(r)
        row["method"] = r["method_bucket"]
        rows.append(row)

    df = pd.DataFrame(rows).dropna(subset=["scheduled_rounds"])

    numeric_cols = mma_features.METHOD_MODEL_NUMERIC_FEATURES
    medians = df[numeric_cols].median()
    df[numeric_cols] = df[numeric_cols].fillna(medians)

    pre = ColumnTransformer([
        ("num", StandardScaler(), numeric_cols),
        ("cat", OneHotEncoder(handle_unknown="ignore"), ["weight_class"]),
    ])
    pipeline = make_pipeline(pre, LogisticRegression(C=0.5, max_iter=2000))
    pipeline.fit(df[numeric_cols + ["weight_class"]], df["method"])

    # Reuses the SAME rolling-state pass as distance_service_mma.py -- both
    # services independently call compute_current_snapshots (cheap, ~0.2s,
    # not worth sharing a cache across two otherwise-independent services).
    snapshots = mma_features.compute_current_snapshots(fights, raw)

    _cache["pipeline"] = pipeline
    _cache["medians"] = medians
    _cache["classes"] = list(pipeline.classes_)
    _cache["snapshots"] = snapshots
    log.info("mma method-of-finish model refreshed: %d training rows, classes=%s", len(df), _cache["classes"])


def predict_method(
    fighter_a_id: str, fighter_b_id: str, weight_class: str | None, scheduled_rounds: int | None,
) -> dict[str, float] | None:
    """Returns {"kotko": p, "submission": p, "decision": p} or None if the
    model/scheduled_rounds isn't available yet. Probabilities sum to 1.0 --
    does NOT include a "draw" outcome (see mma_features.py's
    to_symmetric_method_features docstring; draws are rare and the
    validated feature set doesn't model them separately)."""
    pipeline = _cache.get("pipeline")
    snapshots = _cache.get("snapshots")
    if pipeline is None or snapshots is None or scheduled_rounds is None:
        return None

    a_snap = snapshots.get(fighter_a_id, {})
    b_snap = snapshots.get(fighter_b_id, {})
    feature_row = {
        "a_ko_win_rate": a_snap.get("ko_win_rate"), "b_ko_win_rate": b_snap.get("ko_win_rate"),
        "a_sub_win_rate": a_snap.get("sub_win_rate"), "b_sub_win_rate": b_snap.get("sub_win_rate"),
        "a_ko_loss_rate": a_snap.get("ko_loss_rate"), "b_ko_loss_rate": b_snap.get("ko_loss_rate"),
        "a_sub_loss_rate": a_snap.get("sub_loss_rate"), "b_sub_loss_rate": b_snap.get("sub_loss_rate"),
        "a_experience": a_snap.get("experience"), "b_experience": b_snap.get("experience"),
        "scheduled_rounds": scheduled_rounds,
        "weight_class": weight_class or "Unknown",
    }
    symmetric = mma_features.to_symmetric_method_features(feature_row)

    medians = _cache["medians"]
    numeric_cols = mma_features.METHOD_MODEL_NUMERIC_FEATURES
    row = {col: (symmetric[col] if symmetric[col] is not None else medians[col]) for col in numeric_cols}
    row["weight_class"] = symmetric["weight_class"]

    X = pd.DataFrame([row])[numeric_cols + ["weight_class"]]
    proba = _cache["pipeline"].predict_proba(X)[0]
    return {cls: round(float(p), 4) for cls, p in zip(_cache["classes"], proba)}
