"""tennis-data.co.uk client -- free, unauthenticated, per-year xlsx files
combining ATP/WTA tour-level match results with real bookmaker odds
(Bet365/Pinnacle/Average/Max) and point-in-time WRank/WPts. Confirmed live
2026-07-18: ATP files exist for 2000-2026, WTA for 2007-2026, current
through 2026-07-12 (6 days before this check) -- still an active, maintained
source despite Jeff Sackmann's tennis_atp/tennis_wta GitHub repos both
404ing (confirmed separately, no longer available anywhere free).

Tour-level ONLY -- no Challenger/ITF coverage here (confirmed via a full
Series value-count on the live 2026 file: only ATP250/500/Masters
1000/Grand Slam appear). See app/clients/tennisexplorer_client.py for the
Challenger/ITF source that closes that gap.
"""
from __future__ import annotations

import io

import httpx
import pandas as pd

ATP_URL_TEMPLATE = "http://www.tennis-data.co.uk/{year}/{year}.xlsx"
WTA_URL_TEMPLATE = "http://www.tennis-data.co.uk/{year}w/{year}.xlsx"
ATP_FIRST_YEAR = 2000
WTA_FIRST_YEAR = 2007

# tennis-data.co.uk's own "Comment" column marks rows with no real completed
# play the same way ufc_data.py's NO_PLAY_COMMENTS does for UFC -- excluded
# from Elo training/scoring, never treated as a real result.
NO_PLAY_COMMENTS = {"Walkover", "Disqualified", "Cancelled", "Sched", "Awarded"}


def fetch_year_xlsx(url: str) -> pd.DataFrame | None:
    """Returns None (not raises) for a year with no file yet -- tennis-data.co.uk
    doesn't publish a placeholder for a season that hasn't started, and the
    current year's file simply won't exist before the tour calendar begins."""
    resp = httpx.get(url, timeout=30.0, follow_redirects=True)
    if resp.status_code != 200 or len(resp.content) < 1000:
        return None
    return pd.read_excel(io.BytesIO(resp.content))


def fetch_atp_matches(start_year: int = ATP_FIRST_YEAR, end_year: int | None = None) -> pd.DataFrame:
    return _fetch_range(ATP_URL_TEMPLATE, "atp", start_year, end_year)


def fetch_wta_matches(start_year: int = WTA_FIRST_YEAR, end_year: int | None = None) -> pd.DataFrame:
    return _fetch_range(WTA_URL_TEMPLATE, "wta", start_year, end_year)


def _fetch_range(url_template: str, tour: str, start_year: int, end_year: int | None) -> pd.DataFrame:
    import datetime as dt

    end_year = end_year or dt.date.today().year
    frames = []
    for year in range(start_year, end_year + 1):
        df = fetch_year_xlsx(url_template.format(year=year))
        if df is None:
            continue
        df["tour"] = tour
        df["_source_year"] = year
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
