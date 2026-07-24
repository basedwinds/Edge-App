"""Free coaching-change detection -- nflverse already publishes each team's
coach per game (games.csv's home_coach/away_coach), including for future
games (coaching staffs are set well ahead of the season, unlike QB names).
Diffing a team's currently-listed coach against their most recent PLAYED game
this season (see market_catalog.get_previous_coach) detects a genuine
in-season firing/interim-coach situation with zero news-text parsing.

Scoped deliberately to same-season changes only -- the "new coach bump" is a
documented short-term effect specific to in-season interim changes, not a
signal that an offseason hire (a team's normal, expected coach for a full
training camp) should move the estimate.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

COACH_CHANGE_ADJUSTMENT_PP = 2.0


def compute_coach_change_adjustment(
    away_current: str | None,
    away_previous: str | None,
    home_current: str | None,
    home_previous: str | None,
) -> NewsAdjustment | None:
    adjustment_pct = 0.0
    factors: list[Factor] = []

    if away_previous and away_current and away_previous != away_current:
        adjustment_pct -= COACH_CHANGE_ADJUSTMENT_PP  # bump for away = bad for home
        factors.append(
            Factor(
                factor=f"Away team changed head coach this season ({away_previous} -> {away_current})",
                direction="favor_away",
                weight="moderate",
                rationale="nflverse-listed coach differs from this team's most recent played game this season",
            )
        )

    if home_previous and home_current and home_previous != home_current:
        adjustment_pct += COACH_CHANGE_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Home team changed head coach this season ({home_previous} -> {home_current})",
                direction="favor_home",
                weight="moderate",
                rationale="nflverse-listed coach differs from this team's most recent played game this season",
            )
        )

    if not factors:
        return None

    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(adjustment_pct),
        confidence="medium",
        factors=factors,
        requires_review=True,
    )
