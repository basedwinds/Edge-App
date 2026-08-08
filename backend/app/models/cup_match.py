"""Domestic cup pricing -- knockout ties between clubs in DIFFERENT divisions
of the same country (Coppa Italia I1 v I2, DFB Pokal D1 v D2).

TWO THINGS A LEAGUE MATCH DOES NOT NEED.

1. A CROSS-TIER RATING BRIDGE. Soccer ratings in this app are per-league by
design (see elo_service_soccer's docstring): a Serie A club's attack rating is
relative to Serie A's average, a Serie B club's to Serie B's, so the two are not
comparable and predict_match cannot be handed one of each. season_sim_soccer
already carries the conversion -- PROMOTED_TEAM_ATTACK_LOG_DISCOUNT /
PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT, derived from 476 real promotion events -- and
scripts/check_cup_tier_bridge.py confirmed on 115 real cup ties that it
TRANSFERS to this use: Brier 0.25888 with no bridge vs 0.20890 with it, 3-4
standard errors, and genuinely out-of-sample since the constant had never seen a
cup fixture.

   KNOWN RESIDUAL BIAS, deliberately not "fixed". That same test found the
   in-sample optimum is about -0.45, steeper than the -0.2558 in use, and all
   four leave-one-cup-out folds independently chose a steeper value. But the
   refit did NOT survive hold-out (pooled gain +0.0089 against an SE of
   0.015-0.021, sign flipped on the DFB Pokal, fitted value unstable between
   -0.45 and -0.60), so refitting on 115 in-sample ties would repeat the racing
   attrition mistake. The constant stays, and the consequence is stated instead:
   the model probably OVERRATES the second-tier side, and because this app
   stakes where model > market, the residual points specifically at buying cup
   underdogs. Hence needs_caution below -- cross-tier ties are flagged, not
   trusted silently.

2. AN ADVANCE MODEL. "To Advance" is not "to win". A cup tie level after 90
minutes goes to extra time and then penalties, so P(advance) strictly exceeds
P(win in 90) for both sides and the two must not be priced off the same number.
Extra time is modelled as what it is -- 30 more minutes of the same match, so
the same Poisson intensities scaled by 30/90 -- which is the identical treatment
predict_half already gives to a 45-minute period. A shootout is scored at 0.500.

   WHY 0.500 AND NOT SOMETHING FITTED. Shootout outcomes are close to a coin
   flip, and the one robust asymmetry in the literature (an advantage to the
   team kicking first) is decided by a COIN TOSS at the shootout itself, so it
   is unknowable at pricing time and averages out. Fitting a home-shootout edge
   to this app's data would mean estimating a small effect from the handful of
   shootouts in two seasons of one cup. 0.500 is the honest value, and it is
   flagged here rather than buried.

   WHAT THIS LAYER IS AND IS NOT WORTH (scripts/check_cup_advance_model.py, 322
   real ties, 22% of them decided after 90 minutes). Against the naive method it
   gains +0.00092 Brier -- a tenth of one standard error, i.e. nothing. It is
   NOT an accuracy improvement and must not be sold as one. It is kept because
   the naive method is directionally WRONG in a way pooled Brier cannot see:
   renormalizing splits the draw mass proportionally, handing most of it to the
   favourite, while the real mechanism pulls toward 50/50 because a shootout is a
   coin flip. Across the favourite range that overstates a favourite by up to
   3.7pp and understates an underdog by up to 3.3pp (max disagreement 4.7pp).
   Against this app's 10pp edge gate that is nearly half the gate, which is
   enough to manufacture edges on favourites that do not exist. Bias control,
   not accuracy.

Nothing in this module invents a rating. A tie is priceable only when BOTH clubs
are already rated; callers get None otherwise, same rule as every other soccer
path.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from app.models.baseline.elo_soccer import (
    MAX_GOALS, MatchGoalDistribution, SoccerRatingState, _build_grid, predict_match,
)
from app.models.season_sim_soccer import (
    PROMOTED_TEAM_ATTACK_LOG_DISCOUNT, PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT,
)

# Extra time is 30 minutes against a 90-minute match. Same period-share scaling
# predict_half applies to a half, with no separate constant to tune.
EXTRA_TIME_SHARE = 30.0 / 90.0

# See module docstring -- a shootout is a coin flip whose one real asymmetry is
# assigned by a coin toss at the time.
SHOOTOUT_HOME_PROB = 0.500


@dataclass
class CupTiePrediction:
    """Everything a cup market needs, plus the honesty flags the UI shows."""
    regulation: MatchGoalDistribution
    prob_home_advance: float
    prob_away_advance: float
    cross_tier: bool
    needs_caution: bool
    caution_note: str | None

    def prob_home_win(self) -> float:
        return self.regulation.prob_home_win()

    def prob_draw(self) -> float:
        return self.regulation.prob_draw()

    def prob_away_win(self) -> float:
        return self.regulation.prob_away_win()

    def prob_total_over(self, line: float) -> float:
        """Regulation only. Kalshi's cup TOTAL markets settle on 90 minutes
        plus stoppage, NOT on extra time, so this must not use the ET grid."""
        return self.regulation.prob_total_over(line)


CAUTION_NOTE = (
    "Cross-division cup tie: the second-tier club's rating is converted onto the "
    "top-flight scale using a promotion-derived offset. That offset was measured on "
    "promoted clubs, who are stronger than a typical second-tier cup opponent, so the "
    "model may overrate the lower-division side here. Validated as better than no "
    "conversion, but not refit for cup use."
)


def bridged_state(
    top_state: SoccerRatingState,
    second_state: SoccerRatingState,
    second_tier_teams: set[str],
) -> SoccerRatingState:
    """A top-flight rating state with the named second-tier clubs injected at
    their bridged ratings, so predict_match can price a mixed-tier tie on one
    consistent scale. The original states are not mutated."""
    merged = copy.deepcopy(top_state)
    for team in second_tier_teams:
        if second_state.get_count(team) <= 0:
            continue  # never fabricate -- an unrated club stays unrated
        merged.attack_log[team] = second_state.get_attack(team) + PROMOTED_TEAM_ATTACK_LOG_DISCOUNT
        merged.concede_log[team] = second_state.get_concede(team) + PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT
        merged.match_counts[team] = second_state.get_count(team)
    return merged


def _advance_probs(dist: MatchGoalDistribution) -> tuple[float, float]:
    """P(home advances), P(away advances) for a single-leg knockout tie.

    P(advance) = P(win in 90)
               + P(level at 90) * [ P(win in ET) + P(level after ET) * shootout ]
    """
    p_home, p_draw, p_away = dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win()

    et = _build_grid(dist.expected_home_goals * EXTRA_TIME_SHARE,
                     dist.expected_away_goals * EXTRA_TIME_SHARE)
    et_home = sum(et[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if h > a)
    et_away = sum(et[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if h < a)
    et_draw = max(0.0, 1.0 - et_home - et_away)

    home_after_draw = et_home + et_draw * SHOOTOUT_HOME_PROB
    away_after_draw = et_away + et_draw * (1.0 - SHOOTOUT_HOME_PROB)
    return (p_home + p_draw * home_after_draw,
            p_away + p_draw * away_after_draw)


def predict_cup_tie(
    home_team: str,
    away_team: str,
    top_state: SoccerRatingState,
    second_state: SoccerRatingState | None,
    second_tier_teams: set[str] | None = None,
) -> CupTiePrediction | None:
    """Price a single-leg domestic cup tie. Returns None unless BOTH clubs are
    rated -- in their own tier, after bridging.

    second_tier_teams names which of the two sides (if any) come from the
    second division; pass an empty set for an all-top-flight tie, which needs
    no bridge and carries no caution.
    """
    second_tier_teams = second_tier_teams or set()
    both_second = {home_team, away_team} <= second_tier_teams
    cross_tier = bool(second_tier_teams) and not both_second

    if both_second:
        # BOTH clubs are second-tier -- a Serie B v Serie B cup tie. There is no
        # cross-tier gap to bridge here, and bridging would be actively wrong:
        # it would discount both sides onto a scale neither of them is on, and
        # raise a caution the tie does not warrant. Price it in its own division,
        # exactly like a league match. (Caught 2026-08-08 when Monza v Avellino
        # priced as cross-tier after the staleness gate correctly moved Monza
        # from its stale Serie A rating to its current Serie B one.)
        if second_state is None:
            return None
        state = second_state
    elif cross_tier:
        if second_state is None:
            return None
        state = bridged_state(top_state, second_state, second_tier_teams)
    else:
        state = top_state

    for team in (home_team, away_team):
        if state.get_count(team) <= 0:
            return None  # unrated -- never price off a fabricated baseline

    dist = predict_match(state, home_team, away_team)
    home_adv, away_adv = _advance_probs(dist)
    return CupTiePrediction(
        regulation=dist,
        prob_home_advance=home_adv,
        prob_away_advance=away_adv,
        cross_tier=cross_tier,
        needs_caution=cross_tier,
        caution_note=CAUTION_NOTE if cross_tier else None,
    )
