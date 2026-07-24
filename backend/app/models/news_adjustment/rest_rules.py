"""Free rest/travel adjustment -- nflverse already publishes away_rest/
home_rest (days since each team's last game) in games.csv; this was being
fetched but never used anywhere in the live app until now.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

REST_PP_PER_DAY = 0.3
REST_MAX_ADJUSTMENT_PP = 1.5
NOTABLE_REST_DIFF_DAYS = 4  # e.g. a Thursday short week vs. an opponent off a bye


def compute_rest_adjustment(home_rest: int | None, away_rest: int | None) -> NewsAdjustment | None:
    if home_rest is None or away_rest is None:
        return None
    rest_diff = home_rest - away_rest
    if rest_diff == 0:
        return None

    adjustment_pct = clamp_adjustment(rest_diff * REST_PP_PER_DAY)
    if adjustment_pct == 0:
        return None
    # clamp_adjustment uses the schema-wide +/-7pp cap; also cap to this
    # factor's own, much smaller, max contribution.
    adjustment_pct = max(-REST_MAX_ADJUSTMENT_PP, min(REST_MAX_ADJUSTMENT_PP, adjustment_pct))

    factor = Factor(
        factor=f"{'Home' if rest_diff > 0 else 'Away'} team has {abs(rest_diff)} more rest day(s)",
        direction="favor_home" if rest_diff > 0 else "favor_away",
        weight="moderate" if abs(rest_diff) >= NOTABLE_REST_DIFF_DAYS else "minor",
        rationale="nflverse-published rest days for each team (short week / bye-week advantage)",
    )
    return NewsAdjustment(
        adjustment_pct=adjustment_pct,
        confidence="low",
        factors=[factor],
        requires_review=abs(rest_diff) >= NOTABLE_REST_DIFF_DAYS,
    )
