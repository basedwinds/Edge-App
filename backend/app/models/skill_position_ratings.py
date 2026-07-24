"""Free per-RB/WR/TE career-quality signal, extending qb_ratings.py's exact
methodology (canonical name-key, career EPA average from cached PBP,
minimum-volume gating) to the other two positions with a clean, free
per-play EPA attribution. Used by roster_change_rules.py to score whether an
offseason starter change at these positions was a real upgrade/downgrade,
not just "different."

RB uses rushing EPA/carry (rush_attempt==1 plays, keyed by rusher_player_name).
WR/TE use receiving EPA/target -- TARGETS (every pass attempt where a
receiver is listed), not just completions, to avoid conditioning the average
on catches actually made (a receiver who gets targeted on a well-designed but
broken-up play still reflects real usage/opportunity).

Offensive line / defensive front / secondary / linebacker positions have NO
comparably clean free box-score quality metric (sacks-allowed and tackles
are heavily scheme- and teammate-confounded) -- see roster_change_rules.py
for why this project deliberately does not attempt a similar signal there.
"""
import datetime
import glob
import os

import pandas as pd

MIN_CAREER_PLAYS_TO_RATE = 100  # matches qb_ratings.py's MIN_CAREER_DROPBACKS_TO_RATE

CACHE_TTL = datetime.timedelta(hours=24)
_cache: dict = {"fetched_at": None, "rush": None, "recv": None}


def _cache_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "pbp_cache")


def _canonical_key(name: str) -> str:
    """Same first-initial+last-name convention as qb_ratings.py -- kept as an
    independent copy rather than a cross-module import since it's a tiny,
    stable helper, matching this project's existing per-module pattern (e.g.
    weather_rules.py's own _is_outdoor rather than sharing one elsewhere)."""
    cleaned = name.replace(".", " ").replace("'", "").strip()
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].lower()
    return (parts[0][0] + parts[-1]).lower()


def _rate_by_player(pbp: pd.DataFrame, player_col: str) -> dict[str, dict]:
    pbp = pbp[pbp[player_col].notna() & pbp["epa"].notna()]
    totals = pbp.groupby(player_col).agg(total_plays=("epa", "size"), epa_sum=("epa", "sum"))
    out: dict[str, dict] = {}
    for name, row in totals.iterrows():
        if row["total_plays"] < MIN_CAREER_PLAYS_TO_RATE:
            continue
        key = _canonical_key(name)
        if not key:
            continue
        out[key] = {"plays": int(row["total_plays"]), "epa_per_play": float(row["epa_sum"] / row["total_plays"])}
    return out


def _compute_stats() -> tuple[dict[str, dict], dict[str, dict]]:
    files = sorted(glob.glob(os.path.join(_cache_dir(), "pbp_*.parquet")))
    if not files:
        return {}, {}

    cols = ["rush_attempt", "rusher_player_name", "pass_attempt", "receiver_player_name", "epa"]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except Exception:
            continue
    if not frames:
        return {}, {}
    pbp = pd.concat(frames, ignore_index=True)

    rush = _rate_by_player(pbp[pbp["rush_attempt"] == 1], "rusher_player_name")
    recv = _rate_by_player(pbp[pbp["pass_attempt"] == 1], "receiver_player_name")
    return rush, recv


def get_rushing_career_stats() -> dict[str, dict]:
    """Returns {canonical_key: {"plays": int, "epa_per_play": float}}."""
    _refresh_if_stale()
    return _cache["rush"]


def get_receiving_career_stats() -> dict[str, dict]:
    _refresh_if_stale()
    return _cache["recv"]


def _refresh_if_stale():
    now = datetime.datetime.utcnow()
    if _cache["fetched_at"] is not None and now - _cache["fetched_at"] < CACHE_TTL:
        return
    rush, recv = _compute_stats()
    _cache.update(fetched_at=now, rush=rush, recv=recv)
