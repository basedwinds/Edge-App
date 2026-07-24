"""Orchestrates NBA's situational-factor modules into one combined
adjustment per game -- parallel to situational.py (NFL). Rest/back-to-back
is handled at the BASELINE Elo level for NBA, not here (see elo_nba.py's
docstring for why that's a structural correction rather than an uncertain
"news" signal).
"""
import datetime

from app.models.news_adjustment.coach_rules_nba import compute_coach_change_adjustment
from app.models.news_adjustment.injury_rules_nba import compute_injury_adjustment
from app.models.news_adjustment.playoff_motivation_nba import compute_load_management_adjustment
from app.models.news_adjustment.schedule_spot_rules_nba import compute_schedule_spot_adjustment
from app.models.news_adjustment.schema import NewsAdjustment, merge_adjustments


def compute_situational_adjustment(
    home_team: str,
    away_team: str,
    away_injuries: list[dict],
    home_injuries: list[dict],
    gameday: str,
    away_standing: dict | None = None,
    home_standing: dict | None = None,
    home_last_opp: str | None = None,
    home_last_won: bool | None = None,
    home_next_opp: str | None = None,
    away_last_opp: str | None = None,
    away_last_won: bool | None = None,
    away_next_opp: str | None = None,
    player_ppg: dict[str, float] | None = None,
    away_coach_name: str | None = None,
    away_previous_coach_name: str | None = None,
    away_coach_since: datetime.datetime | None = None,
    home_coach_name: str | None = None,
    home_previous_coach_name: str | None = None,
    home_coach_since: datetime.datetime | None = None,
) -> NewsAdjustment | None:
    adjustments = [
        compute_injury_adjustment(home_injuries, away_injuries, player_ppg),
        compute_load_management_adjustment(gameday, away_standing, home_standing),
        compute_schedule_spot_adjustment(
            home_team, away_team,
            home_last_opp, home_last_won, home_next_opp,
            away_last_opp, away_last_won, away_next_opp,
        ),
        compute_coach_change_adjustment(
            away_coach_name, away_previous_coach_name, away_coach_since,
            home_coach_name, home_previous_coach_name, home_coach_since,
        ),
    ]
    return merge_adjustments(adjustments)
