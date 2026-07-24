"""Orchestrates MLB's situational-factor modules into one combined
adjustment per game -- parallel to situational.py (NFL)/situational_nba.py.
Just injuries this round (position players only -- see
injury_rules_mlb.py's docstring for why starting-pitcher injuries are
already handled at the baseline level, not here). Checked and rejected this
same session, real data not guesses: rest-days (weak/inconsistent pattern,
not a clean signal) and getaway-day fatigue (team win rate 49.94% vs 50.00%
baseline, n=7,003 -- genuinely no effect). Bullpen fatigue is a plausible
MLB-native candidate but would need new box-score/pitching-line ingestion
infrastructure this app doesn't have yet -- left for a future round, not
silently dropped.
"""
from app.models.news_adjustment.injury_rules_mlb import BatterOpsCache, compute_injury_adjustment
from app.models.news_adjustment.schema import NewsAdjustment, merge_adjustments


def compute_situational_adjustment(
    home_injuries: list[dict],
    away_injuries: list[dict],
    season: int,
    ops_cache: BatterOpsCache,
) -> NewsAdjustment | None:
    adjustments = [
        compute_injury_adjustment(home_injuries, away_injuries, season, ops_cache),
    ]
    return merge_adjustments(adjustments)
