"""NBA coaching-change detection -- unlike NFL (nflverse publishes each
team's coach per historical game, so a change is a simple diff, see
coach_rules.py), ESPN's NBA roster endpoint only ever exposes the CURRENT
coach with no history. This app tracks its own longitudinal snapshot
instead (see NbaCoachSnapshot, market_catalog_nba.upsert_coach_snapshot).

A snapshot row's previous_coach_name is NULL until a GENUINE transition has
been observed by this app -- that's the signal used here to avoid treating
"this app just started tracking the team" as a coaching change. Combined
with a recency window on `since` (RECENT_CHANGE_WINDOW_DAYS), this fires
only for changes that are both real and still fresh enough to plausibly
matter for an upcoming game -- mirroring NFL's "same season only" scoping,
translated into a day-based window since NBA snapshots don't carry
season/week structure the way nflverse's per-game rows do.
"""
import datetime

from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

COACH_CHANGE_ADJUSTMENT_PP = 2.0  # mirrors NFL's coach_rules.py bump -- same "new coach" short-term effect, no NBA-specific data exists yet to re-derive this independently
RECENT_CHANGE_WINDOW_DAYS = 45  # rough cutoff for "the interim-coach bump still plausibly applies" -- unvalidated, flagged as rough


def _is_recent_genuine_change(previous_coach_name: str | None, since: datetime.datetime | None) -> bool:
    if not previous_coach_name or since is None:
        return False
    age_days = (datetime.datetime.utcnow() - since).days
    return 0 <= age_days <= RECENT_CHANGE_WINDOW_DAYS


def compute_coach_change_adjustment(
    away_coach_name: str | None,
    away_previous_coach_name: str | None,
    away_coach_since: datetime.datetime | None,
    home_coach_name: str | None,
    home_previous_coach_name: str | None,
    home_coach_since: datetime.datetime | None,
) -> NewsAdjustment | None:
    adjustment_pct = 0.0
    factors: list[Factor] = []

    if _is_recent_genuine_change(away_previous_coach_name, away_coach_since):
        adjustment_pct -= COACH_CHANGE_ADJUSTMENT_PP  # bump for away = bad for home
        factors.append(
            Factor(
                factor=f"Away team recently changed head coach ({away_previous_coach_name} -> {away_coach_name})",
                direction="favor_away",
                weight="moderate",
                rationale=f"Coaching change detected by this app's own tracking within the last {RECENT_CHANGE_WINDOW_DAYS} days (no historical per-game coach data exists for NBA)",
            )
        )

    if _is_recent_genuine_change(home_previous_coach_name, home_coach_since):
        adjustment_pct += COACH_CHANGE_ADJUSTMENT_PP
        factors.append(
            Factor(
                factor=f"Home team recently changed head coach ({home_previous_coach_name} -> {home_coach_name})",
                direction="favor_home",
                weight="moderate",
                rationale=f"Coaching change detected by this app's own tracking within the last {RECENT_CHANGE_WINDOW_DAYS} days (no historical per-game coach data exists for NBA)",
            )
        )

    if not factors:
        return None

    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(adjustment_pct),
        confidence="low",  # lower than NFL's "medium" -- this is app-observed recency, not a precise per-game diff
        factors=factors,
        requires_review=True,
    )
