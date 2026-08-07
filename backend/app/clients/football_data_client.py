"""football-data.co.uk client -- free, unauthenticated, per-season CSV files
for England/Spain/Italy/Germany/France's top two divisions, combining match
results (full-time/half-time goals) with real opening+closing bookmaker odds
(3-way moneyline, Asian Handicap, Over/Under 2.5 goals). Confirmed live
2026-07-19: E0 (EPL) goes back to season code "9394" (1993/94) through the
just-finished 2025/26 season; SP1/I1/D1/F1 (La Liga/Serie A/Bundesliga/
Ligue 1) confirmed present on the same `mmz4281/{season}/{div}.csv` URL
pattern via their own `spainm.php`/`italym.php`/`germanym.php`/`francem.php`
pages.

Also confirmed live 2026-07-19: this publisher's own `fixtures.csv` (linked
from `data.php`) is too thin to serve as a real forward schedule -- only
~12 rows at check time, next-matchday-only, and didn't include ANY of these
5 leagues (all off-season in July). Soccer's live/upcoming-match schedule is
therefore derived from Kalshi/Polymarket listings directly, same pattern as
Tennis (see market_catalog_soccer.py), not from this client.

Column names changed over the years as the site added more bookmakers and
opening/closing-line tracking -- confirmed live, not guessed: seasons before
~2019 have no "C"-suffixed closing-odds columns at all (only a single
snapshot per bookmaker), and even in recent seasons a specific bookmaker
column can be absent for lower divisions. `AvgH/AvgD/AvgA` (opening,
averaged across every tracked book) exists back much further than any single
book's own column, so closing-if-present else opening-average is the most
complete real-odds signal available, not a guess.
"""
from __future__ import annotations

import io

import httpx
import pandas as pd

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{div}.csv"
# Confirmed live 2026-07-19 (see docstring). Earliest season football-data.co.uk
# actually serves for England; Spain/Italy/Germany/France may start later --
# fetch_division_history simply gets an empty/404 response for years a
# division has no file yet and skips it, rather than hardcoding a per-league
# start year that would need separate live-verification per league.
EARLIEST_SEASON_START_YEAR = 1993

DIVISIONS = {
    "E0": "England - Premier League",
    "SP1": "Spain - La Liga",
    "I1": "Italy - Serie A",
    "D1": "Germany - Bundesliga",
    "F1": "France - Ligue 1",
    # Added 2026-08-07 to price the four Liga Portugal 1st-half markets Kalshi
    # listed (KXLIGAPORTUGAL1H/1HBTTS/1HSPREAD/1HTOTAL). Confirmed live on the
    # same mmz4281 URL pattern: P1 returns 306 rows / 18 clubs for 2025-26.
    #
    # This was previously deferred as "waiting on volume -- Kalshi quotes it
    # with ~0 trades". That measurement was taken in JULY, with every European
    # league off-season, and it does not survive re-checking: on 2026-08-07
    # KXEPLGAME -- a league this app has priced all along -- also reports 0
    # volume, because the season starts on the 14th. "0 volume in the
    # off-season" says nothing about a league's worth. The runtime
    # has_real_trading gate is what decides whether these can be staked, so
    # building them cannot produce a bad bet; it only decides whether there is
    # anything to look at when trading opens.
    "P1": "Portugal - Primeira Liga",
}

# Second tier of each country -- added 2026-07-19 specifically to fix a real
# gap: a newly-promoted team has ZERO rating in the top-flight-only cache
# above (confirmed live via season_sim_soccer.py's own real Monte Carlo run,
# see that module's docstring), so this app previously fell back to a rough
# bottom-quartile placeholder for every promoted team. Confirmed live on the
# same spainm.php/italym.php/germanym.php/francem.php pages the top-flight
# codes came from -- same `mmz4281/{season}/{div}.csv` URL pattern, no new
# fetch logic needed. Second divisions are trained into their OWN separate
# rating pool (league="E1" etc, same "one state per league" convention as
# every top-flight league) -- see elo_service_soccer.py's own
# get_promoted_team_rating for the real, data-derived conversion this
# enables instead of the old blind placeholder.
SECOND_DIVISIONS = {
    "E1": "England - Championship",
    "SP2": "Spain - Segunda Division",
    "I2": "Italy - Serie B",
    "D2": "Germany - Bundesliga 2",
    "F2": "France - Ligue 2",
}

# Maps a top-flight division code to its own country's second tier -- the
# real promotion pathway this app cares about (a team promoted INTO one of
# the 5 leagues this app models, not every possible tier transition).
PROMOTION_SOURCE_DIVISION = {
    "E0": "E1",
    "SP1": "SP2",
    "I1": "I2",
    "D1": "D2",
    "F1": "F2",
}

# A browser-like User-Agent is needed -- confirmed live 2026-07-19, requests
# with no UA / a bare httpx default UA got a real 429 from this host during
# the audit, while a Mozilla UA succeeded immediately after.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _season_code(start_year: int) -> str:
    """1993 -> "9394", 1999 -> "9900", 2000 -> "0001" -- football-data.co.uk's
    own two-digit-start+two-digit-end season code."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def fetch_season_csv(div: str, start_year: int) -> pd.DataFrame | None:
    """Returns None (not raises) for a season/division combo with no file --
    early seasons for some divisions, or a not-yet-started current season,
    simply don't have a file published yet."""
    url = BASE_URL.format(season=_season_code(start_year), div=div)
    resp = httpx.get(url, timeout=30.0, follow_redirects=True, headers=_HEADERS)
    if resp.status_code != 200 or len(resp.content) < 200:
        return None
    try:
        df = pd.read_csv(io.BytesIO(resp.content), encoding="utf-8-sig", on_bad_lines="skip")
    except Exception:
        return None
    if "Date" not in df.columns or "FTR" not in df.columns:
        return None
    return df.assign(_div=div, _season_start_year=start_year)


def fetch_division_history(div: str, end_year: int | None = None) -> pd.DataFrame:
    import datetime as dt

    end_year = end_year or dt.date.today().year
    frames = []
    for start_year in range(EARLIEST_SEASON_START_YEAR, end_year + 1):
        df = fetch_season_csv(div, start_year)
        if df is not None and len(df):
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
