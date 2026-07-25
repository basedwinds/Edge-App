"""Static NFL stadium reference data -- coordinates, roof classification, and
timezone are stable public facts, not something worth an API call for.
games.csv's own `roof` column is blank for future games at retractable-roof
stadiums (the open/close decision isn't made until game week), so this
table's roof_type is the fallback classification used to decide whether
weather applies at all.

tz_offset is hours relative to US Eastern (0), ignoring DST nuance -- e.g.
Arizona doesn't observe DST and is technically its own case, but classifying
it as Mountain (-2) is close enough for a body-clock travel signal.
"""

# roof_type: "outdoor" (always exposed) | "dome" (fixed, never exposed) |
# "retractable" (unknown in advance -- skip weather rather than guess)
STADIUMS = {
    "ARI": {"lat": 33.5276, "lon": -112.2626, "roof_type": "retractable", "tz_offset": -2},
    "ATL": {"lat": 33.7554, "lon": -84.4008, "roof_type": "retractable", "tz_offset": 0},
    "BAL": {"lat": 39.2780, "lon": -76.6227, "roof_type": "outdoor", "tz_offset": 0},
    "BUF": {"lat": 42.7738, "lon": -78.7870, "roof_type": "outdoor", "tz_offset": 0},
    "CAR": {"lat": 35.2258, "lon": -80.8528, "roof_type": "outdoor", "tz_offset": 0},
    "CHI": {"lat": 41.8623, "lon": -87.6167, "roof_type": "outdoor", "tz_offset": -1},
    "CIN": {"lat": 39.0954, "lon": -84.5160, "roof_type": "outdoor", "tz_offset": 0},
    "CLE": {"lat": 41.5061, "lon": -81.6995, "roof_type": "outdoor", "tz_offset": 0},
    "DAL": {"lat": 32.7473, "lon": -97.0945, "roof_type": "retractable", "tz_offset": -1},
    "DEN": {"lat": 39.7439, "lon": -105.0201, "roof_type": "outdoor", "tz_offset": -2},
    "DET": {"lat": 42.3400, "lon": -83.0456, "roof_type": "dome", "tz_offset": 0},
    "GB": {"lat": 44.5013, "lon": -88.0622, "roof_type": "outdoor", "tz_offset": -1},
    "HOU": {"lat": 29.6847, "lon": -95.4107, "roof_type": "retractable", "tz_offset": -1},
    "IND": {"lat": 39.7601, "lon": -86.1639, "roof_type": "retractable", "tz_offset": 0},
    "JAX": {"lat": 30.3239, "lon": -81.6373, "roof_type": "outdoor", "tz_offset": 0},
    "KC": {"lat": 39.0489, "lon": -94.4839, "roof_type": "outdoor", "tz_offset": -1},
    "LA": {"lat": 33.9535, "lon": -118.3392, "roof_type": "dome", "tz_offset": -3},
    "LAC": {"lat": 33.9535, "lon": -118.3392, "roof_type": "dome", "tz_offset": -3},
    "LV": {"lat": 36.0909, "lon": -115.1833, "roof_type": "dome", "tz_offset": -3},
    "MIA": {"lat": 25.9580, "lon": -80.2389, "roof_type": "outdoor", "tz_offset": 0},
    "MIN": {"lat": 44.9735, "lon": -93.2575, "roof_type": "dome", "tz_offset": -1},
    "NE": {"lat": 42.0909, "lon": -71.2643, "roof_type": "outdoor", "tz_offset": 0},
    "NO": {"lat": 29.9511, "lon": -90.0812, "roof_type": "dome", "tz_offset": -1},
    "NYG": {"lat": 40.8135, "lon": -74.0745, "roof_type": "outdoor", "tz_offset": 0},
    "NYJ": {"lat": 40.8135, "lon": -74.0745, "roof_type": "outdoor", "tz_offset": 0},
    "PHI": {"lat": 39.9008, "lon": -75.1675, "roof_type": "outdoor", "tz_offset": 0},
    "PIT": {"lat": 40.4468, "lon": -80.0158, "roof_type": "outdoor", "tz_offset": 0},
    "SEA": {"lat": 47.5952, "lon": -122.3316, "roof_type": "outdoor", "tz_offset": -3},
    "SF": {"lat": 37.4032, "lon": -121.9698, "roof_type": "outdoor", "tz_offset": -3},
    "TB": {"lat": 27.9759, "lon": -82.5033, "roof_type": "outdoor", "tz_offset": 0},
    "TEN": {"lat": 36.1665, "lon": -86.7713, "roof_type": "outdoor", "tz_offset": -1},
    "WAS": {"lat": 38.9076, "lon": -76.8645, "roof_type": "outdoor", "tz_offset": 0},
}

# Real IANA timezone per team's stadium -- same "stable public fact, not
# worth an API call" reasoning as STADIUMS above, but a proper zoneinfo name
# (DST-aware) rather than the rough Eastern-relative tz_offset, needed for
# markets.py's real "has this game actually started" gate (same fix as
# mlb_ballparks.py::TEAM_TZ / mlb_markets.py::_game_already_started -- NFL's
# own version of that gap, confirmed real but not yet triggered since it was
# only ever hit off-season). Detroit and Indianapolis both observe US Eastern
# time with DST (same rules as America/New_York) despite not being in the
# Eastern seaboard, so America/New_York is used for both rather than their
# more obscure but functionally-identical zoneinfo aliases.
NFL_TEAM_TZ = {
    "ARI": "America/Phoenix", "ATL": "America/New_York", "BAL": "America/New_York",
    "BUF": "America/New_York", "CAR": "America/New_York", "CHI": "America/Chicago",
    "CIN": "America/New_York", "CLE": "America/New_York", "DAL": "America/Chicago",
    "DEN": "America/Denver", "DET": "America/New_York", "GB": "America/Chicago",
    "HOU": "America/Chicago", "IND": "America/New_York", "JAX": "America/New_York",
    "KC": "America/Chicago", "LA": "America/Los_Angeles", "LAC": "America/Los_Angeles",
    "LV": "America/Los_Angeles", "MIA": "America/New_York", "MIN": "America/Chicago",
    "NE": "America/New_York", "NO": "America/Chicago", "NYG": "America/New_York",
    "NYJ": "America/New_York", "PHI": "America/New_York", "PIT": "America/New_York",
    "SEA": "America/Los_Angeles", "SF": "America/Los_Angeles", "TB": "America/New_York",
    "TEN": "America/Chicago", "WAS": "America/New_York",
}
