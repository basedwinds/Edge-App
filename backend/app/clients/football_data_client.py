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
    # Added 2026-08-07. Kalshi lists KXEREDIVISIEGAME (30 open markets) and
    # KXEREDIVISIE (18 league-winner), none of which we could price. Verified
    # live on the same mmz4281 pattern before wiring: N1 serves 1993-94 through
    # 2025-26, 306 rows / 18 clubs a season, and every file self-reports
    # Div="N1" -- so it is NOT subject to the redirect-substitution bug that put
    # a Spanish season into the Portuguese pool (see fetch_season_csv).
    #
    # No Dutch second tier is added: football-data does not publish one, so
    # promoted Eredivisie clubs will have no prior rating and simply price as
    # "no baseline" rather than getting a fabricated one.
    "N1": "Netherlands - Eredivisie",
    # Added 2026-08-08 for a different reason than every league above it: not
    # because Kalshi lists a DOMESTIC market for them, but because their clubs
    # are what blocks UEFA pricing. scripts/check_uefa_coverage.py measured the
    # 2025-26 UEFA season and found the Champions League ceiling capped at
    # 55.6% of matches, with the most frequent unrateable clubs being Racing
    # Genk and Club Brugge (Belgium), Galatasaray and Fenerbahce (Turkey), and
    # Panathinaikos, Olympiacos and PAOK (Greece) -- seven of the fifteen worst
    # blockers, all three countries already published on the mmz4281 pattern
    # this client has always used. Adding them raises the UEFA ceiling with no
    # new fetch logic and no new source.
    #
    # Verified live before wiring, on the ONE check that matters here: every
    # season self-reports its own Div (B1/T1/G1), so none of the three is
    # subject to the redirect substitution that put a Spanish season into the
    # Portuguese pool (see fetch_season_csv). Coverage starts around 2005-06 --
    # 1993-94 returns HTTP 300, which fetch_season_csv already treats as "no
    # file" rather than following into another division's data.
    #
    # No second tier for any of the three: football-data does not publish one,
    # so a promoted Belgian/Turkish/Greek club prices as "no baseline" rather
    # than getting a fabricated rating -- same deliberate handling as N1.
    "B1": "Belgium - Jupiler Pro League",
    "T1": "Turkey - Super Lig",
    "G1": "Greece - Super League",
    # Added 2026-08-08. SCOTLAND closes a real UEFA gap: Celtic and Rangers are
    # regular Champions/Europa League participants and were unrateable, which is
    # also why check_uefa_name_gap.py's fuzzy matcher tried to map Rangers onto
    # Angers and Celtic onto Celta -- there was no correct answer available to it.
    "SC0": "Scotland - Premiership",
    # ENGLISH TIERS 3 AND 4 exist for the EFL Cup, added the same day. Its first
    # round is Plymouth (League One) vs Exeter and Mansfield (League Two) vs
    # Sheffield United -- with only E0/E1 rated, three of those four clubs were
    # unrateable and the competition was almost entirely unpriceable until the
    # Premier League entered at round three.
    "E2": "England - League One",
    "E3": "England - League Two",
}
# ALSO AVAILABLE on the same pattern, not added: SC1/SC2/SC3 (Scottish lower
# tiers) and EC (National League, English 5th tier). No market inventory reaches
# them and they would only add rating pools nothing queries -- revisit if a cup
# draw or a Kalshi series ever does.

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
    simply don't have a file published yet.

    A MISSING FILE DOES NOT 404. football-data.co.uk redirects a request for a
    season/division it has never published to a DIFFERENT division's file, and
    the redirect returns 200 with a perfectly well-formed CSV. Asking for
    9394/P1.csv (Liga Portugal's first season is 94/95) lands on 9394/SP1.csv
    and hands back a full Spanish La Liga season. Verified live, four such
    combos exist across the 11 football-data divisions we pull:

        SP2 1993, SP2 1994, SP2 1995, P1 1993   -> all served SP1

    Because this used to `assign(_div=div)` on whatever came back, 1,602
    Spanish matches were training into the Liga Portugal and Segunda rating
    pools -- Barcelona, Ath Madrid, Ath Bilbao, Celta and La Coruna were all
    rated Portuguese clubs, and soccer Elo is per-league so nothing else
    diluted them. Found while auditing the Liga Portugal pricing shipped in
    a3b03b8.

    The file states its own division in the `Div` column, so trust THAT over
    the division we asked for, and keep only rows that agree. Status code and
    column-shape checks cannot catch this: the wrong file is a valid file."""
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
    if "Div" in df.columns:
        df = df[df["Div"].astype(str).str.strip() == div]
        if df.empty:
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


# --- "EXTRA" FORMAT (2026-08-08) -------------------------------------------
# A SECOND, structurally different feed from the same publisher, at
# /new/{CODE}.csv. Everything above uses mmz4281/{season}/{div}.csv -- one file
# per season per division, with FTHG/FTAG and AvgH-style odds. The extra files
# are one file per COUNTRY covering every season, with different column names.
# They are the only free source for the non-European leagues Kalshi actually
# lists, so they get their own reader rather than being forced into the other.
#
# VERIFIED LIVE before writing this (2026-08-08):
#   BRA 5,525 rows / 15 seasons   ARG 6,280 / 16   MEX 4,682 / 15
#   JPN 4,523 / 14                USA 6,084 / 15   CHN 2,972 / 13
#
# Columns: Country, League, Season, Date, Time, Home, Away, HG, AG, Res, then
# PSCH/PSCD/PSCA (Pinnacle CLOSING), MaxCH/MaxCD/MaxCA, AvgCH/AvgCD/AvgCA.
#
# THE ODDS ARE THE REASON THIS IS WORTH DOING. 5,275 of 5,525 Brazilian rows
# carry closing prices, which makes these leagues BACKTESTABLE against a real
# market -- something MLS never was and never can be (see SoccerMatch's own
# docstring). A first check reported "no odds" because it looked for AvgH/PH;
# this format names them AvgCH/PSCH.
#
# FOUR DIFFERENCES that would silently corrupt data if assumed away:
#   1. No `Div` column, so fetch_season_csv's redirect-substitution guard does
#      not apply. `Country` is validated instead -- these files are not served
#      through the redirect that swapped a Spanish season into the Portuguese
#      pool, but validating something is the point of that guard.
#   2. Dates are DD/MM/YYYY only, not the mmz4281 mixed DD/MM/YY.
#   3. One file can hold MULTIPLE divisions via the `League` column. BRA
#      currently carries only "Serie A", but the code must not assume that --
#      it filters on the league name it was asked for.
#   4. Current-season rows have NaN closing odds (not yet settled), so odds
#      must stay optional per row.
EXTRA_BASE_URL = "https://www.football-data.co.uk/new/{code}.csv"

# app division code -> (file code, League value inside that file, display name)
EXTRA_DIVISIONS = {
    "BRA1": ("BRA", "Serie A", "Brazil - Serie A"),
    "ARG1": ("ARG", "Liga Profesional", "Argentina - Liga Profesional"),
    "MEX1": ("MEX", "Liga MX", "Mexico - Liga MX"),
    "JPN1": ("JPN", "J1 League", "Japan - J1 League"),
    # Added 2026-08-08. Each has live Kalshi inventory AND an ESPN feed; that
    # second condition is what admitted them, see below.
    "SWE1": ("SWE", "Allsvenskan", "Sweden - Allsvenskan"),
    "NOR1": ("NOR", "Eliteserien", "Norway - Eliteserien"),
    "DNK1": ("DNK", "Superliga", "Denmark - Superliga"),
    "CHN1": ("CHN", "Super League", "China - Super League"),
    # DELIBERATELY ABSENT: Poland (Ekstraklasa, 66 open Kalshi markets) and
    # Switzerland (Super League, 52). Both have plenty of football-data history
    # and real Kalshi supply, so they look like the four above -- but ESPN has
    # NO Ekstraklasa feed at all (every slug variant 400s) and its sui.1
    # endpoint returns 200 with zero events.
    #
    # That is disqualifying rather than cosmetic, because ESPN is what SETTLES
    # a soccer bet: espn_soccer_client.LEAGUE_CODES decides which leagues
    # refresh_soccer_results fetches at all, and a league missing from it can
    # never resolve. Shipping these two would mint markets whose bets sit
    # pending forever -- precisely the failure that left 20 Maritimo bets stuck
    # on a finished match. Pricing a market this app cannot settle is worse than
    # not listing it.
}


def fetch_extra_division(div: str) -> pd.DataFrame:
    """Every season of one extra-format division, normalised onto the SAME
    column names the mmz4281 reader produces, so soccer_data can treat both
    identically. Returns an empty frame rather than raising."""
    entry = EXTRA_DIVISIONS.get(div)
    if entry is None:
        return pd.DataFrame()
    code, league_name, _label = entry
    try:
        resp = httpx.get(EXTRA_BASE_URL.format(code=code), timeout=60.0,
                         follow_redirects=True, headers=_HEADERS)
        if resp.status_code != 200 or len(resp.content) < 200:
            return pd.DataFrame()
        df = pd.read_csv(io.BytesIO(resp.content), encoding="latin-1", on_bad_lines="skip")
    except Exception:
        return pd.DataFrame()

    for required in ("Home", "Away", "HG", "AG", "Date"):
        if required not in df.columns:
            return pd.DataFrame()  # shape changed -- refuse rather than guess

    # A file may carry several divisions; keep only the one asked for. If the
    # League column is absent entirely, the file IS one division.
    if "League" in df.columns:
        df = df[df["League"].astype(str).str.strip() == league_name]
    if df.empty:
        return pd.DataFrame()

    out = pd.DataFrame({
        "Date": df["Date"],
        "HomeTeam": df["Home"],
        "AwayTeam": df["Away"],
        "FTHG": pd.to_numeric(df["HG"], errors="coerce"),
        "FTAG": pd.to_numeric(df["AG"], errors="coerce"),
        "FTR": df["Res"] if "Res" in df.columns else None,
        # Map this format's closing-odds names onto the ones
        # _row_to_football_data_match already reads.
        "AvgCH": pd.to_numeric(df.get("AvgCH", df.get("PSCH")), errors="coerce"),
        "AvgCD": pd.to_numeric(df.get("AvgCD", df.get("PSCD")), errors="coerce"),
        "AvgCA": pd.to_numeric(df.get("AvgCA", df.get("PSCA")), errors="coerce"),
    })
    out = out.dropna(subset=["FTHG", "FTAG"])  # unplayed fixtures carry no goals
    season = pd.to_numeric(df.get("Season"), errors="coerce") if "Season" in df.columns else None
    return out.assign(_div=div, _season_start_year=season)
