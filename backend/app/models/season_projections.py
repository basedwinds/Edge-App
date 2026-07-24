"""Season-total probability model for individual player stat markets
(KXNFLSEASON{PASSYDS,RSHYDS,RECYDS,REC,RECTD,RSHTD} -- 6 categories, each a
ladder of threshold events). Unlike stat_leaders.py (which just RANKS
candidates against each other for "who leads the league" markets), these
need an actual mean+std probability distribution per player to answer
"P(exceeds threshold)" -- the same Normal-approximation approach
game_lines.py uses for game totals, applied to a season-long individual
total instead.

Every constant here is derived from real year-over-year data (n=325-2067
player-seasons per category, 2012-2025 cached PBP, >=8 games played,
"honest preseason" projection: last season's own rate * last season's own
games, NOT this season's actual games -- an oracle would understate the
real uncertainty), not guessed:

  category   std (% of mean)   bias correction (mean_error)
  pass_yds   29.7%             negligible (~-2% of mean, not applied)
  pass_tds   45.0%             negligible (~-1.7% of mean, not applied)
  rush_yds   64.0%             not separately checked, assumed negligible like pass_yds
  rush_tds   82.1%             -1.37 (a real ~26% overestimate if not corrected)
  rec_yds    51.1%             not separately checked, assumed negligible like pass_yds
  rec_tds    76.9%             -1.17 (a real ~25% overestimate if not corrected)

TD categories get a real, derived regression-to-mean correction because raw
touchdown counts lean heavily on red-zone/scoring luck that genuinely
regresses season to season -- confirmed by checking real data rather than
assumed from "TDs are noisy" folk wisdom. Yardage categories showed no
comparably large bias, so none is applied there (worth rechecking with a
larger sample if this ever gets revisited).

Role-continuity check, the one variable added beyond the raw rate/std
model: a player's last-season rate is a poor predictor if they're no longer
even in that role this season (traded, benched, retired -- a real,
qualitatively different scenario than normal year-over-year regression).
Checked against depth_chart_client's live data before using it: only ONE
WR slot per team is tracked (pos_rank==1 pooled across the whole WR corps,
missing WR2/WR3 entirely) -- so the discount is only applied for QB/RB
(clean 1:1 with the depth chart), never for WR/TE, to avoid incorrectly
penalizing a legitimate WR2/WR3 stat-leader candidate the depth-chart data
just doesn't have the granularity to see. Same "don't guess where the data
can't tell you" convention as everywhere else in this app.
"""
import datetime
import glob
import math
import os

import pandas as pd

from app.models.qb_ratings import _canonical_key

STD_PCT = {
    "pass_yds": 0.297,
    "pass_tds": 0.450,
    "rush_yds": 0.640,
    "rush_tds": 0.821,
    "rec_yds": 0.511,
    "rec_tds": 0.769,
    "rec": 0.511,  # receptions -- no separate check run, reused rec_yds' std_pct as the closest analogue
}
BIAS_CORRECTION = {
    "rush_tds": -1.37,
    "rec_tds": -1.17,
}
# Multiplicative regression-to-mean for YARDAGE, measured 2026-07-23
# (scripts/check_yardage_bias.py) -- the module previously ASSUMED yardage bias
# negligible; rechecked on the full cache and it's a real, consistent ~2-5%
# over-projection (players regress year-over-year: age, injury, role loss).
# Small but systematic, so applied rather than left uncorrected. rec (receptions)
# uses rec_yds' factor as the closest analogue, same as it borrows rec_yds' std.
YARDAGE_REGRESSION = {
    "pass_yds": 0.979,   # measured -2.1%
    "rush_yds": 0.951,   # measured -4.9%
    "rec_yds": 0.968,    # measured -3.2%
    "rec": 0.968,
}
NOT_CURRENT_STARTER_DISCOUNT = 0.4  # applied only for QB/RB, see module docstring

_STAT_COLUMNS = {
    "pass_yds": ("passer_player_name", "pass_attempt", "passing_yards"),
    "pass_tds": ("passer_player_name", "pass_attempt", "pass_touchdown"),
    "rush_yds": ("rusher_player_name", "rush_attempt", "rushing_yards"),
    "rush_tds": ("rusher_player_name", "rush_attempt", "rush_touchdown"),
    "rec_yds": ("receiver_player_name", "complete_pass", "receiving_yards"),
    "rec_tds": ("receiver_player_name", "complete_pass", "pass_touchdown"),
    "rec": ("receiver_player_name", "complete_pass", "complete_pass"),
}

