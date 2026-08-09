"""Price a single national-team match.

The ratings themselves live in the ordinary soccer pool under the league code
"INTL" (see ingestion/international_data.py), so this module adds exactly one
thing on top of the standard match distribution: a refusal to price ACROSS
CONFEDERATIONS.

WHY THAT REFUSAL IS THE WHOLE POINT. The INTL pool looks like one rating pool
and is really six that barely touch. Confederation qualifying is a closed
competition: CONMEBOL is a round-robin among ten strong South American sides, so
Brazil never plays a minnow, while AFC qualifying gives Vietnam a steady diet of
them. Almost nothing connects the groups, so the goal-scaling between them was
never pinned down -- and the fitted ratings say so out loud. Measured
2026-08-09, on 2,421 competitive internationals:

    brazil      attack -0.005    argentina   attack +0.063
    vietnam     attack +0.190    thailand    attack +0.154

Read literally that claims Vietnam has a better attack than Brazil. It does not;
it means the two numbers are denominated in different currencies. Within a
confederation the ratings are sound -- those teams play each other constantly,
which is exactly the connectivity the pooling assumption needs -- so an
all-AFC fixture like the ASEAN Championship is fine, and Brazil vs Vietnam is
not.

WHY IT IS NOT FIXED WITH AN OFFSET, the way uefa_match.py and
leagues_cup_match.py fix the club version. Those offsets are fitted on real
cross-league matches: UEFA competition and the Leagues Cup respectively supply
hundreds of them. The equivalent here would be inter-confederation fixtures, and
outside a World Cup those are almost entirely FRIENDLIES -- which
check_club_friendlies_signal.py measured this model to be WORSE than a
knows-nothing baseline at predicting, and which international_data therefore
excludes from training. Fitting a confederation offset on matches the model
cannot predict would produce a confident number with nothing behind it. The
honest answer is to refuse the fixture until a real source of competitive
cross-confederation results exists.

model_validated stays False. These ratings have never been scored against a
market or against held-out results.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ingestion import international_data
from app.models.baseline.elo_soccer import MatchGoalDistribution, SoccerRatingState

INTL_LEAGUE = "INTL"
# Below this a national team's rating is noise. Chosen to match the level the
# feasibility check reported as comfortably available (178 of 211 teams cleared
# 6 matches), so it excludes the genuinely unrated without excluding real
# fixtures.
MIN_MATCHES = 6

_cache: dict = {"loaded": False, "confederation": {}}


def confederation_map(force: bool = False) -> dict:
    if _cache["loaded"] and not force:
        return _cache["confederation"]
    _cache["loaded"] = True
    try:
        _cache["confederation"] = international_data.confederation_by_team()
    except Exception:
        _cache["confederation"] = {}
    return _cache["confederation"]


@dataclass
class NationalMatchPrediction:
    distribution: MatchGoalDistribution
    confederation: str

    def prob_home_win(self) -> float:
        return self.distribution.prob_home_win()

    def prob_draw(self) -> float:
        return self.distribution.prob_draw()

    def prob_away_win(self) -> float:
        return self.distribution.prob_away_win()

    def prob_total_over(self, line: float) -> float:
        return self.distribution.prob_total_over(line)


def predict_national_match(
    home_team: str, away_team: str, state: SoccerRatingState | None,
) -> NationalMatchPrediction | None:
    """One national-team match. Returns None -- rather than a number -- when the
    two sides are from different confederations, when either is unrated, or when
    either is below MIN_MATCHES."""
    if state is None:
        return None
    if state.get_count(home_team) < MIN_MATCHES or state.get_count(away_team) < MIN_MATCHES:
        return None

    confs = confederation_map()
    home_conf, away_conf = confs.get(home_team), confs.get(away_team)
    if not home_conf or not away_conf or home_conf != away_conf:
        return None  # see the module docstring: not comparable, so not priced

    from app.models.baseline.elo_soccer import predict_match
    dist = predict_match(state, home_team, away_team)
    if dist is None:
        return None
    return NationalMatchPrediction(distribution=dist, confederation=home_conf)
