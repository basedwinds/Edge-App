"""Matches Kalshi KXWNBAGAME markets to canonical (ESPN-abbreviation) WNBA
games -- parallel to market_matcher_nba.py. Two Kalshi<->ESPN abbreviation
differences, confirmed live 2026-07-22 against real open+settled KXWNBAGAME
tickers vs this app's ESPN scoreboard abbreviations:
  Connecticut: Kalshi "CONN" -> ESPN "CON"
  Portland:    Kalshi "PDX"  -> ESPN "POR"
All 13 other current-team abbreviations are identical between the two.
Unlike the NBA, WNBA seasons are a single calendar year, so the season a game
belongs to is just game_date.year (no ending-year convention).
"""
import re
from datetime import date

KALSHI_TO_ESPN_ABBR = {"CONN": "CON", "PDX": "POR"}
ESPN_TO_KALSHI_ABBR = {v: k for k, v in KALSHI_TO_ESPN_ABBR.items()}

# Kalshi-side abbreviations (the ones that appear in ticker team-blobs).
KALSHI_TEAM_ABBRS = {
    "ATL", "CHI", "CONN", "DAL", "GS", "IND", "LA", "LV",
    "MIN", "NY", "PDX", "PHX", "SEA", "TOR", "WSH",
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
# KXWNBAGAME-26JUL22DALPDX -> (2026, 7, 22, "DALPDX")
# Any per-game WNBA series, not just moneyline: KXWNBAGAME / KXWNBASPREAD /
# KXWNBATOTAL all use the same "-YYMMMDD<TEAMS>" event suffix. This was pinned to
# KXWNBAGAME, so when spread/total ingestion was added (2026-08-02) every one of
# those tickers failed to parse and its markets landed unlinked -- unpriceable and
# unsettleable. Season-long tickers like "KXWNBAWINS-27-ATL" still don't match,
# since they have no 3-letter month segment.
# The series segment allows DIGITS as well as letters. This was [A-Z]* and that
# silently broke the half markets the moment they were ingested: KXWNBA1HSPREAD
# and KXWNBA2HTOTAL contain a digit, so all 51 half rows parsed to None, linked
# to no game, and would have sat unpriceable and unsettleable -- the exact
# failure this regex was ALREADY widened once to prevent (from KXWNBAGAME, when
# spread/total were added). Widening it to [A-Z0-9]* covers the quarter series
# (KXWNBA1QSPREAD etc) too, if those are ever built.
_EVENT_TICKER_RE = re.compile(r"^KXWNBA[A-Z0-9]*-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$")


def to_espn_abbr(kalshi_abbr: str) -> str:
    return KALSHI_TO_ESPN_ABBR.get(kalshi_abbr, kalshi_abbr)


def parse_kalshi_event_ticker(event_ticker: str):
    m = _EVENT_TICKER_RE.match(event_ticker)
    if not m:
        return None
    yy, mon, dd, teams = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        game_date = date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None
    return {"date": game_date, "teams_blob": teams}


def split_teams_blob(teams_blob: str, known_abbrs: set[str]):
    for split_at in range(2, len(teams_blob) - 1):
        away, home = teams_blob[:split_at], teams_blob[split_at:]
        if away in known_abbrs and home in known_abbrs:
            return away, home
    return None


def build_game_index(wnba_games: list[dict]) -> dict:
    index: dict[tuple, list[dict]] = {}
    for g in wnba_games:
        index.setdefault((g["season"], g["away_team"], g["home_team"]), []).append(g)
    return index


def _match_by_teams_and_date(away: str, home: str, game_date: date, game_index: dict) -> str | None:
    season = game_date.year  # WNBA: single-calendar-year season
    # Match the UNORDERED pair (merge both orders), same fix as NBA's matcher:
    # neutral-site / All-Star games have no reliable home/away ground truth.
    candidates = game_index.get((season, away, home), []) + game_index.get((season, home, away), [])
    if not candidates:
        return None
    best = min(candidates, key=lambda g: abs((date.fromisoformat(g["gameday"]) - game_date).days))
    if abs((date.fromisoformat(best["gameday"]) - game_date).days) > 5:
        return None
    return best["id"]


def match_kalshi_event_ticker(event_ticker: str, game_index: dict) -> str | None:
    parsed = parse_kalshi_event_ticker(event_ticker)
    if not parsed:
        return None
    split = split_teams_blob(parsed["teams_blob"], KALSHI_TEAM_ABBRS)
    if not split:
        return None
    away_k, home_k = split
    return _match_by_teams_and_date(to_espn_abbr(away_k), to_espn_abbr(home_k), parsed["date"], game_index)
