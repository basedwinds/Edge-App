"""In-process cache of the UFC round-of-finish model -- parallel to
distance_service_mma.py/method_service_mma.py. Extends both with a
per-round distribution (1..scheduled_rounds).

**Real, but weaker/noisier signal than distance or method-of-finish**: the
raw 5-way round target only beats a naive per-scheduled_rounds baseline in
13/17 yearly walk-forward Brier folds (vs. 17/17 for the other two MMA
models) and loses on accuracy most years. The market-relevant "ends before
round N" ladder question is more robust (10-15/17 yearly Brier folds
depending on rung, always net Brier-positive) since summing several
classes' probabilities partially cancels per-class error -- see
scripts/backtest_mma_rounds.py's docstring for the full validation. Still
model_validated: false per the standing rule.

Fits ONE final multinomial pipeline on ALL available historical data each
time refresh_model() runs, same pattern as the other two services.
"""
import logging

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.ingestion import ufc_data
from app.models import mma_features

log = logging.getLogger("rounds_service_mma")

_cache: dict = {"pipeline": None, "medians": None, "classes": None, "snapshots": None}


def refresh_model():
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    feature_rows = mma_features.build_feature_rows(fights, raw, bios)
    fight_round_by_id = {f["id"]: f.get("round") for f in fights}

    rows = []
    for r in feature_rows:
        round_of_finish = fight_round_by_id.get(r["fight_id"])
        if round_of_finish is None or r["scheduled_rounds"] is None:
            continue
        if round_of_finish > r["scheduled_rounds"]:
            continue
        row = mma_features.to_symmetric_rounds_features(r)
        row["round_of_finish"] = round_of_finish
        rows.append(row)

    df = pd.DataFrame(rows).dropna(subset=["scheduled_rounds"])

    feature_cols = mma_features.ROUNDS_MODEL_NUMERIC_FEATURES
    medians = df[feature_cols].median()
    df[feature_cols] = df[feature_cols].fillna(medians)

    pipeline = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))
    pipeline.fit(df[feature_cols], df["round_of_finish"])

    # Reuses the SAME rolling-state pass as distance_service_mma.py/
    # method_service_mma.py -- each service independently calls
    # compute_current_snapshots (cheap, ~0.2s, not worth sharing a cache
    # across three otherwise-independent services).
    snapshots = mma_features.compute_current_snapshots(fights, raw)

    _cache["pipeline"] = pipeline
    _cache["medians"] = medians
    _cache["classes"] = list(pipeline.classes_)
    _cache["snapshots"] = snapshots
    log.info("mma rounds model refreshed: %d training rows, classes=%s", len(df), _cache["classes"])


