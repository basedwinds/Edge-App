"""Free "nothing left to play for" adjustment, sourced from ESPN's own
computed standings clincher code (app/clients/espn_client.py::fetch_standings)
rather than re-deriving NFL's real playoff tiebreaker rules ourselves.

Well-documented, high-magnitude effect (a team with its seed fully locked
rests starters, most famously teams sitting starters in meaningless Week 18
games) that's structurally different from the injury signal -- a rested
starter is usually a healthy, game-day-inactive decision, not something that
shows up on the injury report.

Deliberately conservative and week-gated, same philosophy as this project's
other situational rules (e.g. the non-starter injury discount, or rejecting
referee tendencies entirely for small samples):
- Only a clinched #1 seed (bye guaranteed, truly zero incentive left) fires
  before the final week, since that's the cleanest, best-evidenced case.
- Clinched division/wildcard fires only in the literal regular-season finale
  (week 18), when seeding is realistically settled for most such teams.
- Mathematically eliminated teams ("e") are NOT modeled -- research here is
  much weaker/mixed (some teams play loose and hot, others mail it in) than
  the clean clinched-#1-seed case, so left alone rather than guessing a
  direction.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

MIN_WEEK_FOR_ONE_SEED = 17
FINALE_WEEK = 18

ONE_SEED_ADJUSTMENT_PP = 3.5
DIVISION_ADJUSTMENT_PP = 2.0
WILDCARD_ADJUSTMENT_PP = 1.0

_LABELS = {
    "*": ("clinched the #1 seed (bye guaranteed)", ONE_SEED_ADJUSTMENT_PP, "high", "major"),
    "z": ("clinched their division", DIVISION_ADJUSTMENT_PP, "medium", "moderate"),
    "y": ("clinched a wildcard berth", WILDCARD_ADJUSTMENT_PP, "medium", "minor"),
}


def _team_rest_risk(week: int, clincher: str | None) -> tuple[float, str, str, str] | None:
    """Returns (pp, label, confidence, weight) if this team's clinch status is
    likely to matter for THIS game given the week, else None."""
    if not clincher or clincher not in _LABELS:
        return None
    label, pp, confidence, weight = _LABELS[clincher]
    if clincher == "*":
        if week < MIN_WEEK_FOR_ONE_SEED:
            return None
    else:
        if week < FINALE_WEEK:
            return None
    return pp, label, confidence, weight


def compute_playoff_motivation_adjustment(
    away_team: str,
    home_team: str,
    week: int,
    away_standing: dict | None,
    home_standing: dict | None,
) -> NewsAdjustment | None:
    if week < MIN_WEEK_FOR_ONE_SEED:
        return None

    away_clincher = (away_standing or {}).get("clincher")
    home_clincher = (home_standing or {}).get("clincher")

    away_risk = _team_rest_risk(week, away_clincher)
    home_risk = _team_rest_risk(week, home_clincher)
    if away_risk is None and home_risk is None:
        return None

    adjustment_pct = 0.0
    factors: list[Factor] = []
    confidence = "low"
    _rank = {"low": 0, "medium": 1, "high": 2}

    if away_risk is not None:
        pp, label, conf, weight = away_risk
        adjustment_pct += pp  # bad for away = good for home
        confidence = conf if _rank[conf] > _rank[confidence] else confidence
        factors.append(
            Factor(
                factor=f"Away team has {label}, week {week}",
                direction="favor_home",
                weight=weight,
                rationale="ESPN standings clincher code; elevated risk of resting starters with nothing at stake",
            )
        )

    if home_risk is not None:
        pp, label, conf, weight = home_risk
        adjustment_pct -= pp  # bad for home = good for away
        confidence = conf if _rank[conf] > _rank[confidence] else confidence
        factors.append(
            Factor(
                factor=f"Home team has {label}, week {week}",
                direction="favor_away",
                weight=weight,
                rationale="ESPN standings clincher code; elevated risk of resting starters with nothing at stake",
            )
        )

    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(adjustment_pct),
        confidence=confidence,
        factors=factors,
        requires_review=True,  # whether starters actually sit is a real game-day unknown
    )
