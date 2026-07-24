"""Free per-defender career-quality signal for DPOY, extending the same
career-stat-from-cached-PBP pattern as qb_ratings.py/skill_position_ratings.py
to defense. Unlike those two (which use EPA, a clean per-play efficiency
metric), defense has no equivalent clean single-number attribution in this
data -- sacks/INTs/TFLs/forced fumbles are the standard defensive counting
stats DPOY voting actually weighs, so this uses a rough WEIGHTED COUNTING
score instead of a rate stat. Weights are hand-picked to roughly reflect
each event's rarity/impact (INTs and forced fumbles are rarer and more
game-changing than tackles for loss), same "rough, auditable constant"
spirit as this project's other hand-picked weights (e.g. injury_rules.py's
POSITION_WEIGHTS_PP) -- NOT derived from a DPOY-voting regression, honestly
flagged as such.

Career total (not per-game rate) is used deliberately: DPOY voting rewards
cumulative single-season production, and a rate stat would over-favor a
player with very few career snaps who got lucky on a handful of plays.
"""
import datetime
import glob
import os

import pandas as pd

from app.models.qb_ratings import _canonical_key

SACK_WEIGHT = 2.0
INTERCEPTION_WEIGHT = 3.0
FORCED_FUMBLE_WEIGHT = 2.5
TACKLE_FOR_LOSS_WEIGHT = 1.0

MIN_CAREER_EVENTS_TO_RATE = 5  # below this, treat as unrated -- too little signal to trust

CACHE_TTL = datetime.timedelta(hours=24)
_cache: dict = {"fetched_at": None, "scores": None}


def _cache_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "pbp_cache")


def _tally(pbp: pd.DataFrame, cols: list[str], weight: float, tally: dict[str, float]):
    for col in cols:
        if col not in pbp.columns:
            continue
        counts = pbp[col].dropna().apply(_canonical_key).value_counts()
        for key, n in counts.items():
            if key:
                tally[key] = tally.get(key, 0.0) + n * weight


def compute_defensive_career_scores() -> dict[str, float]:
    """Returns {canonical_key: weighted_career_score}. No per-player minimum-
    snap gating like the offensive ratings (defense has no clean "opportunity"
    denominator the way dropbacks/targets/carries are for offense) -- instead
    gated by MIN_CAREER_EVENTS_TO_RATE on the tallied weighted total itself."""
    files = sorted(glob.glob(os.path.join(_cache_dir(), "pbp_*.parquet")))
    if not files:
        return {}

    cols = [
        "sack_player_name", "half_sack_1_player_name", "half_sack_2_player_name",
        "interception_player_name",
        "forced_fumble_player_1_player_name", "forced_fumble_player_2_player_name",
        "tackle_for_loss_1_player_name", "tackle_for_loss_2_player_name",
    ]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except Exception:
            continue
    if not frames:
        return {}
    pbp = pd.concat(frames, ignore_index=True)

    tally: dict[str, float] = {}
    _tally(pbp, ["sack_player_name", "half_sack_1_player_name", "half_sack_2_player_name"], SACK_WEIGHT, tally)
    _tally(pbp, ["interception_player_name"], INTERCEPTION_WEIGHT, tally)
    _tally(pbp, ["forced_fumble_player_1_player_name", "forced_fumble_player_2_player_name"], FORCED_FUMBLE_WEIGHT, tally)
    _tally(pbp, ["tackle_for_loss_1_player_name", "tackle_for_loss_2_player_name"], TACKLE_FOR_LOSS_WEIGHT, tally)

    return {k: v for k, v in tally.items() if v >= MIN_CAREER_EVENTS_TO_RATE}


def get_defensive_career_scores() -> dict[str, float]:
    now = datetime.datetime.utcnow()
    if _cache["scores"] is not None and _cache["fetched_at"] is not None and now - _cache["fetched_at"] < CACHE_TTL:
        return _cache["scores"]
    scores = compute_defensive_career_scores()
    _cache.update(fetched_at=now, scores=scores)
    return scores
