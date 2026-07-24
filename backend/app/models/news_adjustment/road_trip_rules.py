"""Free consecutive-road-game fatigue signal. Distinct from travel_rules.py
(which only scores THIS game's own distance/body-clock burden) -- a team on
its 2nd or 3rd straight road game without returning home in between carries
a real, documented cumulative fatigue effect (time away from home routine
and family, hotel living) on top of any single-game travel effect.

Uses the same schedule opponent-index built for schedule_spot_rules.py's
lookahead/letdown-spot detection (ingestion/nfl_data.py::build_opponent_index),
just reading the `was_home` field. Deliberately shallow -- only looks 2 weeks
back (this game + 2 prior), same "simple, auditable, not over-fit" philosophy
as this project's other situational constants.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

ROAD_TRIP_PP_PER_EXTRA_GAME = 0.5
ROAD_TRIP_MAX_ADJUSTMENT_PP = 1.5

_ORDINAL_SUFFIX = {2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    return f"{n}{_ORDINAL_SUFFIX.get(n, 'th')}"


def _consecutive_road_streak(last_week: dict | None, two_weeks_back: dict | None) -> int:
    """Streak includes the CURRENT game -- always called for the team that's
    away this week, so it always starts at 1."""
    streak = 1
    if last_week is not None and last_week.get("was_home") is False:
        streak += 1
        if two_weeks_back is not None and two_weeks_back.get("was_home") is False:
            streak += 1
    return streak


def compute_road_trip_adjustment(
    away_team: str,
    away_last_week: dict | None,
    away_two_weeks_back: dict | None,
) -> NewsAdjustment | None:
    streak = _consecutive_road_streak(away_last_week, away_two_weeks_back)
    if streak < 2:
        return None

    adjustment_pct = clamp_adjustment(min((streak - 1) * ROAD_TRIP_PP_PER_EXTRA_GAME, ROAD_TRIP_MAX_ADJUSTMENT_PP))
    factor = Factor(
        factor=f"{away_team} is on its {_ordinal(streak)} consecutive road game",
        direction="favor_home",
        weight="minor" if streak == 2 else "moderate",
        rationale="Cumulative road-trip fatigue (time away from home routine), distinct from this game's own "
        "single-game travel distance/body-clock effect -- see travel_rules.py",
    )
    return NewsAdjustment(
        adjustment_pct=adjustment_pct,
        confidence="low",
        factors=[factor],
        requires_review=False,
    )
