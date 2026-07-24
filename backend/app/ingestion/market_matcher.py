"""Matches Kalshi/Polymarket markets to canonical nflverse games.

Kalshi <-> nflverse abbreviation differences confirmed 2026-07-14 by
cross-referencing live Kalshi event tickers against nflverse's games.csv for
the same matchups (e.g. KXNFLGAME-26SEP14DENKC == nflverse game 2026_01_DEN_KC,
away@home order matches exactly):
  - Jacksonville: Kalshi "JAC"  -> nflverse "JAX"
  - LA Rams:      Kalshi "LAR" -> nflverse "LA"
  All other current-team abbreviations are identical between the two.
"""
import re
from datetime import date

KALSHI_TO_NFLVERSE_ABBR = {
    "JAC": "JAX",
    "LAR": "LA",
}

# Reverse of the above -- needed by division_order matching (market_catalog.py),
# which has to build a Kalshi-style ticker blob FROM a known nflverse
# abbreviation (the inverse direction of every other lookup in this file).
NFLVERSE_TO_KALSHI_ABBR = {v: k for k, v in KALSHI_TO_NFLVERSE_ABBR.items()}


def to_kalshi_abbr(nflverse_abbr: str) -> str:
    return NFLVERSE_TO_KALSHI_ABBR.get(nflverse_abbr, nflverse_abbr)

# Polymarket's futures markets (division/conference/Super Bowl champion,
# playoff qualifier -- see polymarket_client.py::get_futures_markets) key
# each team by its full "City Mascot" display name via the market's own
# `groupItemTitle` field, not an abbreviation -- confirmed live 2026-07-15
# across all of division-winner/conference-champion/Super-Bowl-champion/
# playoff-qualifier events, consistent full names throughout.
POLYMARKET_FULLNAME_TO_NFLVERSE_ABBR = {
    "Arizona Cardinals": "ARI",
    "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR",
    "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN",
    "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN",
    "Detroit Lions": "DET",
    "Green Bay Packers": "GB",
    "Houston Texans": "HOU",
    "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC",
    "Los Angeles Rams": "LA",
    "Los Angeles Chargers": "LAC",
    "Las Vegas Raiders": "LV",
    "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN",
    "New England Patriots": "NE",
    "New Orleans Saints": "NO",
    "New York Giants": "NYG",
    "New York Jets": "NYJ",
    "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT",
    "Seattle Seahawks": "SEA",
    "San Francisco 49ers": "SF",
    "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}

# Per-game spread/total sub-markets (see polymarket_client.py::
# get_spread_total_markets) use mascot-ONLY outcome names ("Panthers",
# "Cardinals"), NOT the full "City Mascot" names the futures markets use
# (POLYMARKET_FULLNAME_TO_NFLVERSE_ABBR above) -- a real, confirmed
# inconsistency within Polymarket's own platform (different market
# template), caught live 2026-07-15/16 against the first real NFL spread
# market to open (nfl-car-ari-2026-08-07): every row silently failed to
# match on the full-name dict alone. resolve_polymarket_team_name tries
# both rather than assuming either format.
POLYMARKET_MASCOT_TO_NFLVERSE_ABBR = {
    name.split(" ")[-1]: abbr for name, abbr in POLYMARKET_FULLNAME_TO_NFLVERSE_ABBR.items()
}


def resolve_polymarket_team_name(name: str) -> str | None:
    return POLYMARKET_FULLNAME_TO_NFLVERSE_ABBR.get(name) or POLYMARKET_MASCOT_TO_NFLVERSE_ABBR.get(name)


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# KXNFLGAME-26SEP14DENKC -> (2026, 9, 14, "DEN", "KC"). KXNFLSPREAD-/
# KXNFLTOTAL- event tickers share this exact date+teams-blob convention
# (confirmed live: KXNFLSPREAD-26FEB08SEANE, KXNFLTOTAL-26FEB08SEANE for the
# same matchup as a KXNFLGAME- event would use) -- one generic parser for
# all three series prefixes rather than one per market type.
_EVENT_TICKER_RE = re.compile(r"^KXNFL[A-Z]+-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$")


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

    # teams blob is AWAYHOME concatenated with no separator; team codes are
    # 2-3 chars, so try splits from the known-abbreviation set built from the
    # games table at match time (see match_kalshi_moneyline_market).
    return {"date": game_date, "teams_blob": teams}


def to_nflverse_abbr(kalshi_abbr: str) -> str:
    return KALSHI_TO_NFLVERSE_ABBR.get(kalshi_abbr, kalshi_abbr)


def split_teams_blob(teams_blob: str, known_abbrs: set[str]):
    """Try every prefix split of the concatenated AWAYHOME code blob against
    the known set of (Kalshi-side) team abbreviations."""
    for split_at in range(2, len(teams_blob) - 1):
        away, home = teams_blob[:split_at], teams_blob[split_at:]
        if away in known_abbrs and home in known_abbrs:
            return away, home
    return None


def build_game_index(nfl_games: list[dict]) -> dict:
    """Index nflverse games by (season, away_team, home_team) -> list of games
    (usually length 1; disambiguated by date if a rematch exists)."""
    index: dict[tuple, list[dict]] = {}
    for g in nfl_games:
        key = (g["season"], g["away_team"], g["home_team"])
        index.setdefault(key, []).append(g)
    return index


KALSHI_TEAM_ABBRS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAC", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE",
    "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
}