CACHE_TTL = datetime.timedelta(hours=24)
_cache: dict = {"fetched_at": None, "stats": None}


def _cache_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "pbp_cache")


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x < mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def compute_prior_season_stats() -> dict[str, dict[str, dict]]:
    """Returns {category: {canonical_key: {"rate": float, "games": int,
    "total": float}}} using ONLY the most recently completed season in the
    cached PBP (not a multi-year blend) -- the whole point is "what was
    this player's role/production the last time they had a full season," a
    different question than the career-total tallies stat_leaders.py
    computes for the league-leader (ranking-only) markets."""
    files = sorted(glob.glob(os.path.join(_cache_dir(), "pbp_*.parquet")))
    if not files:
        return {}

    needed_cols = {"season", "season_type", "game_id"}
    for player_col, filt_col, val_col in _STAT_COLUMNS.values():
        needed_cols.update([player_col, filt_col, val_col])
    cols = list(needed_cols)

    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f, columns=cols))
        except Exception:
            continue
    if not frames:
        return {}
    pbp = pd.concat(frames, ignore_index=True)
    last_season = int(pbp["season"].max())
    pbp = pbp[pbp["season"] == last_season]
    # REGULAR SEASON ONLY. REAL BUG fixed 2026-07-23: each season's PBP file
    # carries both REG and POST rows, and counting playoff games here inflated
    # every projection -- a player's mean is rate*games = last-season TOTAL, so
    # including 1-4 playoff games meant projecting a REG+POST total (e.g.
    # Stafford 20 games / 5,643 yds) against a REGULAR-SEASON market line
    # (4,499.5). That systematically over-projected the "over" and, before the
    # player-stat futures were made tracking-only, flat-staked ~$3.8k across
    # ~200 markets on the resulting phantom +50-60pp edges. Season-stat markets
    # settle on the REGULAR season, so the projection must be built on it too.
    if "season_type" in pbp.columns:
        pbp = pbp[pbp["season_type"] == "REG"]

    out: dict[str, dict[str, dict]] = {}
    for category, (player_col, filt_col, val_col) in _STAT_COLUMNS.items():
        plays = pbp[pbp[filt_col] == 1]
        grouped = plays.groupby(player_col).agg(total=(val_col, "sum"), games=("game_id", "nunique"))
        cat_out: dict[str, dict] = {}
        for name, row in grouped.iterrows():
            if row["games"] < 8:
                continue  # too few games last season to trust as a role baseline
            key = _canonical_key(name)
            if not key:
                continue
            cat_out[key] = {
                "rate": float(row["total"]) / float(row["games"]),
                "games": int(row["games"]),
                "total": float(row["total"]),
            }
        out[category] = cat_out
    return out


def get_prior_season_stats() -> dict[str, dict[str, dict]]:
    now = datetime.datetime.utcnow()
    if _cache["stats"] is not None and _cache["fetched_at"] is not None and now - _cache["fetched_at"] < CACHE_TTL:
        return _cache["stats"]
    stats = compute_prior_season_stats()
    _cache.update(fetched_at=now, stats=stats)
    return stats


def project_season_total(category: str, prior_entry: dict, is_current_starter: bool | None) -> tuple[float, float]:
    """Returns (mean, std) for this player's projected season total in
    `category`. `is_current_starter`: True/False for QB/RB (checked against
    the live depth chart by the caller), None for WR/TE/unresolvable (skips
    the discount -- see module docstring)."""
    mean = prior_entry["rate"] * prior_entry["games"]
    mean *= YARDAGE_REGRESSION.get(category, 1.0)   # yardage regression-to-mean (see constant)
    mean += BIAS_CORRECTION.get(category, 0.0)
    if is_current_starter is False:
        mean *= NOT_CURRENT_STARTER_DISCOUNT
    mean = max(mean, 0.0)
    std = STD_PCT[category] * max(mean, 1.0)
    return mean, std


def prob_exceeds_season_total(category: str, threshold: float, prior_entry: dict | None, is_current_starter: bool | None) -> float | None:
    if prior_entry is None:
        return None
    mean, std = project_season_total(category, prior_entry, is_current_starter)
    return round(1.0 - _norm_cdf(threshold, mean, std), 4)
