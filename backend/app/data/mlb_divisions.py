"""Static MLB division alignment -- stable public facts, not worth an API
call for (same reasoning as divisions.py/nba_divisions.py). Team
abbreviations match this app's canonical MLB Stats API convention (see
mlb_data.py) -- "AZ" not "ARI", "ATH" not "OAK".
"""

DIVISIONS = {
    "AL East": ["BAL", "BOS", "NYY", "TB", "TOR"],
    "AL Central": ["CWS", "CLE", "DET", "KC", "MIN"],
    "AL West": ["HOU", "LAA", "ATH", "SEA", "TEX"],
    "NL East": ["ATL", "MIA", "NYM", "PHI", "WSH"],
    "NL Central": ["CHC", "CIN", "MIL", "PIT", "STL"],
    "NL West": ["AZ", "COL", "LAD", "SD", "SF"],
}

TEAM_DIVISION: dict[str, str] = {team: division for division, teams in DIVISIONS.items() for team in teams}
TEAM_LEAGUE: dict[str, str] = {team: division.split(" ")[0] for team, division in TEAM_DIVISION.items()}


def is_divisional(home_team: str, away_team: str) -> bool:
    return TEAM_DIVISION.get(home_team) is not None and TEAM_DIVISION.get(home_team) == TEAM_DIVISION.get(away_team)
