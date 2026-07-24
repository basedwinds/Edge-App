"""Free per-team offense/defense rolling-EPA form, used by
news_adjustment/epa_mismatch_rules.py for a small "offense-vs-defense
mismatch" adjustment. Built from the same cached PBP parquet files as
qb_ratings.py (already downloaded for the Phase 2 backtest -- data/pbp_cache/,
2012-2025, no new network call).

Re-examining backend/scripts/backtest_moneyline_gbm.py's actual walk-forward
results (2026-07-15, asked for by the user before building this) found one
specific feature -- home offense EPA/play vs away defense EPA/play allowed --
was positively signed in 100% of 10 walk-forward test seasons (2016-2025)
and only weakly correlated with Elo (r=0.16), a real if modest non-redundant
signal. The MIRROR direction (away offense vs home defense, called
`def_epa_diff` in that backtest) showed NO reliable signal there -- sign
flipped in 70% of seasons -- so this app deliberately does NOT build a
symmetric away-offense-vs-home-defense factor; see epa_mismatch_rules.py.

Caveat kept front-and-center: the FULL Phase 2 model (Elo + this exact
feature + rest + div_game) still did not beat the market's own Brier score.
This signal is real but almost certainly already priced in by the market --
building it makes the app's own reference estimate more complete, not a new
source of edge.
"""
import datetime
import glob
import os

import pandas as pd

from app.ingestion.pbp_data import CACHE_DIR, compute_team_week_epa

ROLLING_WINDOW = 8  # games, matches pbp_data.py's own rolling-feature default

CACHE_TTL = datetime.timedelta(hours=24)
_cache: dict = {"fetched_at": None, "ratings_by_team": None}


def compute_current_epa_ratings() -> dict[str, dict]:
    """Returns {team: {"off_epa": float, "def_epa_allowed": float}} -- each
    team's trailing mean over its last ROLLING_WINDOW played games, across
    all cached seasons (no season reset, matching pbp_data.py's own
    "recent form matters more than a hard reset" rolling-feature choice)."""
    files = sorted(glob.glob(os.path.join(CACHE_DIR, "pbp_*.parquet")))
    if not files:
        return {}

    cols = ["season", "week", "posteam", "defteam", "play_type", "epa"]
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except Exception:
            continue
    if not frames:
        return {}

    pbp = pd.concat(frames, ignore_index=True)
    team_week = compute_team_week_epa(pbp).sort_values(["team", "season", "week"])

    out: dict[str, dict] = {}
    for team, group in team_week.groupby("team"):
        recent = group.tail(ROLLING_WINDOW)
        off_vals = recent["off_epa"].dropna()
        def_vals = recent["def_epa_allowed"].dropna()
        if off_vals.empty or def_vals.empty:
            continue
        out[team] = {"off_epa": float(off_vals.mean()), "def_epa_allowed": float(def_vals.mean())}
    return out


def get_current_epa_ratings() -> dict[str, dict]:
    now = datetime.datetime.utcnow()
    if (
        _cache["ratings_by_team"] is not None
        and _cache["fetched_at"] is not None
        and now - _cache["fetched_at"] < CACHE_TTL
    ):
        return _cache["ratings_by_team"]
    ratings = compute_current_epa_ratings()
    _cache.update(fetched_at=now, ratings_by_team=ratings)
    return ratings
