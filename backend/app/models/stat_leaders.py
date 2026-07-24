"""Free career RAW counting-stat totals (not weighted composites like
defensive_ratings.py's DPOY score) -- one number per category, used
directly for the Kalshi "league leader" markets (KXLEADERNFL{PYDS,PTDS,
RYDS,RTDS,RUSHYDS,RUSHTDS,INT,SACKS} + KXLEADERNFLPINT), added 2026-07-16.

Deliberately a fresh, self-contained module rather than extending
defensive_ratings.py/skill_position_ratings.py -- those two compute a
single WEIGHTED EPA-or-counting composite per position group for award
scoring; a "league leader" market is about ONE raw stat category exactly
(passing yards, not "offensive quality"), so reusing those would mean
unpicking a composite back into its parts. All categories computed in ONE
pass over the cached PBP for efficiency, same "single scan, many outputs"
shape as epa_ratings.py.
"""
import datetime
import glob
import os

import pandas as pd

from app.models.qb_ratings import _canonical_key

CACHE_TTL = datetime.timedelta(hours=24)
_cache: dict = {"fetched_at": None, "totals": None}


def _cache_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "pbp_cache")


def _sum_by_player(pbp: pd.DataFrame, player_col: str, value_col: str) -> dict[str, float]:
    sub = pbp[pbp[player_col].notna() & pbp[value_col].notna()]
    totals = sub.groupby(player_col)[value_col].sum()
    out: dict[str, float] = {}
    for name, total in totals.items():
        key = _canonical_key(name)
        if key:
            out[key] = out.get(key, 0.0) + float(total)
    return out


def compute_stat_leader_totals() -> dict[str, dict[str, float]]:
    """Returns {category: {canonical_key: career_total}} for: pass_yds,
    pass_tds, pass_int (thrown BY the passer), rush_yds, rush_tds, rec_yds,
    rec_tds, def_int (made BY the defender), sacks."""
    files = sorted(glob.glob(os.path.join(_cache_dir(), "pbp_*.parquet")))
    if not files:
        return {}

    cols = [
        "pass_attempt", "rush_attempt", "complete_pass",
        "passer_player_name", "rusher_player_name", "receiver_player_name",
        "passing_yards", "rushing_yards", "receiving_yards",
        "pass_touchdown", "rush_touchdown", "interception",
        "interception_player_name",
        "sack_player_name", "half_sack_1_player_name", "half_sack_2_player_name",
    ]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=[c for c in cols if c not in ("half_sack_1_player_name", "half_sack_2_player_name")] + ["half_sack_1_player_name", "half_sack_2_player_name"]))
        except Exception:
            continue
    if not frames:
        return {}
    pbp = pd.concat(frames, ignore_index=True)

    pass_plays = pbp[pbp["pass_attempt"] == 1]
    rush_plays = pbp[pbp["rush_attempt"] == 1]
    completions = pbp[(pbp["pass_attempt"] == 1) & (pbp["complete_pass"] == 1)]

    out = {
        "pass_yds": _sum_by_player(pass_plays, "passer_player_name", "passing_yards"),
        "pass_tds": _sum_by_player(pass_plays, "passer_player_name", "pass_touchdown"),
        "pass_int": _sum_by_player(pass_plays, "passer_player_name", "interception"),
        "rush_yds": _sum_by_player(rush_plays, "rusher_player_name", "rushing_yards"),
        "rush_tds": _sum_by_player(rush_plays, "rusher_player_name", "rush_touchdown"),
        "rec_yds": _sum_by_player(completions, "receiver_player_name", "receiving_yards"),
        "rec_tds": _sum_by_player(completions, "receiver_player_name", "pass_touchdown"),
        "def_int": _sum_by_player(pbp, "interception_player_name", "interception"),
    }

    sacks: dict[str, float] = {}
    for col, weight in (("sack_player_name", 1.0), ("half_sack_1_player_name", 0.5), ("half_sack_2_player_name", 0.5)):
        if col not in pbp.columns:
            continue
        for name in pbp[col].dropna():
            key = _canonical_key(name)
            if key:
                sacks[key] = sacks.get(key, 0.0) + weight
    out["sacks"] = sacks

    return out


def get_stat_leader_totals() -> dict[str, dict[str, float]]:
    now = datetime.datetime.utcnow()
    if _cache["totals"] is not None and _cache["fetched_at"] is not None and now - _cache["fetched_at"] < CACHE_TTL:
        return _cache["totals"]
    totals = compute_stat_leader_totals()
    _cache.update(fetched_at=now, totals=totals)
    return totals


def compute_leader_scores(candidate_names: list[str], category_totals: dict[str, float]) -> dict[str, float]:
    """Normalizes candidates' career totals in one category into a
    probability distribution -- same "sum to 1 across only the resolvable
    candidates" convention as awards.py's compute_mvp_scores. No team-
    success multiplier here (unlike MVP/OPOY/DPOY) -- a single-stat leader
    market is purely about individual raw production, not team wins."""
    raw: dict[str, float] = {}
    for name in candidate_names:
        key = _canonical_key(name)
        total = category_totals.get(key)
        if total is None or total <= 0:
            continue
        raw[key] = total
    s = sum(raw.values())
    if s <= 0:
        return {}
    return {k: v / s for k, v in raw.items()}
