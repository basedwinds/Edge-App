"""Free, unauthenticated Transfermarkt client -- one whole-LEAGUE injury-list
page per league (not per-team, no team-ID mapping needed), confirmed live
2026-07-19: plain httpx + a browser User-Agent loads real HTML tables, no
bot/Cloudflare gate (same "just needs a real User-Agent" finding as this
app's other scraped sources, e.g. football_data_client.py).

Real page structure confirmed live: https://www.transfermarkt.com/x/
verletztespieler/wettbewerb/{code} lists EVERY currently-injured player
across the whole competition on one page (no pagination observed live --
counts ranged 22-64 rows across the 6 leagues below, well past a typical
25-per-page cap, so this is treated as the real complete list, not a
first-page slice). Columns: player name + position (nested inline-table),
club name (via the badge image's `title` attr), free-text injury
description, an "until" return-date column (usually blank -- most listings
don't carry a known return date), and the player's real Transfermarkt
market value in EUR -- used here as a free, real severity PROXY (how
"important" this player is), the same role ESPN's PPG plays for NBA (see
injury_rules_nba.py's own docstring) but arguably a closer analogue since
market value is an actual market-assessed valuation of the player, not a
single derived stat.

No explicit status/severity field exists here (unlike ESPN's NFL/NBA/MLB
injury reports, which carry "Out"/"Questionable"/etc) -- Transfermarkt's own
list is binary: on it (currently injured) or not. injury_rules_soccer.py's
own severity model is built around that real constraint, not a guessed
status vocabulary.

Competition codes (Transfermarkt's own, confirmed live against all 6
leagues this app tracks): GB1=Premier League, ES1=La Liga, IT1=Serie A,
L1=Bundesliga, FR1=Ligue 1, MLS1=MLS -- keyed here by THIS app's own
football-data.co.uk-style league codes (E0/SP1/I1/D1/F1/MLS) for a direct
join against SoccerMatch.league, same convention as every other Soccer
client in this app."""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.transfermarkt.com"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

COMPETITION_CODES = {
    "E0": "GB1",
    "SP1": "ES1",
    "I1": "IT1",
    "D1": "L1",
    "F1": "FR1",
    "MLS": "MLS1",
}

_VALUE_RE = re.compile(r"([\d.,]+)\s*(m|k|bn)?", re.IGNORECASE)
_UNIT_MULTIPLIER = {"k": 1_000, "m": 1_000_000, "bn": 1_000_000_000, None: 1}


def _parse_market_value(text: str) -> float | None:
    """"€10.00m" -> 10_000_000.0, "€600k" -> 600_000.0, "-" or empty -> None
    (no listed value -- a real, occasionally-occurring gap, not an error)."""
    if not text:
        return None
    match = _VALUE_RE.search(text.replace(",", "."))
    if not match:
        return None
    number, unit = match.groups()
    try:
        value = float(number)
    except ValueError:
        return None
    return value * _UNIT_MULTIPLIER.get((unit or "").lower() or None, 1)


def fetch_league_injuries(league: str) -> list[dict]:
    """One row per currently-injured player in this league, across every
    club at once. `club` is Transfermarkt's own real club name (e.g.
    "Ipswich Town", "1. FC Koln") -- matched against this app's own team
    roster via market_matcher_soccer.py's existing canonical_team_key/
    TEAM_ALIASES machinery at the call site, not re-derived here."""
    code = COMPETITION_CODES.get(league)
    if code is None:
        return []
    url = f"{BASE_URL}/x/verletztespieler/wettbewerb/{code}"
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception:
        return []

    # Pinned explicitly rather than trusting auto-detection (httpx already
    # gets this right for Transfermarkt, confirmed live -- accented names
    # like "Ángel" decode correctly; this just makes the real UTF-8 encoding
    # a hard guarantee rather than an inference).
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="items")
    if table is None or table.tbody is None:
        return []

    rows = []
    for tr in table.tbody.find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        name_link = cells[0].find("a", title=True)
        if name_link is None:
            continue
        player_name = name_link["title"].strip()
        position_row = cells[0].find_all("tr")
        position = position_row[1].get_text(strip=True) if len(position_row) > 1 else None
        club_link = cells[1].find("a", title=True)
        club = club_link["title"].strip() if club_link else None
        injury = cells[2].get_text(strip=True) or None
        until = cells[3].get_text(strip=True) or None
        market_value = _parse_market_value(cells[4].get_text(strip=True))
        if not player_name or not club:
            continue
        rows.append({
            "player_name": player_name,
            "position": position,
            "club": club,
            "injury": injury,
            "until": until,
            "market_value_eur": market_value,
        })
    return rows


def fetch_all_injuries() -> dict[str, list[dict]]:
    """{league_code: [injury rows]} across all 6 tracked leagues -- one
    request per league (6 total), same "fetch everything up front, filter
    at the call site" pattern as espn_nba_client.fetch_all_injuries()."""
    return {league: fetch_league_injuries(league) for league in COMPETITION_CODES}
