"""Free, rule-based WNBA availability adjustment -- sibling of
injury_rules_nba.py, reusing the same sport-agnostic NewsAdjustment/Factor
schema.

WHY IT IS NOT A COPY OF THE NBA'S NUMBERS. The NBA weight was calibrated
against real box scores rather than guessed, so copying it here would have
thrown that away: a WNBA roster carries ~11-12 active players against the
NBA's 12-15, so one starter is a LARGER share of team value, and the effect
should be measured. It was (see scripts/calibrate_wnba_injury.py):

  602 ESPN box scores (2024+2025), walk-forward elo_wnba, residual measured
  for a team missing k of its top-3-by-minutes players WHILE THE OPPONENT IS
  AT FULL STRENGTH (requiring the opponent whole is what makes the effect
  attributable rather than diluted):

      top-3 out    n     mean residual    SE
          0       878      -0.00pp       1.59
          1       139      -9.70pp       3.70
          2         9     -32.51pp      14.67   (too thin to use)

  The 0-out baseline landing at exactly -0.00pp is the methodological check:
  the Elo predictions are unbiased on that subset, so the -9.70pp is not an
  artifact of a skewed baseline. 95% CI [-16.9, -2.5] excludes zero.

  NBA's equivalent is -4.2pp. The WNBA effect is ~2.3x, which is the
  direction the roster-size argument predicts.

MEASURED BY MINUTES, SO PRICED BY MINUTES. The calibration defined a "key
player" as top-3 by minutes, so the live rule tiers on minutes per game, not
points. Tiering on scoring would price something other than what was measured.

DELIBERATELY CONSERVATIVE. BASE_WEIGHT_PP is 7.0 against a measured 9.7 --
the same shrink-toward-zero the NBA calibration applied (3.0 against a
measured 4.2), because SE 3.70 is wide. And like the NBA's, this adjustment
mostly makes the model MATCH the market, which prices a star's absence
instantly; it is defensive, not an edge source. Without it the model would
show a large fake edge on a team missing its best player.

MAX_TOTAL_PP is NOT calibrated. The 2-out bucket is 9 games; -32.51pp with an
SE of 14.67 is not a number to build on. The cap is set at roughly twice the
single-player effect as a bound, and should be revisited if that bucket ever
gets a real sample.
"""
from app.models.news_adjustment.schema import Factor, NewsAdjustment, clamp_adjustment

# Confirmed live 2026-08-06: only "Out" and "Day-To-Day" are in use across all
# 15 teams / 39 listed players. The others mirror the NBA module's vocabulary
# and are NOT yet confirmed for the WNBA -- flagged rather than presented as
# observed, same convention as this app's other unvalidated constants.
STATUS_RULES = {
    "Out": {"severity": 1.0, "confidence": "high"},          # confirmed live
    "Day-To-Day": {"severity": 0.35, "confidence": "low"},   # confirmed live
    "Injured Reserve": {"severity": 1.0, "confidence": "high"},   # not confirmed for WNBA
    "Suspension": {"severity": 1.0, "confidence": "high"},        # not confirmed for WNBA
    "Doubtful": {"severity": 0.6, "confidence": "medium"},        # not confirmed for WNBA
    "Questionable": {"severity": 0.25, "confidence": "low"},      # not confirmed for WNBA
}

# THE MEASUREMENT EXCEEDS THE APP-WIDE CLAMP, and that is worth stating rather
# than letting it disappear quietly. schema.ADJUSTMENT_CAP_PP is 7.0pp and is
# SHARED -- NFL, NBA, MLB and Soccer were all calibrated underneath it -- so
# raising it for the WNBA would silently re-tune four other sports. It is left
# alone.
#
# The consequence: one top-3 player out measured -9.70pp, so the WNBA layer
# SATURATES at 7.0pp on any genuine star absence and cannot express the rest.
# Base is set to exactly 7.0 so a 1.0-tier player lands on that ceiling; the
# effect is a deliberate UNDER-adjustment of roughly 2.7pp on the worst cases,
# which is the conservative direction (it under-reacts to injuries rather than
# inventing edge). Revisit only if the shared cap is ever revisited.
BASE_WEIGHT_PP = 7.0
# Per-side cap BEFORE the shared clamp. It is deliberately above 7.0 and is
# therefore inert today -- the app clamp always binds first. Kept non-trivial
# so that if ADJUSTMENT_CAP_PP is ever raised, a team with a long injury list
# still has a bound of its own rather than growing without limit. NOT
# calibrated: the 2-out bucket was 9 games (-32.51pp, SE 14.67), which is not
# a number to build on.
MAX_TOTAL_PP = 14.0

