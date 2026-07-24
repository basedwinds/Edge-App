"""Orchestrates all the free, rule-based situational-factor modules into one
combined adjustment per game: injuries (all positions, QB starter-matched,
non-QB confirmed against depth charts), rest/travel days, body-clock/distance
travel, and weather. Kept decoupled from the DB layer -- callers pass plain
values, not ORM objects.
"""
from app.models.news_adjustment.coach_rules import compute_coach_change_adjustment
from app.models.news_adjustment.epa_mismatch_rules import compute_epa_mismatch_adjustment
from app.models.news_adjustment.injury_rules import compute_injury_adjustment, offense_scoring_penalty_pp
from app.models.news_adjustment.playoff_motivation import compute_playoff_motivation_adjustment
from app.models.news_adjustment.rest_rules import compute_rest_adjustment
from app.models.news_adjustment.road_trip_rules import compute_road_trip_adjustment
from app.models.news_adjustment.roster_change_rules import compute_roster_change_adjustment
from app.models.news_adjustment.schedule_spot_rules import compute_schedule_spot_adjustment
from app.models.news_adjustment.schema import NewsAdjustment, merge_adjustments
from app.models.news_adjustment.travel_rules import compute_travel_adjustment
from app.models.news_adjustment.weather_rules import compute_weather_adjustment


def compute_situational_adjustment(
    away_team: str,
    home_team: str,
    away_qb_name: str | None,
    home_qb_name: str | None,
    away_injuries: list[dict],
    home_injuries: list[dict],
    away_rest: int | None,
    home_rest: int | None,
    roof: str | None,
    game_date_iso: str,
    gametime: str | None,
    away_coach_current: str | None = None,
    away_coach_previous: str | None = None,
    home_coach_current: str | None = None,
    home_coach_previous: str | None = None,
    away_starters: set[str] | None = None,
    home_starters: set[str] | None = None,
    week: int | None = None,
    away_standing: dict | None = None,
    home_standing: dict | None = None,
    away_backup_qb: str | None = None,
    home_backup_qb: str | None = None,
    qb_career_stats: dict | None = None,
    home_last_opp: str | None = None,
    home_last_won: bool | None = None,
    home_next_opp: str | None = None,
    away_last_opp: str | None = None,
    away_last_won: bool | None = None,
    away_next_opp: str | None = None,
    away_last_week: dict | None = None,
    away_two_weeks_back: dict | None = None,
    epa_ratings: dict | None = None,
    home_current_positions: dict | None = None,
    away_current_positions: dict | None = None,
    home_previous_positions: dict | None = None,
    away_previous_positions: dict | None = None,
    rush_career_stats: dict | None = None,
    recv_career_stats: dict | None = None,
) -> tuple[NewsAdjustment | None, float, float]:
    """Returns (merged win-probability-space adjustment, home_scoring_penalty_pp,
    away_scoring_penalty_pp). The scoring-penalty pair is a SEPARATE,
    totals-space-relevant signal (backup-QB quality + injury clustering only,
    see injury_rules.py::offense_scoring_penalty_pp) -- not part of the
    win-probability NewsAdjustment, since it feeds game_lines-space totals
    models (markets.py) rather than the moneyline blend."""
    home_scoring_penalty_pp = offense_scoring_penalty_pp(
        home_injuries, home_qb_name, home_starters, home_backup_qb, qb_career_stats
    )
    away_scoring_penalty_pp = offense_scoring_penalty_pp(
        away_injuries, away_qb_name, away_starters, away_backup_qb, qb_career_stats
    )
    injury_adj = compute_injury_adjustment(
        away_qb_name,
        home_qb_name,
        away_injuries,
        home_injuries,
        away_starters,
        home_starters,
        away_backup_qb,
        home_backup_qb,
        qb_career_stats,
    )
    rest_adj = compute_rest_adjustment(home_rest, away_rest)
    weather_adj = compute_weather_adjustment(home_team, away_team, roof, game_date_iso)
    travel_adj = compute_travel_adjustment(away_team, home_team, gametime)
    coach_adj = compute_coach_change_adjustment(
        away_coach_current, away_coach_previous, home_coach_current, home_coach_previous
    )
    playoff_adj = (
        compute_playoff_motivation_adjustment(away_team, home_team, week, away_standing, home_standing)
        if week is not None
        else None
    )
    schedule_spot_adj = compute_schedule_spot_adjustment(
        home_team,
        away_team,
        home_last_opp,
        home_last_won,
        home_next_opp,
        away_last_opp,
        away_last_won,
        away_next_opp,
    )
    road_trip_adj = compute_road_trip_adjustment(away_team, away_last_week, away_two_weeks_back)
    epa_adj = compute_epa_mismatch_adjustment(home_team, away_team, epa_ratings or {})
    roster_change_adj = (
        compute_roster_change_adjustment(
            home_team,
            away_team,
            home_current_positions,
            away_current_positions,
            home_previous_positions,
            away_previous_positions,
            qb_career_stats or {},
            rush_career_stats or {},
            recv_career_stats or {},
        )
        if (home_current_positions and away_current_positions and home_previous_positions and away_previous_positions)
        else None
    )
    merged = merge_adjustments(
        [
            injury_adj,
            rest_adj,
            weather_adj,
            travel_adj,
            coach_adj,
            playoff_adj,
            schedule_spot_adj,
            road_trip_adj,
            epa_adj,
            roster_change_adj,
        ]
    )
    return merged, home_scoring_penalty_pp, away_scoring_penalty_pp
