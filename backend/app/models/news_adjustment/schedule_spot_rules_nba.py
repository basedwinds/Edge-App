"""Free "trap game" / "letdown spot" signal for NBA -- parallel to
schedule_spot_rules.py (NFL), same deliberately-speculative status: this is
handicapping folk wisdom without a clean, independently-documented effect
size, unlike injuries/rest/load-management above it in the situational
stack. Kept small, capped, and always low-confidence on purpose.

Uses elo_service_nba ratings as the "how strong is this opponent" yardstick
and nba_data.py::build_team_schedule_index/get_adjacent_games (this team's
own immediately-previous/next REG game, not a "week" -- the NBA's dense
day-to-day schedule has no week concept) to find each team's adjacent
opponents. A team's first/last game of the season simply produces no
signal, same "no gap to guess at" convention as the NFL version.
"""
from app.models.baseline import elo_service_nba
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

WEAK_OPPONENT_ELO_GAP = 100.0
STRONG_OPPONENT_ELO_MARGIN = 50.0

TRAP_ADJUSTMENT_PP = 1.0
LETDOWN_ADJUSTMENT_PP = 1.0


def _rating(team: str) -> float | None:
    return elo_service_nba.get_team_rating(team)


def _is_trap(team: str, this_game_opp: str | None, next_game_opp: str | None) -> bool:
    if not this_game_opp or not next_game_opp:
        return False
    own, this_opp, next_opp = _rating(team), _rating(this_game_opp), _rating(next_game_opp)
    if own is None or this_opp is None or next_opp is None:
        return False
    weak_this_game = (own - this_opp) >= WEAK_OPPONENT_ELO_GAP
    tough_next_game = (next_opp - own) >= -STRONG_OPPONENT_ELO_MARGIN
    return weak_this_game and tough_next_game


def _is_letdown(team: str, last_game_opp: str | None, last_game_won: bool | None, this_game_opp: str | None) -> bool:
    if not last_game_opp or not this_game_opp or last_game_won is not True:
        return False
    own, last_opp, this_opp = _rating(team), _rating(last_game_opp), _rating(this_game_opp)
    if own is None or last_opp is None or this_opp is None:
        return False
    tough_last_game = (last_opp - own) >= -STRONG_OPPONENT_ELO_MARGIN
    weak_this_game = (own - this_opp) >= WEAK_OPPONENT_ELO_GAP
    return tough_last_game and weak_this_game


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
                factor=f"Home team ({home_team}) plays a much tougher game next -- possible lookahead spot",
                direction="favor_away",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules_nba.py",
            )
        )
    if _is_trap(away_team, home_team, away_next_opp):
        adjustment_pct += TRAP_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Away team ({away_team}) plays a much tougher game next -- possible lookahead spot",
                direction="favor_home",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules_nba.py",
            )
        )
    if _is_letdown(home_team, home_last_opp, home_last_won, away_team):
        adjustment_pct -= LETDOWN_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Home team ({home_team}) beat a much tougher opponent last game -- possible letdown spot",
                direction="favor_away",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules_nba.py",
            )
        )
    if _is_letdown(away_team, away_last_opp, away_last_won, home_team):
        adjustment_pct += LETDOWN_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Away team ({away_team}) beat a much tougher opponent last game -- possible letdown spot",
                direction="favor_home",
                weight="minor",
                rationale="Elo-based schedule-strength comparison; speculative folk-wisdom signal, not "
                "independently validated -- see schedule_spot_rules_nba.py",
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
