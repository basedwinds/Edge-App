"""Matches Kalshi/Polymarket markets to canonical (MLB Stats API abbreviation)
MLB games. Parallel to market_matcher_nba.py, same "parallel modules per
sport" architecture call.

Kalshi's own per-team ticker suffixes ALREADY match this app's canonical
convention exactly (confirmed live 2026-07-17 against KXMLBWINS-{team}/
KXMLB-26-{team} futures tickers -- "AZ"/"CWS"/"ATH" all present, matching
MLB Stats API, not ESPN's "ARI"/"CHW") -- unlike NBA, no Kalshi-side
translation map is needed here. Polymarket (and ESPN) differ on those same 2
teams, reusing mlb_data.py's ESPN_TO_STATSAPI_ABBR.
"""
import re
from datetime import date, time

from app.ingestion.mlb_data import ESPN_TO_STATSAPI_ABBR

STATSAPI_TO_POLYMARKET_ABBR = {v: k for k, v in ESPN_TO_STATSAPI_ABBR.items()}


def to_statsapi_abbr(other_abbr: str) -> str:
    return ESPN_TO_STATSAPI_ABBR.get(other_abbr, other_abbr)


# Polymarket's own full "City Mascot" display names, confirmed live 2026-07-17
# against the real mlb-2026-regular-season-win-totals event (all 30 real
# teams, no placeholder slots unlike NBA/NFL futures) -- same role as NBA's
# POLYMARKET_FULLNAME_TO_ESPN_ABBR. Two real naming quirks confirmed, not
# guessed: Athletics have no city prefix ("Athletics", not "Oakland
# Athletics" or "Sacramento Athletics"), and St. Louis carries a period.
POLYMARKET_FULLNAME_TO_STATSAPI_ABBR = {
    "New York Yankees": "NYY",
    "Boston Red Sox": "BOS",
    "Toronto Blue Jays": "TOR",
    "Baltimore Orioles": "BAL",
    "Tampa Bay Rays": "TB",
    "Detroit Tigers": "DET",
    "Kansas City Royals": "KC",
    "Minnesota Twins": "MIN",
    "Cleveland Guardians": "CLE",
    "Chicago White Sox": "CWS",
    "Seattle Mariners": "SEA",
    "Texas Rangers": "TEX",
    "Houston Astros": "HOU",
    "Athletics": "ATH",
    "Los Angeles Angels": "LAA",
    "Atlanta Braves": "ATL",
    "New York Mets": "NYM",
    "Philadelphia Phillies": "PHI",
    "Miami Marlins": "MIA",
    "Washington Nationals": "WSH",
    "Chicago Cubs": "CHC",
    "Pittsburgh Pirates": "PIT",
    "Milwaukee Brewers": "MIL",
    "Cincinnati Reds": "CIN",
    "St. Louis Cardinals": "STL",
    "Los Angeles Dodgers": "LAD",
    "San Francisco Giants": "SF",
    "Arizona Diamondbacks": "AZ",
    "San Diego Padres": "SD",
    "Colorado Rockies": "COL",
}
POLYMARKET_MASCOT_TO_STATSAPI_ABBR = {
    name.split(" ")[-1]: abbr for name, abbr in POLYMARKET_FULLNAME_TO_STATSAPI_ABBR.items()
}


def resolve_polymarket_team_name(name: str) -> str | None:
    """Tries, in order: exact full "City Mascot" name, exact mascot-only
    name, then the input's own last word against the mascot dict -- same
    fallback chain as market_matcher_nba.py's version, cheap insurance
    against a naming-convention variant not yet observed live."""
    if name in POLYMARKET_FULLNAME_TO_STATSAPI_ABBR:
        return POLYMARKET_FULLNAME_TO_STATSAPI_ABBR[name]
    if name in POLYMARKET_MASCOT_TO_STATSAPI_ABBR:
        return POLYMARKET_MASCOT_TO_STATSAPI_ABBR[name]
    return POLYMARKET_MASCOT_TO_STATSAPI_ABBR.get(name.split(" ")[-1])


# KXMLBGAME-26JUL191920LADNYY -> (2026, 7, 19, "19:20" UTC, "LADNYY"). Unlike
# NBA's equivalent ticker, MLB's embeds a 4-digit UTC HHMM BEFORE the team
# blob -- confirmed live 2026-07-17 across real KXMLBGAME/KXMLBSPREAD/
# KXMLBTOTAL/KXMLBF5/KXMLBTEAMTOTAL/KXMLBRFI tickers, all sharing this same
# {yy}{MON}{dd}{HHMM}{teams} shape. Needed for real disambiguation, not just
# defensive parsing: 523 of 23,864 team-days in this app's own cached
# schedule (~2.2%) are doubleheaders (same two teams, same date, two
# separate games) -- the embedded start time is how a specific doubleheader
# game is picked out, see _match_by_teams_and_date below.
_EVENT_TICKER_RE = re.compile(r"^KXMLB[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{4})([A-Z]+)$")

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_kalshi_event_ticker(event_ticker: str):
    m = _EVENT_TICKER_RE.match(event_ticker)
    if not m:
        return None
    yy, mon, dd, hhmm, teams = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    year = 2000 + int(yy)
    try:
        game_date = date(year, month, int(dd))
        game_time = time(int(hhmm[:2]) % 24, int(hhmm[2:]) % 60)
    except ValueError:
        return None
    return {"date": game_date, "time": game_time, "teams_blob": teams}


