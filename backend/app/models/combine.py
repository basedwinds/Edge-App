from app.models.news_adjustment.schema import CONFIDENCE_SCALAR, NewsAdjustment

EPS = 1e-4

DEFAULT_BLEND_WEIGHT = 0.5  # user-tunable in Settings; 0 = ignore news entirely, 1 = full adjustment

# Divisional games are historically closer than a naive rating-based estimate
# expects (division rivals know each other, records tend to even out over the
# season) -- a well-documented squeeze effect, structurally different from the
# "news" uncertainty-scaled adjustments above, so it's applied directly
# rather than through blend_weight/confidence.
DIVISIONAL_SQUEEZE_FRACTION = 0.10


def combine_probability(
    baseline_prob: float,
    news: NewsAdjustment | None,
    blend_weight: float = DEFAULT_BLEND_WEIGHT,
    is_divisional: bool = False,
) -> float:
    """baseline_prob is the home-team win probability from the stats model
    (e.g. Elo). news.adjustment_pct is a home-perspective percentage-point
    nudge, already clamped to +/-7pp (see schema.ADJUSTMENT_CAP_PP)."""
    prob = baseline_prob
    if news is not None:
        scalar = CONFIDENCE_SCALAR[news.confidence]
        delta = blend_weight * scalar * (news.adjustment_pct / 100.0)
        prob = prob + delta

    if is_divisional:
        prob = 0.5 + (prob - 0.5) * (1 - DIVISIONAL_SQUEEZE_FRACTION)

    return max(EPS, min(1 - EPS, prob))


def combine_probability_3way(
    p_home: float, p_draw: float, p_away: float, news: NewsAdjustment | None, blend_weight: float = DEFAULT_BLEND_WEIGHT,
) -> tuple[float, float, float]:
    """Soccer-shaped variant of combine_probability -- a genuinely different
    real structure (3 outcomes, not 2), so this is a parallel function, not
    a generalization of the binary one above. news.adjustment_pct is still a
    home-perspective pp nudge; it shifts probability directly between home
    and away (a news event doesn't make a draw more or less likely on its
    own, only which SIDE wins), leaving p_draw fixed, then renormalizes all
    three so they still sum to 1 after clamping."""
    if news is None:
        return p_home, p_draw, p_away
    scalar = CONFIDENCE_SCALAR[news.confidence]
    delta = blend_weight * scalar * (news.adjustment_pct / 100.0)
    new_home = max(EPS, min(1 - EPS, p_home + delta))
    new_away = max(EPS, min(1 - EPS, p_away - delta))
    total = new_home + p_draw + new_away
    return new_home / total, p_draw / total, new_away / total
