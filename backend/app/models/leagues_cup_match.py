"""Price a single Leagues Cup match (MLS vs Liga MX).

WHY A SEPARATE MODULE FROM uefa_match.py. The shape is the same -- per-league
attack/concede ratings are not comparable across leagues, so a cross-league match
needs a fitted strength offset -- but two things differ enough that sharing the
code would smuggle a wrong constant into one of them.

  1. VENUE. UEFA ties are real home-and-away fixtures and use the domestic home
     advantage. The Leagues Cup is not: the 2023 and 2024 editions were played
     entirely in the United States and Canada, so ESPN's "home" competitor is
     often a DESIGNATION rather than a host. This was not assumed -- the fit
     estimated the venue term from the results and got +0.0071 log against a
     domestic constant of +0.2624, i.e. essentially nothing. Reusing the
     domestic value would have applied a ~30% scoring boost that does not exist,
     and the league offset would have quietly absorbed the error, leaving an
     offset that was wrong for every true Liga MX home match.

  2. THE POOL. uefa_match's offsets cover 10 European leagues fitted on European
     competition. Nothing in that fit says anything about MLS or Liga MX, and
     pooling the two sets would imply a comparability between, say, Ligue 1 and
     Liga MX that was never measured. This module refuses any pairing that is
     not MLS vs Liga MX for exactly that reason.

WHAT THE OFFSET IS WORTH. Fitted on 172 completed cross-league matches from the
2023-2026 editions, held out BY SEASON. Against the honest baseline -- pretend
the two leagues are equal -- it improved Poisson deviance in ALL FOUR held-out
seasons with the domestic-HFA variant (+0.1040 weighted mean) and in three of
four with the fitted-venue variant (+0.1387 weighted mean, losing only the 2026
fold, which is also the smallest at n=24 and still in progress).

Coverage, which is what usually kills these projects, is a non-issue here: the
field IS the two leagues this app already rates, and a sweep of four editions
resolved every club with zero unresolved names.

WHAT THIS DELIBERATELY WILL NOT DO.

  * PRICE A KNOCKOUT "ADVANCE" MARKET. Leagues Cup knockout matches go straight
    to a shootout when level, with no extra time. A single-match goal
    distribution cannot express that, and cup_match's extra-time formula is
    WRONG here rather than merely approximate. Callers get a single-match
    distribution only.
  * PRICE AN MLS-vs-MLS OR MEX1-vs-MEX1 MATCH. Those are same-league fixtures
    that the ordinary domestic model already handles correctly, at a real venue
    with a real home advantage. Sending them here would apply a neutral-venue
    assumption to a normal home game.
  * PRICE ANY OTHER LEAGUE. Returns None rather than treating an unfitted pool
    as league-average.

model_validated stays False. The offset is CALIBRATED -- it predicts goals
better than assuming the leagues are equal -- which is a weaker claim than
beating a market price, and is not evidence of an edge.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from app.models.baseline.elo_soccer import (
    MatchGoalDistribution, SoccerRatingState, _build_grid,
)

_STRENGTH_PATH = Path(__file__).resolve().parents[3] / "data" / "leagues_cup_strength.json"
_SUPPORTED = frozenset({"MLS", "MEX1"})
_cache: dict = {"loaded": False, "mu": None, "home_log": 0.0, "offsets": {}}


def load_strength(force: bool = False) -> tuple[float | None, float, dict]:
    """(mu, home_log, offsets). mu is None when the fit has never been run, which
    makes every prediction return None rather than fall back to a guess."""
    if _cache["loaded"] and not force:
        return _cache["mu"], _cache["home_log"], _cache["offsets"]
    _cache["loaded"] = True
    try:
        blob = json.loads(_STRENGTH_PATH.read_text(encoding="utf-8"))
        _cache["mu"] = float(blob["mu"])
        _cache["home_log"] = float(blob.get("home_log", 0.0))
        _cache["offsets"] = {k: float(v) for k, v in blob["offsets"].items()}
    except Exception:
        _cache["mu"], _cache["home_log"], _cache["offsets"] = None, 0.0, {}
    return _cache["mu"], _cache["home_log"], _cache["offsets"]


# A shootout, priced as a coin flip -- named so it can be tested rather than
# argued about, same as two_leg_tie.SHOOTOUT_HOME_PROB.
SHOOTOUT_SPLIT = 0.5


@dataclass
class LeaguesCupPrediction:
    distribution: MatchGoalDistribution
    home_league: str
    away_league: str
    strength_gap: float  # s_home - s_away, positive = home league stronger

    def prob_home_win(self) -> float:
        return self.distribution.prob_home_win()

    def prob_draw(self) -> float:
        return self.distribution.prob_draw()

    def prob_away_win(self) -> float:
        return self.distribution.prob_away_win()

    def prob_total_over(self, line: float) -> float:
        return self.distribution.prob_total_over(line)

    # ---- ADVANCE (2026-08-18) ----------------------------------------------
    # EXACT for this competition, not an approximation, and that is only true
    # because of the format note at the top of this module: the Leagues Cup
    # knockout goes STRAIGHT TO A SHOOTOUT when level, with NO extra time. So
    # there is no ET grid to convolve and no aggregate across legs -- a single
    # match, and a coin flip for whatever it leaves level.
    #
    # This is deliberately NOT models/two_leg_tie.py, which exists for the
    # two-legged UEFA/CONMEBOL ties and would add an extra-time term this
    # competition does not play.
    def prob_home_advance(self) -> float:
        return self.prob_home_win() + self.prob_draw() * SHOOTOUT_SPLIT

    def prob_away_advance(self) -> float:
        return self.prob_away_win() + self.prob_draw() * SHOOTOUT_SPLIT


def predict_leagues_cup_match(
    home_team: str, home_league: str,
    away_team: str, away_league: str,
    states_by_league: dict[str, SoccerRatingState],
) -> LeaguesCupPrediction | None:
    """One Leagues Cup match. None unless this is a genuine MLS-vs-Liga MX
    pairing with both clubs actually rated."""
    mu, home_log, offsets = load_strength()
    if mu is None:
        return None
    # Cross-league ONLY, and only the two pools the offset was fitted on.
    if {home_league, away_league} != _SUPPORTED:
        return None
    if home_league not in offsets or away_league not in offsets:
        return None

    home_state = states_by_league.get(home_league)
    away_state = states_by_league.get(away_league)
    if home_state is None or away_state is None:
        return None
    # A club with no history is refused rather than priced off a default rating.
    if home_state.get_count(home_team) <= 0 or away_state.get_count(away_team) <= 0:
        return None

    gap = offsets[home_league] - offsets[away_league]
    lam_home = mu * math.exp(
        home_state.get_attack(home_team) + away_state.get_concede(away_team)
        + home_log + gap
    )
    lam_away = mu * math.exp(
        away_state.get_attack(away_team) + home_state.get_concede(home_team) - gap
    )
    return LeaguesCupPrediction(
        distribution=MatchGoalDistribution(
            expected_home_goals=lam_home,
            expected_away_goals=lam_away,
            grid=_build_grid(lam_home, lam_away),
        ),
        home_league=home_league,
        away_league=away_league,
        strength_gap=gap,
    )