def _match_by_teams_and_date(away: str, home: str, game_date: date, game_index: dict) -> str | None:
    season = game_date.year if game_date.month >= 3 else game_date.year - 1
    candidates = game_index.get((season, away, home), [])
    if not candidates:
        return None
    # Pick the closest-by-date candidate (disambiguates a same-season
    # rematch), but reject it outright if the gap is too large -- e.g. a
    # preseason market whose only same-team-pair candidate is a regular-season
    # rematch months later is NOT a match, just a shared matchup. A real
    # match should land within a few days of the actual scheduled gameday.
    best = min(candidates, key=lambda g: abs((date.fromisoformat(g["gameday"]) - game_date).days))
    if abs((date.fromisoformat(best["gameday"]) - game_date).days) > 5:
        return None
    return best["id"]


def match_kalshi_event_ticker(event_ticker: str, game_index: dict) -> str | None:
    """Returns the matched nflverse game_id, or None. Works for any
    KXNFL{GAME,SPREAD,TOTAL}- event ticker, not just moneyline."""
    parsed = parse_kalshi_event_ticker(event_ticker)
    if not parsed:
        return None
    split = split_teams_blob(parsed["teams_blob"], KALSHI_TEAM_ABBRS)
    if not split:
        return None
    away_k, home_k = split
    away = to_nflverse_abbr(away_k)
    home = to_nflverse_abbr(home_k)
    return _match_by_teams_and_date(away, home, parsed["date"], game_index)


# Backward-compat alias -- moneyline ingestion already calls this name.
match_kalshi_moneyline_event = match_kalshi_event_ticker


# nfl-{away}-{home}-{yyyy}-{mm}-{dd}, e.g. "nfl-la-kc-2026-08-15"
_POLYMARKET_SLUG_RE = re.compile(r"^nfl-([a-z]+)-([a-z]+)-(\d{4})-(\d{2})-(\d{2})$")


def parse_polymarket_slug(slug: str):
    m = _POLYMARKET_SLUG_RE.match(slug)
    if not m:
        return None
    away, home, yyyy, mm, dd = m.groups()
    try:
        game_date = date(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None
    return {"away": away.upper(), "home": home.upper(), "date": game_date}


def match_polymarket_event(slug: str, game_index: dict) -> str | None:
    parsed = parse_polymarket_slug(slug)
    if not parsed:
        return None
    return _match_by_teams_and_date(parsed["away"], parsed["home"], parsed["date"], game_index)
