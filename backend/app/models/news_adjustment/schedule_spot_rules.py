"""Free "trap game" / "letdown spot" signal -- deliberately the softest,
most speculative module in this situational layer. Unlike injuries, rest
days, and weather, which have real published research behind them, "a team
overlooks a weak opponent before a tougher one next week" (lookahead trap)
and "a team lets down after a big win before a weak opponent" (letdown spot)
are handicapping folk wisdom without a clean, independently-documented effect
size. Flagged explicitly here rather than presented with the same confidence
as the rest of this module -- treat this as the most experimental signal in
the app, kept small, capped, and always low-confidence on purpose.

Uses Elo ratings (app/models/baseline/elo_service.py) as the "how strong is
this opponent" yardstick, and nflverse's own schedule (already fetched for
Elo/rest-day data -- see ingestion/nfl_data.py::build_opponent_index) to find
each team's previous- and next-week REG-season opponent. Byes and
season-boundary weeks (no prior/next REG game) simply produce no signal --
no gap to guess at.
"""
from app.models.baseline import elo_service
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

# How much weaker (Elo) this week's/that week's opponent must be to count as
# "weak", and how close the OTHER week's opponent must be (or exceed) to
# count as "not an easy way out" -- both deliberately rough, round-number
# thresholds, not fitted to any backtest (there's no clean historical trap-
# game dataset to fit against).
WEAK_OPPONENT_ELO_GAP = 100.0
STRONG_OPPONENT_ELO_MARGIN = 50.0

TRAP_ADJUSTMENT_PP = 1.0
LETDOWN_ADJUSTMENT_PP = 1.0


def _rating(team: str) -> float | None:
    return elo_service.get_team_rating(team)


def _is_trap(team: str, this_week_opp: str | None, next_week_opp: str | None) -> bool:
    """This week's opponent is weak, but next week's is not an easy way out."""
    if not this_week_opp or not next_week_opp:
        return False
    own, this_opp, next_opp = _rating(team), _rating(this_week_opp), _rating(next_week_opp)
    if own is None or this_opp is None or next_opp is None:
        return False
    weak_this_week = (own - this_opp) >= WEAK_OPPONENT_ELO_GAP
    tough_next_week = (next_opp - own) >= -STRONG_OPPONENT_ELO_MARGIN
    return weak_this_week and tough_next_week


def _is_letdown(team: str, last_week_opp: str | None, last_week_won: bool | None, this_week_opp: str | None) -> bool:
    """Beat a tough opponent last week, now facing a weak one."""
    if not last_week_opp or not this_week_opp or last_week_won is not True:
        return False
    own, last_opp, this_opp = _rating(team), _rating(last_week_opp), _rating(this_week_opp)
    if own is None or last_opp is None or this_opp is None:
        return False
    tough_last_week = (last_opp - own) >= -STRONG_OPPONENT_ELO_MARGIN
    weak_this_week = (own - this_opp) >= WEAK_OPPONENT_ELO_GAP
    return tough_last_week and weak_this_week


def compute_schedule_spot_adjustment(
    home_team: str,
    away_team: str,
    home_last_opp: str | None,
    home_last_won: bool | None,
    home_next_opp: str | None,
    away_last_opp: str | None,
    away_last_won: bool | None,
    away_next_opp: str | None,
) -> NewsAdjustment | None:
    adjustment_pct = 0.0
    factors: list[Factor] = []

    if _is_trap(home_team, away_team, home_next_opp):
        adjustment_pct -= TRAP_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Home team ({home_team}) plays a much tougher game next week -- possible lookahead spot",
                direction="favor_away",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules.py",
            )
        )
    if _is_trap(away_team, home_team, away_next_opp):
        adjustment_pct += TRAP_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Away team ({away_team}) plays a much tougher game next week -- possible lookahead spot",
                direction="favor_home",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules.py",
            )
        )
    if _is_letdown(home_team, home_last_opp, home_last_won, away_team):
        adjustment_pct -= LETDOWN_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Home team ({home_team}) beat a much tougher opponent last week -- possible letdown spot",
                direction="favor_away",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules.py",
            )
        )
    if _is_letdown(away_team, away_last_opp, away_last_won, home_team):
        adjustment_pct += LETDOWN_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Away team ({away_team}) beat a much tougher opponent last week -- possible letdown spot",
                direction="favor_home",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules.py",
            )
        )

    if not factors:
        return None
    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(adjustment_pct),
        confidence="low",
        factors=factors,
        requires_review=False,
    )
