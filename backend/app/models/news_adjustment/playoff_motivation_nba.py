"""Free "nothing left to play for" / load-management adjustment for NBA,
sourced from ESPN's own computed standings clincher code
(espn_nba_client.fetch_standings) -- parallel to playoff_motivation.py (NFL).

NBA's clincher system is richer than NFL's (confirmed live 2026-07-16, each
code carries its own human-readable `description` field): z=clinched
conference/#1 seed, y=clinched division, x=clinched playoff berth,
xp=clinched playoff via play-in win, pb=clinched play-in berth only,
e=eliminated. Mapped by rough analogy to NFL's tiers: z~NFL's "*" (cleanest,
biggest effect -- true nothing-left-to-play-for), y~NFL's "z" (division),
x/xp~NFL's "y" (wildcard-like -- seeding still matters, smaller effect).
pb (play-in berth only) is NOT modeled -- a team fighting to avoid falling
out of even the play-in has real incentive left, not a rest-your-stars
situation. Eliminated ("e") is NOT modeled, same as NFL's precedent --
tanking-for-lottery-position evidence is real in the NBA but mixed/team-
dependent, same "don't guess a direction" reasoning NFL's own docstring gives.

Date-gated instead of week-gated (NBA has no discrete "week" the way the
NFL's 18-week regular season does) -- only fires in the regular season's
final stretch (April), a simple, honestly-approximate proxy for "seeding is
realistically close to settled," not derived from a specific game-count
threshold.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

FINAL_STRETCH_MONTH = 4  # April -- see module docstring for why this is a simple proxy, not a fitted threshold

CONFERENCE_ADJUSTMENT_PP = 3.5
DIVISION_ADJUSTMENT_PP = 2.0
PLAYOFF_BERTH_ADJUSTMENT_PP = 1.0

_LABELS = {
    "z": ("clinched their conference (#1 seed)", CONFERENCE_ADJUSTMENT_PP, "high", "major"),
    "y": ("clinched their division", DIVISION_ADJUSTMENT_PP, "medium", "moderate"),
    "x": ("clinched a playoff berth", PLAYOFF_BERTH_ADJUSTMENT_PP, "medium", "minor"),
    "xp": ("clinched a playoff berth (via play-in)", PLAYOFF_BERTH_ADJUSTMENT_PP, "medium", "minor"),
}


def _team_rest_risk(month: int, clincher: str | None) -> tuple[float, str, str, str] | None:
    if not clincher or clincher not in _LABELS or month < FINAL_STRETCH_MONTH:
        return None
    return _LABELS[clincher]


def compute_load_management_adjustment(
    gameday: str,
    away_standing: dict | None,
    home_standing: dict | None,
) -> NewsAdjustment | None:
    month = int(gameday.split("-")[1])
    if month < FINAL_STRETCH_MONTH:
        return None

    away_risk = _team_rest_risk(month, (away_standing or {}).get("clincher"))
    home_risk = _team_rest_risk(month, (home_standing or {}).get("clincher"))
    if away_risk is None and home_risk is None:
        return None

    adjustment_pct = 0.0
    factors: list[Factor] = []
    confidence = "low"
    _rank = {"low": 0, "medium": 1, "high": 2}

    if away_risk is not None:
        label, pp, conf, weight = away_risk
        adjustment_pct += pp  # away team resting stars is good for home
        confidence = conf if _rank[conf] > _rank[confidence] else confidence
        factors.append(
            Factor(
                factor=f"Away team has {label}",
                direction="favor_home",
                weight=weight,
                rationale="ESPN standings clincher code; elevated risk of resting starters with nothing at stake",
            )
        )

    if home_risk is not None:
        label, pp, conf, weight = home_risk
        adjustment_pct -= pp
        confidence = conf if _rank[conf] > _rank[confidence] else confidence
        factors.append(
            Factor(
                factor=f"Home team has {label}",
                direction="favor_away",
                weight=weight,
                rationale="ESPN standings clincher code; elevated risk of resting starters with nothing at stake",
            )
        )

    return NewsAdjustment(
        adjustment_pct=clamp_adjustment(adjustment_pct),
        confidence=confidence,
        factors=factors,
        requires_review=True,  # whether stars actually sit is a real game-day unknown
    )
