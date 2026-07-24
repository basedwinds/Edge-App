"""Matches Kalshi/Polymarket markets to canonical (ESPN-abbreviation) NBA
games. Parallel to market_matcher.py (NFL), kept separate per this project's
architecture decision to not fold sport-specific matching logic into shared
code (see feedback_edge_finder_scope_and_polish memory).

Kalshi <-> ESPN abbreviation differences, confirmed live 2026-07-16 by
comparing Kalshi's championship-futures ticker suffixes (all 30 teams, event
KXNBA-27) against ESPN's own team list (espn_nba_client.py):
  Golden State: Kalshi "GSW" -> ESPN "GS"
  New Orleans:  Kalshi "NOP" -> ESPN "NO"
  New York:     Kalshi "NYK" -> ESPN "NY"
  San Antonio:  Kalshi "SAS" -> ESPN "SA"
  Utah:         Kalshi "UTA" -> ESPN "UTAH"
  Washington:   Kalshi "WAS" -> ESPN "WSH"
  All other current-team abbreviations are identical between the two.
"""
import re
from datetime import date

KALSHI_TO_ESPN_ABBR = {
    "GSW": "GS",
    "NOP": "NO",
    "NYK": "NY",
    "SAS": "SA",
    "UTA": "UTAH",
    "WAS": "WSH",
}
ESPN_TO_KALSHI_ABBR = {v: k for k, v in KALSHI_TO_ESPN_ABBR.items()}


def to_kalshi_abbr(espn_abbr: str) -> str:
    return ESPN_TO_KALSHI_ABBR.get(espn_abbr, espn_abbr)


def to_espn_abbr(kalshi_abbr: str) -> str:
    return KALSHI_TO_ESPN_ABBR.get(kalshi_abbr, kalshi_abbr)


KALSHI_TEAM_ABBRS = {
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
}

# Polymarket futures markets (championship/conference/playoff-qualifier) key
# each team by full "City Mascot" display name via groupItemTitle -- confirmed
# live 2026-07-16 against the real nba-2027-champion event (30 real team
# names + 5 unactivated "Team A".."Team E" placeholder slots + "Other",
# filtered out by the caller the same way polymarket_client.py filters "Other"
# on the NFL side).
POLYMARKET_FULLNAME_TO_ESPN_ABBR = {
    "Atlanta Hawks": "ATL",
    "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET",
    "Golden State Warriors": "GS",
    "Houston Rockets": "HOU",
    "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NO",
    "New York Knicks": "NY",
    "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SA",
    "Toronto Raptors": "TOR",
    "Utah Jazz": "UTAH",
    "Washington Wizards": "WSH",
}
# Not yet confirmed needed live (no real NBA per-game bundle open yet to check
# against, same "not solved yet" status as NFL's spread/total was before one
# opened) -- built proactively since NFL's own mascot-only quirk was a real,
# silent-failure bug caught only once real per-game data existed. Cheap
# insurance: derives directly from the dict above, same pattern as NFL's
# POLYMARKET_MASCOT_TO_NFLVERSE_ABBR.
POLYMARKET_MASCOT_TO_ESPN_ABBR = {
    name.split(" ")[-1]: abbr for name, abbr in POLYMARKET_FULLNAME_TO_ESPN_ABBR.items()
}


def resolve_polymarket_team_name(name: str) -> str | None:
    """Tries, in order: exact full "City Mascot" name, exact mascot-only
    name, then the INPUT's own last word against the mascot dict -- caught
    live 2026-07-16 that Polymarket's Summer League feed uses a THIRD naming
    convention Kalshi/the futures markets don't ("LA Clippers", "LA Lakers"
    -- abbreviated "LA" city, matching neither the full "Los Angeles ..."
    name nor a bare mascot-only lookup on that exact string). Rather than
    hardcode a growing list of city-abbreviation variants, falling back to
    the input's own last word covers this and any similar future variant in
    one step, since every team's mascot word is already that team's last
    name-token by construction."""
    if name in POLYMARKET_FULLNAME_TO_ESPN_ABBR:
        return POLYMARKET_FULLNAME_TO_ESPN_ABBR[name]
    if name in POLYMARKET_MASCOT_TO_ESPN_ABBR:
        return POLYMARKET_MASCOT_TO_ESPN_ABBR[name]
    return POLYMARKET_MASCOT_TO_ESPN_ABBR.get(name.split(" ")[-1])


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# KXNBASUMMERGAME-26JUL17MINLAC -> (2026, 7, 17, "MINLAC") -- confirmed live
# 2026-07-16 against real open Summer League events. KXNBAGAME/KXNBASPREAD/
# KXNBATOTAL (regular season) have 0 open events to verify directly against
# yet, but share the same platform/series-family convention as NFL's
# KXNFLGAME/-SPREAD/-TOTAL, which do all share one convention -- one generic
# parser for any KXNBA{KIND}- prefix, same as market_matcher.py's NFL version.
_EVENT_TICKER_RE = re.compile(r"^KXNBA[A-Z]+-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$")


def parse_kalshi_event_ticker(event_ticker: str):
    m = _EVENT_TICKER_RE.match(event_ticker)
    if not m:
        return None
    yy, mon, dd, teams = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    year = 2000 + int(yy)
    try:
        game_date = date(year, month, int(dd))
    except ValueError:
        return None
    return {"date": game_date, "teams_blob": teams}


