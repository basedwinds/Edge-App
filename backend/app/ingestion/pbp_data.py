"""Play-by-play ingestion for EPA-based features, pulled directly from
nflverse's published per-season parquet releases (same "go straight to the
nflverse GitHub release" pattern as ingestion/nfl_data.py -- no nfl_data_py).
"""
import os

import httpx
import pandas as pd

PBP_URL_TEMPLATE = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "pbp_cache")


def fetch_pbp_season(season: int, use_cache: bool = True) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"pbp_{season}.parquet")
    if use_cache and os.path.exists(cache_path):
        return pd.read_parquet(cache_path)

    url = PBP_URL_TEMPLATE.format(season=season)
    resp = httpx.get(url, timeout=120.0, follow_redirects=True)
    resp.raise_for_status()
    with open(cache_path, "wb") as f:
        f.write(resp.content)
    return pd.read_parquet(cache_path)


def fetch_pbp_seasons(seasons: list[int]) -> pd.DataFrame:
    frames = [fetch_pbp_season(s) for s in seasons]
    return pd.concat(frames, ignore_index=True)


def compute_team_week_epa(pbp: pd.DataFrame) -> pd.DataFrame:
    """Per (season, week, team): offensive EPA/play and defensive EPA/play
    allowed, scrimmage plays only (run/pass, excludes kneels/spikes/special
    teams which aren't meaningful team-strength signal)."""
    scrimmage = pbp[pbp["play_type"].isin(["run", "pass"]) & pbp["epa"].notna()]

    off = (
        scrimmage.groupby(["season", "week", "posteam"])["epa"]
        .mean()
        .reset_index()
        .rename(columns={"posteam": "team", "epa": "off_epa"})
    )
    deff = (
        scrimmage.groupby(["season", "week", "defteam"])["epa"]
        .mean()
        .reset_index()
        .rename(columns={"defteam": "team", "epa": "def_epa_allowed"})
    )
    merged = off.merge(deff, on=["season", "week", "team"], how="outer")
    return merged.sort_values(["team", "season", "week"]).reset_index(drop=True)


def add_rolling_features(team_week: pd.DataFrame, window: int = 8) -> pd.DataFrame:
    """Trailing rolling mean of off_epa/def_epa_allowed per team, shifted by
    one game so a team's rating going INTO week W never includes week W's
    own result (no leakage). Rolls across season boundaries by design (recent
    form matters more than a hard reset); Elo already carries the
    season-regression story if we want that blended in separately."""
    team_week = team_week.sort_values(["team", "season", "week"]).copy()
    team_week["off_epa_roll"] = (
        team_week.groupby("team")["off_epa"].transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
    )
    team_week["def_epa_roll"] = (
        team_week.groupby("team")["def_epa_allowed"].transform(
            lambda s: s.shift(1).rolling(window, min_periods=1).mean()
        )
    )
    return team_week