def _round_distribution(fighter_a_id: str, fighter_b_id: str, scheduled_rounds: int | None) -> dict[int, float] | None:
    pipeline = _cache.get("pipeline")
    snapshots = _cache.get("snapshots")
    if pipeline is None or snapshots is None or scheduled_rounds is None:
        return None

    a_snap = snapshots.get(fighter_a_id, {})
    b_snap = snapshots.get(fighter_b_id, {})
    feature_row = {
        "a_experience": a_snap.get("experience"), "b_experience": b_snap.get("experience"),
        "a_win_rate": a_snap.get("win_rate"), "b_win_rate": b_snap.get("win_rate"),
        "a_finish_rate": a_snap.get("finish_rate"), "b_finish_rate": b_snap.get("finish_rate"),
        "a_went_distance_rate": a_snap.get("went_distance_rate"), "b_went_distance_rate": b_snap.get("went_distance_rate"),
        "a_avg_sig_str_landed": a_snap.get("avg_sig_str_landed"), "b_avg_sig_str_landed": b_snap.get("avg_sig_str_landed"),
        "a_avg_td_landed": a_snap.get("avg_td_landed"), "b_avg_td_landed": b_snap.get("avg_td_landed"),
        "a_layoff_days": a_snap.get("layoff_days"), "b_layoff_days": b_snap.get("layoff_days"),
        "a_age": None, "b_age": None,  # live age comes from elo_service_mma, not tracked in this snapshot -- median-filled, same as an unknown debut fighter
        "a_reach_in": None, "b_reach_in": None,  # not in compute_current_snapshots' output -- median-filled
        "a_ko_win_rate": a_snap.get("ko_win_rate"), "b_ko_win_rate": b_snap.get("ko_win_rate"),
        "a_sub_win_rate": a_snap.get("sub_win_rate"), "b_sub_win_rate": b_snap.get("sub_win_rate"),
        "a_ko_loss_rate": a_snap.get("ko_loss_rate"), "b_ko_loss_rate": b_snap.get("ko_loss_rate"),
        "a_sub_loss_rate": a_snap.get("sub_loss_rate"), "b_sub_loss_rate": b_snap.get("sub_loss_rate"),
        "scheduled_rounds": scheduled_rounds,
        "is_title_bout": 1 if scheduled_rounds == 5 else 0,  # is_title_bout isn't tracked in this snapshot either -- 5-round non-title main events exist, so this is a rough proxy, not exact
        "weight_class": None,  # to_symmetric_distance_features reads this key directly -- dropped again below, ROUNDS_MODEL_NUMERIC_FEATURES doesn't include it
    }
    symmetric = mma_features.to_symmetric_rounds_features(feature_row)

    medians = _cache["medians"]
    feature_cols = mma_features.ROUNDS_MODEL_NUMERIC_FEATURES
    row = {col: (symmetric[col] if symmetric[col] is not None else medians[col]) for col in feature_cols}

    X = pd.DataFrame([row])[feature_cols]
    proba = _cache["pipeline"].predict_proba(X)[0]
    raw = {cls: float(p) for cls, p in zip(_cache["classes"], proba)}

    # A round beyond scheduled_rounds is a structural impossibility (the
    # fight literally cannot continue), not just improbable -- the
    # multinomial leaks a small amount of mass there (trained across ALL
    # scheduled_rounds together) since it's a soft classifier, not a hard
    # constraint. Clip and renormalize rather than leave that mass stranded.
    valid = {k: v for k, v in raw.items() if k <= scheduled_rounds}
    total = sum(valid.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in valid.items()}


# REAL BUG caught live via Recommended Bets (2026-07-18): Polymarket's
# lowest rounds rung is "O/U 0.5 Rounds" -- "does round 1 even happen" --
# which every fight satisfies once it starts (round_of_finish is always
# >= 1), so the model correctly returns 1.0 for P(round > 0.5). But the
# real market priced that rung at 51-67%, not ~100%, because it's pricing
# in something this model has ZERO information about: the chance the fight
# never happens as scheduled (withdrawal, weigh-in miss, injury) -- this
# app's training data is built ONLY from fights that actually occurred.
# That mismatch produced a huge, entirely artifactual "edge" that dominated
# Recommended Bets (7 of the top 12 rows were this exact degenerate rung).
# A "does the fight occur" question is genuinely out of scope for a
# round-of-finish model trained on completed fights -- not a real edge, so
# it's excluded rather than surfaced as a false-confidence 100%.
_MIN_MEANINGFUL_ROUND_THRESHOLD = 1.0


def predict_ends_before_round(fighter_a_id: str, fighter_b_id: str, scheduled_rounds: int | None, before_round: float) -> float | None:
    """P(round_of_finish < before_round) -- Kalshi's own "ends before round
    N?" phrasing, N always an integer there."""
    if before_round <= _MIN_MEANINGFUL_ROUND_THRESHOLD:
        return None
    dist = _round_distribution(fighter_a_id, fighter_b_id, scheduled_rounds)
    if dist is None:
        return None
    return round(sum(p for k, p in dist.items() if k < before_round), 4)


def predict_over_rounds(fighter_a_id: str, fighter_b_id: str, scheduled_rounds: int | None, line: float) -> float | None:
    """P(round_of_finish > line) -- Polymarket's O/U {N}.5 phrasing (line
    is always a half-integer there, so this is never an exact-equality
    edge case)."""
    if line <= _MIN_MEANINGFUL_ROUND_THRESHOLD:
        return None
    dist = _round_distribution(fighter_a_id, fighter_b_id, scheduled_rounds)
    if dist is None:
        return None
    return round(sum(p for k, p in dist.items() if k > line), 4)