def split_teams_blob(teams_blob: str, known_abbrs: set[str]):
    """Try every prefix split of the concatenated AWAYHOME code blob against
    the known set of (Kalshi-side) team abbreviations. Same approach as
    market_matcher.py; NBA has one 3-letter-vs-3-letter ambiguity NFL didn't
    (e.g. "GSWSAS" splits cleanly since both are exactly 3 chars, but a
    3-vs-3 pair sharing a prefix isn't currently possible in the 30-team set
    -- not specially handled, same "try all splits" approach as NFL)."""
    for split_at in range(2, len(teams_blob) - 1):
        away, home = teams_blob[:split_at], teams_blob[split_at:]
        if away in known_abbrs and home in known_abbrs:
            return away, home
    return None


def build_game_index(nba_games: list[dict]) -> dict:
    """Index cached NBA games by (season, away_team, home_team) -> list of
    games (usually length 1; disambiguated by date if a rematch exists)."""
    index: dict[tuple, list[dict]] = {}
    for g in nba_games:
        key = (g["season"], g["away_team"], g["home_team"])
        index.setdefault(key, []).append(g)
    return index


def _match_by_teams_and_date(away: str, home: str, game_date: date, game_index: dict) -> str | None:
    # ESPN's NBA season convention: labeled by the season's ENDING year.
    # REAL BUG caught by testing against live Summer League data (2026-07-16):
    # this was originally month>=8, but Summer League runs in JULY and this
    # app's own nba_summer_league_data.py already labels those games with
    # the NEXT season's ending year (season=2027 for a July 2026 game,
    # preceding the 2026-27 season) -- a month>=8 cutoff computed season=2026
    # for that same July date, a genuine mismatch that silently zeroed out
    # EVERY Summer League match (24/24 Kalshi rows, 26/26 Polymarket rows,
    # confirmed live) before this fix. Playoffs/Finals (June, month<=6)
    # correctly still belong to the SAME season they conclude, unaffected.
    season = game_date.year + 1 if game_date.month >= 7 else game_date.year
    # REAL BUG caught live (2026-07-16): requiring an exact (away, home) order
    # match failed on every real Summer League game -- ESPN and Polymarket
    # disagree on which team is nominally "home" for these (confirmed: real
    # game 401881873 is DAL@OKC per ESPN, but Polymarket's own slug encodes
    # it as OKC@DAL). Summer League is played at neutral tournament venues
    # (Vegas/Salt Lake City) with no real home-court, so there's no ground
    # truth "home" team to disagree about in the first place -- matching the
    # UNORDERED team pair is the correct fix, not a workaround.
    #
    # MUST merge candidates from BOTH orders, not just fall back to the
    # reversed order when the first is empty -- a second real bug caught the
    # same session: Atlanta and Memphis played each other TWICE in Summer
    # League (July 7 ATL@MEM, July 17 MEM@ATL). An `or`-based fallback would
    # only ever see whichever order's list is non-empty first and never even
    # compare it against the other order's candidates, so the "closest by
    # date" pick below could silently pick the wrong one of two real games
    # against the same opponent, or (as happened here) reject a genuine
    # match because it only ever looked at the FAR game.
    candidates = game_index.get((season, away, home), []) + game_index.get((season, home, away), [])
    if not candidates:
        return None
    best = min(candidates, key=lambda g: abs((date.fromisoformat(g["gameday"]) - game_date).days))
    if abs((date.fromisoformat(best["gameday"]) - game_date).days) > 5:
        return None
    return best["id"]


def match_kalshi_event_ticker(event_ticker: str, game_index: dict) -> str | None:
    """Returns the matched NBA game id, or None. Works for any
    KXNBA{GAME,SPREAD,TOTAL,SUMMERGAME,...}- event ticker."""
    parsed = parse_kalshi_event_ticker(event_ticker)
    if not parsed:
        return None
    split = split_teams_blob(parsed["teams_blob"], KALSHI_TEAM_ABBRS)
    if not split:
        return None
    away_k, home_k = split
    away = to_espn_abbr(away_k)
    home = to_espn_abbr(home_k)
    return _match_by_teams_and_date(away, home, parsed["date"], game_index)


# nba-{away}-{home}-{yyyy}-{mm}-{dd} (regular season, mirroring NFL's
# convention) OR nbasl-{away}-{home}-{yyyy}-{mm}-{dd} (Summer League, confirmed
# live 2026-07-16 e.g. "nbasl-okc-dal-2026-07-16"). Regular-season slug isn't
# confirmed against a real live event yet (0 open so far), only inferred from
# Polymarket's consistent NFL-side convention -- worth a quick sanity check
# once real regular-season markets open, same "flagged, not yet verified"
# status this project gives every not-yet-observable structural guess.
#
# REAL BUG caught live: the slug's own team codes are KALSHI-style, not
# ESPN's (confirmed against all 13 real Summer League slugs -- "nyk", "gsw",
# "uta" appear repeatedly, not ESPN's "ny"/"gs"/"utah") -- only 1 of 13 real
# games happened to match by coincidence (both its teams' Kalshi and ESPN
# codes are identical) before this was caught, 24/26 team-rows silently
# unmatched. Routed through the same to_espn_abbr() translation used for
# Kalshi tickers, rather than assuming Polymarket's slug uses its own
# convention.
_POLYMARKET_SLUG_RE = re.compile(r"^nba(?:sl)?-([a-z]+)-([a-z]+)-(\d{4})-(\d{2})-(\d{2})$")


def parse_polymarket_slug(slug: str):
    m = _POLYMARKET_SLUG_RE.match(slug)
    if not m:
        return None
    away, home, yyyy, mm, dd = m.groups()
    try:
        game_date = date(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None
    return {"away": to_espn_abbr(away.upper()), "home": to_espn_abbr(home.upper()), "date": game_date}


def match_polymarket_event(slug: str, game_index: dict) -> str | None:
    parsed = parse_polymarket_slug(slug)
    if not parsed:
        return None
    return _match_by_teams_and_date(parsed["away"], parsed["home"], parsed["date"], game_index)
