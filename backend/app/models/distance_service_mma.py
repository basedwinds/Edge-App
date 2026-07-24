"""In-process cache of the UFC went-the-distance model -- parallel to
elo_service_mma.py, but for this app's flagship differentiator market (per
a SEPARATE, standalone research project's earlier finding, re-tested fresh
here in this app's own harness -- see scripts/backtest_mma_distance.py's
docstring for the full validation).

**Real, validated signal: +7.08pp accuracy over a naive base-rate baseline,
95% bootstrap CI [+5.54pp, +8.67pp] (excludes zero), won 17/17 yearly
walk-forward folds (2010-2026).** This independently confirms (different
feature set, this app's own harness, not ported) the earlier project's
+7.4pp / CI [+4.9,+10.0] / 7-for-7 finding. Still model_validated: false in
this app per the standing rule -- "beats an internal accuracy baseline"
is not the same claim as "beats the market," which hasn't been tested here
yet (no historical UFC go-the-distance odds archive exists to backtest
against, same gap the earlier project also had to work around via live
paper-trading instead).

Fits ONE final pipeline on ALL available historical data (not held-out
walk-forward folds -- those are for validation only, see the backtest
script) each time refresh_model() runs. Feature set is the sport-agnostic
symmetric combination logic in mma_features.py, the SAME function the
backtest script uses, so live serving can't silently drift from what was
actually validated.
"""
import datetime as dt
import logging

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.ingestion import ufc_data
from app.models import mma_features

log = logging.getLogger("distance_service_mma")

_cache: dict = {
    "pipeline": None, "medians": None, "feature_cols": None,
    "snapshots": None, "bios": None,
}


def refresh_model():
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    feature_rows = mma_features.build_feature_rows(fights, raw, bios)

    rows = []
    for r in feature_rows:
        row = mma_features.to_symmetric_distance_features(r)
        row["went_the_distance"] = r["went_the_distance"]
        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["scheduled_rounds"])

    numeric_cols = mma_features.DISTANCE_MODEL_NUMERIC_FEATURES
    medians = df[numeric_cols].median()
    df[numeric_cols] = df[numeric_cols].fillna(medians)

    weight_dummies = pd.get_dummies(df["weight_class"], prefix="wc")
    df = pd.concat([df, weight_dummies], axis=1)
    feature_cols = numeric_cols + list(weight_dummies.columns)

    pipeline = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))
    pipeline.fit(df[feature_cols], df["went_the_distance"])

    # Current (as-of-today) rolling snapshot per fighter -- computed ONCE
    # here and reused for every live prediction below, rather than
    # recomputing from the full 8,780-fight history on every call.
    snapshots = mma_features.compute_current_snapshots(fights, raw, as_of=dt.date.today())

    _cache["pipeline"] = pipeline
    _cache["medians"] = medians
    _cache["feature_cols"] = feature_cols
    _cache["snapshots"] = snapshots
    _cache["bios"] = bios
    log.info("mma distance model refreshed: %d training rows, %d fighters snapshotted", len(df), len(snapshots))


def predict_went_distance(
    fighter_a_id: str, fighter_b_id: str, weight_class: str | None,
    scheduled_rounds: int | None, is_title_bout: bool,
) -> float | None:
    pipeline = _cache.get("pipeline")
    snapshots = _cache.get("snapshots")
    if pipeline is None or snapshots is None or scheduled_rounds is None:
        return None

    a_snap = snapshots.get(fighter_a_id, {})
    b_snap = snapshots.get(fighter_b_id, {})
    bios = _cache.get("bios") or {}
    a_bio, b_bio = bios.get(fighter_a_id, {}), bios.get(fighter_b_id, {})
    today = dt.date.today()
    a_dob, b_dob = mma_features.parse_dob(a_bio.get("dob")), mma_features.parse_dob(b_bio.get("dob"))
    a_age = (today - a_dob).days / 365.25 if a_dob else None
    b_age = (today - b_dob).days / 365.25 if b_dob else None

    feature_row = {
        "a_experience": a_snap.get("experience"), "b_experience": b_snap.get("experience"),
        "a_win_rate": a_snap.get("win_rate"), "b_win_rate": b_snap.get("win_rate"),
        "a_finish_rate": a_snap.get("finish_rate"), "b_finish_rate": b_snap.get("finish_rate"),
        "a_went_distance_rate": a_snap.get("went_distance_rate"), "b_went_distance_rate": b_snap.get("went_distance_rate"),
        "a_avg_sig_str_landed": a_snap.get("avg_sig_str_landed"), "b_avg_sig_str_landed": b_snap.get("avg_sig_str_landed"),
        "a_avg_td_landed": a_snap.get("avg_td_landed"), "b_avg_td_landed": b_snap.get("avg_td_landed"),
        "a_layoff_days": a_snap.get("layoff_days"), "b_layoff_days": b_snap.get("layoff_days"),
        "a_age": a_age, "b_age": b_age,
        "a_reach_in": mma_features.parse_reach_inches(a_bio.get("reach")),
        "b_reach_in": mma_features.parse_reach_inches(b_bio.get("reach")),
        "scheduled_rounds": scheduled_rounds,
        "is_title_bout": 1 if is_title_bout else 0,
        "weight_class": weight_class or "Unknown",
    }
    symmetric = mma_features.to_symmetric_distance_features(feature_row)

    medians = _cache["medians"]
    numeric_cols = mma_features.DISTANCE_MODEL_NUMERIC_FEATURES
    row = {col: (symmetric[col] if symmetric[col] is not None else medians[col]) for col in numeric_cols}
    for col in _cache["feature_cols"]:
        if col.startswith("wc_"):
            row[col] = 1 if col == f"wc_{symmetric['weight_class']}" else 0

    X = pd.DataFrame([row])[_cache["feature_cols"]]
    p = pipeline.predict_proba(X)[0, 1]
    return round(float(p), 4)
