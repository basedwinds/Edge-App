"""Static MLB ballpark reference data -- coordinates and roof classification
are stable public facts, mirroring stadiums.py's exact reasoning for NFL.
Used only for the weather-vs-totals signal check (see
scripts/build_mlb_weather_cache.py/check_mlb_weather_signal.py) -- NOT the
same team-keying PARK_FACTOR in game_lines_mlb.py uses (that's about a
park's altitude/dimensions effect on scoring, stable across a stadium's
lifetime regardless of which exact building; this is about the physical
LOCATION, which can genuinely change when a team moves ballparks).

Only the 21 OUTDOOR-roof teams are listed -- retractable-roof and fixed-dome
teams are deliberately excluded rather than guessed at "usually open," same
"unknown roof state = skip" conservative convention as NFL's own
weather_rules.py (this app has no live/historical roof-open-or-closed feed
for MLB, so a game at a retractable park could have been played fully
indoors regardless of the outdoor forecast -- including it would silently
dilute a real outdoor-only effect toward zero, not just add noise).

Excluded, not guessed: TB (Tropicana Field, fixed dome, always closed), AZ/
HOU/MIA/MIL/SEA/TEX/TOR (retractable roof, unknown state). Also excluded:
ATH (Athletics) -- relocated from Oakland to Sacramento in 2025, mid-dataset,
a genuine geographic change (unlike a same-city rebuild/renaming) that would
need per-season coordinates to handle correctly; excluded entirely rather
than risk silently mixing two different climates under one team code.
"""

# Real, sourced compass bearing (degrees from true north) from home plate to
# center field, for the wind-direction-relative scoring check (see
# check_mlb_wind_direction_signal.py) -- converted from Clem's Baseball's own
# 16-point compass "Orientation" column (andrewclem.com/Baseball/
# Stadium_statistics.html, sourced there from Lowry's "Green Cathedrals",
# Ritter 1992, and the ESPN Sports Almanac), confirmed live 2026-07-17, NOT
# guessed -- this app's "never guess a number" rule applies to input data
# too, and no other source found gave numeric/tabular values (several
# ballpark-orientation sites exist but present the data only as image
# diagrams, unusable for a real computation).
ORIENTATION_DEG = {
    "ATL": 157.5, "BAL": 22.5, "BOS": 45.0, "CHC": 45.0, "CIN": 112.5,
    "CLE": 0.0, "COL": 0.0, "CWS": 112.5, "DET": 157.5, "KC": 45.0,
    "LAA": 45.0, "LAD": 22.5, "MIN": 90.0, "NYM": 22.5, "NYY": 67.5,
    "PHI": 22.5, "PIT": 112.5, "SD": 0.0, "SF": 112.5, "STL": 45.0, "WSH": 22.5,
}

BALLPARKS = {
    "ATL": {"lat": 33.8908, "lon": -84.4678, "tz": "America/New_York"},
    "BAL": {"lat": 39.2839, "lon": -76.6217, "tz": "America/New_York"},
    "BOS": {"lat": 42.3467, "lon": -71.0972, "tz": "America/New_York"},
    "CHC": {"lat": 41.9484, "lon": -87.6553, "tz": "America/Chicago"},
    "CIN": {"lat": 39.0979, "lon": -84.5063, "tz": "America/New_York"},
    "CLE": {"lat": 41.4962, "lon": -81.6852, "tz": "America/New_York"},
    "COL": {"lat": 39.7559, "lon": -104.9942, "tz": "America/Denver"},
    "CWS": {"lat": 41.8299, "lon": -87.6338, "tz": "America/Chicago"},
    "DET": {"lat": 42.3390, "lon": -83.0485, "tz": "America/New_York"},
    "KC": {"lat": 39.0517, "lon": -94.4803, "tz": "America/Chicago"},
    "LAA": {"lat": 33.8003, "lon": -117.8827, "tz": "America/Los_Angeles"},
    "LAD": {"lat": 34.0739, "lon": -118.2400, "tz": "America/Los_Angeles"},
    "MIN": {"lat": 44.9817, "lon": -93.2777, "tz": "America/Chicago"},
    "NYM": {"lat": 40.7571, "lon": -73.8458, "tz": "America/New_York"},
    "NYY": {"lat": 40.8296, "lon": -73.9262, "tz": "America/New_York"},
    "PHI": {"lat": 39.9061, "lon": -75.1665, "tz": "America/New_York"},
    "PIT": {"lat": 40.4469, "lon": -80.0057, "tz": "America/New_York"},
    "SD": {"lat": 32.7073, "lon": -117.1566, "tz": "America/Los_Angeles"},
    "SF": {"lat": 37.7786, "lon": -122.3893, "tz": "America/Los_Angeles"},
    "STL": {"lat": 38.6226, "lon": -90.1928, "tz": "America/Chicago"},
    "WSH": {"lat": 38.8730, "lon": -77.0074, "tz": "America/New_York"},
}

# All 30 teams' real, stable IANA timezone -- a SEPARATE, broader concern from
# BALLPARKS above (which is scoped to the 21 outdoor teams for the weather
# signal specifically). Used to resolve this app's known gameday(local
# date)/gametime(raw UTC clock) ambiguity for EVERY team, not just outdoor
# ones -- see clv.py::_game_kickoff_dt and RecommendedBetsTable.tsx's
# formatGameDate, both of which need a real local timezone to disambiguate
# which UTC calendar day a game's gametime actually falls on. The 9 teams
# missing from BALLPARKS (retractable/dome roof, or ATH's relocation) still
# have a perfectly real, stable timezone regardless of roof type or which
# exact building they play in -- ATH's own 2025 Oakland->Sacramento move
# doesn't even change this value (both America/Los_Angeles).
TEAM_TZ = {
    **{team: bp["tz"] for team, bp in BALLPARKS.items()},
    "AZ": "America/Phoenix", "HOU": "America/Chicago", "MIA": "America/New_York",
    "MIL": "America/Chicago", "SEA": "America/Los_Angeles", "TB": "America/New_York",
    "TEX": "America/Chicago", "TOR": "America/Toronto", "ATH": "America/Los_Angeles",
}
