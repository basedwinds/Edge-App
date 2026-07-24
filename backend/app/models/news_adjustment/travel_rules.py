"""Free travel/body-clock adjustment, using the same stadium coordinates
built for weather. Two well-documented effects, both favoring the home team
when triggered (the away team is the one traveling):

1. Long-distance travel fatigue -- flat, modest, regardless of direction.
2. Body-clock disadvantage -- a westbound-based team (Pacific/Mountain)
   playing an early-afternoon kickoff several time zones east of home plays
   at an unfavorable body-clock hour. This is the specific, most-cited
   version of the travel effect (the reverse -- an Eastern team playing a
   late West Coast night game -- is far less discussed in the research and
   deliberately not modeled here to avoid inventing a symmetric effect that
   isn't equally well-established).
"""
import math

from app.data.stadiums import STADIUMS
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

TRAVEL_MAX_ADJUSTMENT_PP = 1.0
LONG_TRAVEL_MILES = 1500
LONG_TRAVEL_PP = 0.5
BODY_CLOCK_TZ_SHIFT = 2  # hours
BODY_CLOCK_PP = 0.5
EARLY_KICKOFF_CUTOFF = "13:30"  # local-to-stadium kickoff time


def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8  # Earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def compute_travel_adjustment(away_team: str, home_team: str, gametime: str | None) -> NewsAdjustment | None:
    away_stadium = STADIUMS.get(away_team)
    home_stadium = STADIUMS.get(home_team)
    if away_stadium is None or home_stadium is None:
        return None

    distance = _haversine_miles(away_stadium["lat"], away_stadium["lon"], home_stadium["lat"], home_stadium["lon"])
    tz_shift = home_stadium["tz_offset"] - away_stadium["tz_offset"]  # positive = away team traveled east

    adjustment_pct = 0.0
    factors: list[Factor] = []

    if distance >= LONG_TRAVEL_MILES:
        adjustment_pct += LONG_TRAVEL_PP
        factors.append(
            Factor(
                factor=f"Away team travels ~{distance:.0f} miles for this game",
                direction="favor_home",
                weight="minor",
                rationale="Long-distance travel fatigue; modest, direction-agnostic effect",
            )
        )

    if tz_shift >= BODY_CLOCK_TZ_SHIFT and gametime and gametime <= EARLY_KICKOFF_CUTOFF:
        adjustment_pct += BODY_CLOCK_PP
        factors.append(
            Factor(
                factor=f"Away team crosses {tz_shift} time zone(s) east for an early ({gametime}) kickoff",
                direction="favor_home",
                weight="minor",
                rationale="Body-clock disadvantage for a westbound-based team playing an early kickoff",
            )
        )

    if not factors:
        return None

    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(min(adjustment_pct, TRAVEL_MAX_ADJUSTMENT_PP)),
        confidence="low",
        factors=factors,
        requires_review=False,
    )
