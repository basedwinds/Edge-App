"""Static NBA arena reference data -- coordinates and timezone are stable
public facts, not worth an API call for, same reasoning as stadiums.py
(NFL). No roof_type field -- every NBA arena is indoor, so there's no
weather-applicability question the way NFL's retractable-roof stadiums need.

tz_offset is hours relative to US Eastern (0), ignoring DST nuance -- same
simplification stadiums.py makes (e.g. Phoenix doesn't observe DST and is
technically its own case, classified as Mountain (-2) same as Denver/Utah,
close enough for a body-clock travel signal).

CHECKED AND REJECTED (2026-07-17), not built into a travel_rules_nba.py:
built this file specifically to test NFL's travel_rules.py signal (long-
distance travel fatigue + westbound-team-crossing-timezones-east body-clock
disadvantage) against real NBA data before porting it over. Neither held:
  - Long-distance travel (>=1500mi): away win rate 43.9% vs. 43.2% for
    shorter trips (n=3,726 vs 11,913) -- essentially flat, no real effect.
  - Timezone-crossing body clock: away win rate was actually HIGHER (47.6%
    vs 42.75%, n=2,274 vs 5,708) when a westbound team crossed 2+ zones
    east -- the OPPOSITE sign from NFL's finding, likely because the
    specific mechanism doesn't transfer: NFL's effect is keyed on an EARLY
    LOCAL KICKOFF, and NBA games are almost universally evening tip-offs
    (confirmed live: real gametime data clusters entirely at US-evening UTC
    hours, essentially zero early-afternoon-local starts exist to trigger
    the same disadvantage). Kept this coordinate table since it's accurate,
    reusable reference data on its own merits -- just not wired into a
    situational-adjustment module, unlike NFL's identically-shaped
    stadiums.py.
"""

ARENAS = {
    "ATL": {"lat": 33.7573, "lon": -84.3963, "tz_offset": 0},
    "BOS": {"lat": 42.3662, "lon": -71.0621, "tz_offset": 0},
    "BKN": {"lat": 40.6826, "lon": -73.9754, "tz_offset": 0},
    "CHA": {"lat": 35.2251, "lon": -80.8392, "tz_offset": 0},
    "CHI": {"lat": 41.8807, "lon": -87.6742, "tz_offset": -1},
    "CLE": {"lat": 41.4965, "lon": -81.6882, "tz_offset": 0},
    "DAL": {"lat": 32.7905, "lon": -96.8103, "tz_offset": -1},
    "DEN": {"lat": 39.7487, "lon": -105.0077, "tz_offset": -2},
    "DET": {"lat": 42.3410, "lon": -83.0550, "tz_offset": 0},
    "GS": {"lat": 37.7680, "lon": -122.3877, "tz_offset": -3},
    "HOU": {"lat": 29.7508, "lon": -95.3621, "tz_offset": -1},
    "IND": {"lat": 39.7640, "lon": -86.1555, "tz_offset": 0},  # Indiana observes Eastern time
    "LAC": {"lat": 33.9457, "lon": -118.3423, "tz_offset": -3},
    "LAL": {"lat": 34.0430, "lon": -118.2673, "tz_offset": -3},
    "MEM": {"lat": 35.1382, "lon": -90.0505, "tz_offset": -1},
    "MIA": {"lat": 25.7814, "lon": -80.1870, "tz_offset": 0},
    "MIL": {"lat": 43.0451, "lon": -87.9172, "tz_offset": -1},
    "MIN": {"lat": 44.9795, "lon": -93.2760, "tz_offset": -1},
    "NO": {"lat": 29.9490, "lon": -90.0821, "tz_offset": -1},
    "NY": {"lat": 40.7505, "lon": -73.9934, "tz_offset": 0},
    "OKC": {"lat": 35.4634, "lon": -97.5151, "tz_offset": -1},
    "ORL": {"lat": 28.5392, "lon": -81.3839, "tz_offset": 0},
    "PHI": {"lat": 39.9012, "lon": -75.1720, "tz_offset": 0},
    "PHX": {"lat": 33.4457, "lon": -112.0712, "tz_offset": -2},  # no DST, classified Mountain like Denver/Utah
    "POR": {"lat": 45.5316, "lon": -122.6668, "tz_offset": -3},
    "SAC": {"lat": 38.5802, "lon": -121.4997, "tz_offset": -3},
    "SA": {"lat": 29.4269, "lon": -98.4375, "tz_offset": -1},
    "TOR": {"lat": 43.6435, "lon": -79.3791, "tz_offset": 0},
    "UTAH": {"lat": 40.7683, "lon": -111.9011, "tz_offset": -2},
    "WSH": {"lat": 38.8981, "lon": -77.0209, "tz_offset": 0},
}

# Real IANA timezone per team's arena -- same reasoning as
# stadiums.py::NFL_TEAM_TZ (a proper DST-aware zoneinfo name, needed for
# nba_markets.py's real "has this game actually started" gate, the NBA
# version of the gap mlb_markets.py already fixed -- confirmed real here too
# but not yet triggered since it was only ever hit off-season/Summer League).
NBA_TEAM_TZ = {
    "ATL": "America/New_York", "BOS": "America/New_York", "BKN": "America/New_York",
    "CHA": "America/New_York", "CHI": "America/Chicago", "CLE": "America/New_York",
    "DAL": "America/Chicago", "DEN": "America/Denver", "DET": "America/New_York",
    "GS": "America/Los_Angeles", "HOU": "America/Chicago", "IND": "America/New_York",
    "LAC": "America/Los_Angeles", "LAL": "America/Los_Angeles", "MEM": "America/Chicago",
    "MIA": "America/New_York", "MIL": "America/Chicago", "MIN": "America/Chicago",
    "NO": "America/Chicago", "NY": "America/New_York", "OKC": "America/Chicago",
    "ORL": "America/New_York", "PHI": "America/New_York", "PHX": "America/Phoenix",
    "POR": "America/Los_Angeles", "SAC": "America/Los_Angeles", "SA": "America/Chicago",
    "TOR": "America/Toronto", "UTAH": "America/Denver", "WSH": "America/New_York",
}