def split_teams_blob(teams_blob: str, known_abbrs: set[str]):
    """Try every prefix split of the concatenated AWAYHOME code blob against
    the known team-abbreviation set. Same approach as market_matcher_nba.py."""
    for split_at in range(2, len(teams_blob) - 1):
        away, home = teams_blob[:split_at], teams_blob[split_at:]
        if away in known_abbrs and home in known_abbrs:
            return away, home
    return None


def build_game_index(mlb_games: list[dict]) -> dict:
    """Index cached MLB games by (season, away_team, home_team) -> list of
    games (usually length 1, length 2+ for a doubleheader/rematch series)."""
    index: dict[tuple, list[dict]] = {}
    for g in mlb_games:
        key = (g["season"], g["away_team"], g["home_team"])
        index.setdefault(key, []).append(g)
    return index


def _time_diff_minutes(gametime: str | None, target: time) -> int:
    if not gametime:
        return 24 * 60  # unknown gametime sorts last, never wins over a real match
    hh, mm = gametime.split(":")
    minutes = int(hh) * 60 + int(mm)
    target_minutes = target.hour * 60 + target.minute
    return abs(minutes - target_minutes)


def _match_by_teams_and_date(away: str, home: str, game_date: date, game_time: time | None, game_index: dict) -> str | None:
    season = game_date.year
    candidates = game_index.get((season, away, home), [])
    if not candidates:
        return None
    # Exact-date matches first (the normal case) -- among those, prefer the
    # one whose real start time is closest to the ticker's embedded HHMM,
    # which is how a same-day doubleheader is disambiguated.
    same_day = [g for g in candidates if date.fromisoformat(g["gameday"]) == game_date]
    if same_day:
        if game_time is not None:
            best = min(same_day, key=lambda g: _time_diff_minutes(g.get("gametime"), game_time))
        else:
            best = same_day[0]
        return best["id"]
    # REAL BUG fixed here (2026-07-19, user-reported bogus near-0%/near-100%
    # MLB prices): no exact-date match means this ticker's embedded date is a
    # postponed/rescheduled game (real example: a real "26JUL17...PITCLE"
    # Kalshi ticker for a game rained out on Jul 17 and replayed as part of a
    # Jul 18 doubleheader) -- the correct target is whichever OTHER game
    # between these two teams is closest in DATE, full stop. The previous
    # fallback instead sorted by `_time_diff_minutes`, comparing the
    # candidate's LOCAL gametime string against the ticker's UTC HHMM with NO
    # timezone conversion at all -- a raw, meaningless number comparison that,
    # confirmed live, picked an unrelated LATER game (Jul 19, different real
    # matchup) over the correct rescheduled one (Jul 18) purely because its
    # raw local clock digits happened to be numerically closer to the
    # ticker's UTC digits. Time-of-day is only a meaningful disambiguator
    # WITHIN the same real date (the doubleheader case above); across
    # different dates only date-closeness means anything.
    best = min(candidates, key=lambda g: abs((date.fromisoformat(g["gameday"]) - game_date).days))
    if abs((date.fromisoformat(best["gameday"]) - game_date).days) > 3:
        return None
    return best["id"]


def match_kalshi_event_ticker(event_ticker: str, game_index: dict) -> str | None:
    parsed = parse_kalshi_event_ticker(event_ticker)
    if not parsed:
        return None
    # Kalshi's own team codes ARE this app's canonical convention -- no
    # translation needed on this side, unlike NBA/Polymarket below.
    from app.ingestion.mlb_data import team_abbreviations

    split = split_teams_blob(parsed["teams_blob"], team_abbreviations())
    if not split:
        return None
    away, home = split
    return _match_by_teams_and_date(away, home, parsed["date"], parsed["time"], game_index)


# mlb-{away}-{home}-{yyyy}-{mm}-{dd} (confirmed live 2026-07-17 against real
# open per-game events, e.g. "mlb-lad-nyy-2026-07-17"). Team codes are
# lowercase and use Polymarket/ESPN's convention, not this app's canonical
# one -- routed through to_statsapi_abbr() below, same reasoning as NFL/NBA's
# Polymarket-slug handling.
_POLYMARKET_SLUG_RE = re.compile(r"^mlb-([a-z]+)-([a-z]+)-(\d{4})-(\d{2})-(\d{2})$")


def parse_polymarket_slug(slug: str):
    m = _POLYMARKET_SLUG_RE.match(slug)
    if not m:
        return None
    away, home, yyyy, mm, dd = m.groups()
    try:
        game_date = date(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None
    return {"away": to_statsapi_abbr(away.upper()), "home": to_statsapi_abbr(home.upper()), "date": game_date}


def match_polymarket_event(slug: str, game_index: dict) -> str | None:
    parsed = parse_polymarket_slug(slug)
    if not parsed:
        return None
    # No time component in Polymarket's own slug -- doubleheader disambiguation
    # falls back to "closest date" (which is exact-date here anyway), same as
    # passing game_time=None.
    return _match_by_teams_and_date(parsed["away"], parsed["home"], parsed["date"], None, game_index)
