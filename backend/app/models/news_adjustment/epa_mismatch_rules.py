"""Small, capped offense-vs-defense EPA mismatch adjustment -- the one
scheme-adjacent situational factor backed by real backtest evidence (see
app/models/epa_ratings.py's docstring for the re-examined Phase 2 findings)
rather than folk wisdom like schedule_spot_rules.py.

Deliberately ASYMMETRIC and single-direction, by design, not oversight: the
backtest re-examination found HOME offense vs AWAY defense positively
signed in 100% of 10 walk-forward seasons, but the mirror direction (AWAY
offense vs HOME defense) showed no reliable signal there (sign flipped in
70% of seasons). Building a "symmetric-looking" factor for both directions
would mean re-introducing the exact feature already shown to be unreliable
-- so only the validated direction is modeled here.

EPA_DIFF_SCALE_PP is derived, not guessed: the backtest's mean standardized
logistic-regression coefficient for this feature was ~0.113 (vs. Elo's own
~0.744 on the same standardized scale). Near p=0.5, d(prob)/d(log-odds) =
p(1-p) = 0.25, so 1 standard deviation of the raw feature (std=0.136 EPA/play
in the backtest dataset) maps to roughly 0.113 * 0.25 = 0.0283 (2.83pp) of
win-probability swing -- giving 2.83 / 0.136 = ~20.9 percentage points per
1.0 raw EPA/play unit of mismatch. Still capped hard at a few pp (small
relative to injuries/playoff-motivation) since this is explicitly a
"probably already market-priced" signal, not a claimed edge.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

EPA_DIFF_SCALE_PP = 20.9  # see derivation above
MAX_ADJUSTMENT_PP = 2.0
NOTABLE_EPA_DIFF = 0.05  # below this raw gap (~0.37 std), treat as noise rather than a real mismatch


def compute_epa_mismatch_adjustment(
    home_team: str,
    away_team: str,
    epa_ratings: dict[str, dict],
) -> NewsAdjustment | None:
    home = epa_ratings.get(home_team)
    away = epa_ratings.get(away_team)
    if home is None or away is None:
        return None
    if home.get("off_epa") is None or away.get("def_epa_allowed") is None:
        return None

    diff = home["off_epa"] - away["def_epa_allowed"]
    if abs(diff) < NOTABLE_EPA_DIFF:
        return None

    adjustment_pct = clamp_adjustment(max(-MAX_ADJUSTMENT_PP, min(MAX_ADJUSTMENT_PP, diff * EPA_DIFF_SCALE_PP)))
    favorable = adjustment_pct > 0
    factor = Factor(
        factor=f"{home_team} offense (trailing {8} games) vs {away_team} defense: "
        f"{'favorable' if favorable else 'unfavorable'} EPA/play matchup for {home_team}",
        direction="favor_home" if favorable else "favor_away",
        weight="minor",
        rationale="Rolling EPA/play, home offense vs away defense allowed -- the one scheme-adjacent factor "
        "backed by real backtest evidence (consistently signed across 10 walk-forward seasons), though the "
        "full model including it still didn't beat the market -- likely already priced in, see epa_ratings.py. "
        "The mirror direction (away offense vs home defense) is deliberately not modeled -- unreliable in "
        "the same backtest.",
    )
    return NewsAdjustment(
        adjustment_pct=adjustment_pct,
        confidence="low",
        factors=[factor],
        requires_review=False,
    )
