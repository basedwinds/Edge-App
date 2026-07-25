"""Static NBA division/conference alignment -- stable public fact, not worth
an API call for (same reasoning as divisions.py/stadiums.py for NFL).

Team abbreviations match ESPN's own convention (this app's canonical NBA
team-code source, confirmed live against
https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams
2026-07-16) -- there is no NBA equivalent of nflverse to defer to instead.
Notable quirks vs. more "obvious" abbreviations, worth remembering when
matching against Kalshi/Polymarket team names in a later phase: Golden State
is "GS" (not "GSW"), New York is "NY" (not "NYK"), San Antonio is "SA" (not
"SAS"), Utah is "UTAH" (not "UTA"), Phoenix is "PHX".
"""

DIVISIONS = {
    "Atlantic": ["BOS", "BKN", "NY", "PHI", "TOR"],
    "Central": ["CHI", "CLE", "DET", "IND", "MIL"],
    "Southeast": ["ATL", "CHA", "MIA", "ORL", "WSH"],
    "Northwest": ["DEN", "MIN", "OKC", "POR", "UTAH"],
    "Pacific": ["GS", "LAC", "LAL", "PHX", "SAC"],
    "Southwest": ["DAL", "HOU", "MEM", "NO", "SA"],
}

TEAM_DIVISION: dict[str, str] = {team: division for division, teams in DIVISIONS.items() for team in teams}

_EASTERN_DIVISIONS = {"Atlantic", "Central", "Southeast"}
TEAM_CONFERENCE: dict[str, str] = {
    team: ("East" if division in _EASTERN_DIVISIONS else "West") for team, division in TEAM_DIVISION.items()
}

CONFERENCES = {
    "East": [t for t, c in TEAM_CONFERENCE.items() if c == "East"],
    "West": [t for t, c in TEAM_CONFERENCE.items() if c == "West"],
}