# Minutes-per-game tiers -> multiplier on BASE_WEIGHT_PP. A WNBA starter plays
# roughly 28-34 MPG, so the 28+ tier is the one the calibration actually
# measured; the lower tiers are a reasonable ramp, not separately fitted.
#
# UNKNOWN MPG keeps a mid multiplier rather than the top or bottom one -- a
# rookie with no games yet, or a failed request, is genuinely unknown and must
# not be guessed in either direction. Same convention as the NBA module.
MPG_TIERS = ((28.0, 1.0), (20.0, 0.6), (12.0, 0.3))
LOW_MINUTE_MULTIPLIER = 0.15
UNKNOWN_MPG_MULTIPLIER = 0.5


def _severity_multiplier(mpg: float | None) -> float:
    if mpg is None:
        return UNKNOWN_MPG_MULTIPLIER
    for threshold, mult in MPG_TIERS:
        if mpg >= threshold:
            return mult
    return LOW_MINUTE_MULTIPLIER


def compute_injury_adjustment(
    home_injuries: list[dict],
    away_injuries: list[dict],
    player_mpg: dict[str, float] | None = None,
) -> NewsAdjustment | None:
    """home_injuries/away_injuries: {player_name, position, status,
    athlete_id} rows from espn_wnba_client.fetch_all_injuries().

    player_mpg: {player_name: minutes per game} for whichever injured players
    the poller managed to fetch season stats for. Optional -- anyone missing
    falls back to UNKNOWN_MPG_MULTIPLIER rather than being dropped or treated
    as a star.
    """
    player_mpg = player_mpg or {}
    factors: list[Factor] = []
    home_pp = away_pp = 0.0
    confidences: list[str] = []
    requires_review = False

    for injuries, side in ((home_injuries, "home"), (away_injuries, "away")):
        side_total = 0.0
        for inj in injuries or []:
            rule = STATUS_RULES.get(inj.get("status", ""))
            if rule is None:
                continue
            mpg = player_mpg.get(inj.get("player_name", ""))
            multiplier = _severity_multiplier(mpg)
            pp = BASE_WEIGHT_PP * multiplier * rule["severity"]
            side_total += pp
            confidences.append(rule["confidence"])
            # A genuinely key player whose status is not settled is exactly the
            # "major unknown" this flag exists for -- the same role a
            # questionable starting QB plays in the NFL module.
            if multiplier >= 1.0 and rule["confidence"] != "high":
                requires_review = True
            mpg_note = f", {mpg:.1f} MPG" if mpg is not None else ", minutes unknown"
            factors.append(
                Factor(
                    factor=(
                        f"{inj.get('player_name', 'Unknown')} "
                        f"({inj.get('position', '?')}) listed as {inj.get('status')}{mpg_note}"
                    ),
                    direction="favor_away" if side == "home" else "favor_home",
                    weight="minor" if pp < 2.0 else ("moderate" if pp < 5.0 else "major"),
                    rationale=(
                        f"{BASE_WEIGHT_PP}pp calibrated base x {multiplier:.2f} minutes-tier "
                        f"multiplier x {rule['severity']} severity for {inj.get('status')}."
                    ),
                )
            )
        # Cap each SIDE, not the net. Capping only the net would let one team's
        # long injury list silently cancel the other's, and the adjustment would
        # then understate how depleted both actually are.
        side_total = min(side_total, MAX_TOTAL_PP)
        if side == "home":
            home_pp = side_total
        else:
            away_pp = side_total

    if not factors:
        return None
    return NewsAdjustment(
        # Away injuries help the home side, so the net is away minus home.
        # clamp_adjustment takes the pp value, not the object -- it applies the
        # app-wide ADJUSTMENT_CAP_PP that every sport's news layer shares.
        adjustment_pct=clamp_adjustment(away_pp - home_pp),
        confidence=_overall_confidence(confidences),
        factors=factors,
        requires_review=requires_review,
    )


def _overall_confidence(confidences: list[str]) -> str:
    """Weakest link, not an average: one "Day-To-Day" star is the thing the
    reader most needs to discount, and averaging would bury it under a pile of
    confidently-out bench players."""
    if not confidences:
        return "low"
    order = {"low": 0, "medium": 1, "high": 2}
    return min(confidences, key=lambda c: order.get(c, 0))
