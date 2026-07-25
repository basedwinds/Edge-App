"""Static NFL division/conference alignment -- stable public facts, not
worth an API call for (same reasoning as stadiums.py). Team abbreviations
match nflverse's own convention, consistent with every other team-keyed
table in this app (e.g. "LA" for the Rams, "LAC" for the Chargers).
"""

DIVISIONS = {
    "AFC East": ["BUF", "MIA", "NE", "NYJ"],
    "AFC North": ["BAL", "CIN", "CLE", "PIT"],
    "AFC South": ["HOU", "IND", "JAX", "TEN"],
    "AFC West": ["DEN", "KC", "LAC", "LV"],
    "NFC East": ["DAL", "NYG", "PHI", "WAS"],
    "NFC North": ["CHI", "DET", "GB", "MIN"],
    "NFC South": ["ATL", "CAR", "NO", "TB"],
    "NFC West": ["ARI", "LA", "SEA", "SF"],
}

TEAM_DIVISION: dict[str, str] = {team: division for division, teams in DIVISIONS.items() for team in teams}
TEAM_CONFERENCE: dict[str, str] = {team: division.split(" ")[0] for team, division in TEAM_DIVISION.items()}

CONFERENCES = {
    "AFC": [t for t, c in TEAM_CONFERENCE.items() if c == "AFC"],
    "NFC": [t for t, c in TEAM_CONFERENCE.items() if c == "NFC"],
}
