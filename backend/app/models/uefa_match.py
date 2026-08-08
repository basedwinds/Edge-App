"""Cross-country UEFA pricing -- Champions League, Europa League, Conference
League, using the fitted per-league strength offsets.

WHY THIS EXISTS. Soccer ratings here are per-league: an attack rating says how
many goals a club scores relative to ITS OWN league's average, so a 1.2 in the
Eredivisie and a 1.2 in La Liga mean different things and predict_match cannot
be handed one of each. cup_match.py solved the same problem inside one country
using the promotion-derived tier bridge. This solves it across countries using
offsets fitted on real UEFA results (scripts/fit_uefa_league_strength.py).

THE MODEL IS THE ONE THAT WAS FITTED, not a re-derivation. predict_match is
deliberately NOT reused, because it multiplies by that league's own
league_avg_goals(), whereas the offsets were fitted against a single UEFA-wide
baseline mu. Reusing it would apply the offsets on top of a different scale than
the one they were estimated on -- the "fitted on one question, asked another"
mistake this project keeps finding. So the lambdas are built here exactly as the
fit defined them:

    lambda_home = mu * exp(attack_h + concede_a + HFA + (s_H - s_A))
    lambda_away = mu * exp(attack_a + concede_h       + (s_A - s_H))

WHAT THE OFFSETS ARE WORTH (see the fit script's own docstring for the full
table). Fitted on 583 cross-country matches over 3 seasons, held out BY SEASON,
and they transferred every time: +0.193, +0.192, +0.297 Poisson deviance on the
three unseen seasons. The recovered ordering -- England > Spain/France/Germany/
Italy >> Portugal/Netherlands > Turkey/Belgium > Greece -- matches the consensus
European hierarchy, which the fit was never shown.

WHAT THIS DELIBERATELY WILL NOT PRICE.

  * TWO-LEGGED TIES. UEFA knockout rounds are decided over two legs plus
    possible extra time, and an "advance" market therefore depends on an
    aggregate score across two matches this module knows nothing about.
    cup_match's single-leg advance formula is WRONG here and is not reused.
    predict_uefa_match returns a single-match distribution only; callers must
    not derive an advance probability from it. The league phase, which is all
    single matches, is unaffected -- and it is the large majority of inventory.
  * ANY CLUB WITHOUT AN OFFSET. Only 10 leagues appear in UEFA often enough to
    fit one. A club from anywhere else returns None rather than being priced
    off an assumed-average league, which would silently treat a Kazakh side as
    a Bundesliga side.

model_validated stays False. The offsets are CALIBRATED -- they predict goals
better than pretending leagues are equal -- which is a different and weaker
claim than beating a market price.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from app.models.baseline.elo_soccer import (
    HOME_ADVANTAGE_LOG, MatchGoalDistribution, SoccerRatingState, _build_grid,
)

_STRENGTH_PATH = Path(__file__).resolve().parents[3] / "data" / "soccer_league_strength.json"
_cache: dict = {"loaded": False, "mu": None, "offsets": {}}

# The fit's own baseline, used only if the file is missing so that a caller gets
# a clearly-flagged None rather than a silently wrong price.
_FALLBACK_MU = None


def load_strength(force: bool = False) -> tuple[float | None, dict]:
    if _cache["loaded"] and not force:
        return _cache["mu"], _cache["offsets"]
    _cache["loaded"] = True
    try:
        blob = json.loads(_STRENGTH_PATH.read_text(encoding="utf-8"))
        _cache["mu"] = float(blob["mu"])
        _cache["offsets"] = {k: float(v) for k, v in blob["offsets"].items()}
    except Exception:
        _cache["mu"], _cache["offsets"] = _FALLBACK_MU, {}
    return _cache["mu"], _cache["offsets"]


@dataclass
class UefaMatchPrediction:
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


def predict_uefa_match(
    home_team: str, home_league: str,
    away_team: str, away_league: str,
    states_by_league: dict[str, SoccerRatingState],
) -> UefaMatchPrediction | None:
    """A SINGLE UEFA match. Returns None unless both clubs are rated in a league
    that has a fitted offset -- never prices off an assumed league strength."""
    mu, offsets = load_strength()
    if mu is None:
        return None
    if home_league not in offsets or away_league not in offsets:
        return None

    home_state = states_by_league.get(home_league)
    away_state = states_by_league.get(away_league)
    if home_state is None or away_state is None:
        return None
    if home_state.get_count(home_team) <= 0 or away_state.get_count(away_team) <= 0:
        return None

    gap = offsets[home_league] - offsets[away_league]
    lam_home = mu * math.exp(
        home_state.get_attack(home_team) + away_state.get_concede(away_team)
        + HOME_ADVANTAGE_LOG + gap
    )
    lam_away = mu * math.exp(
        away_state.get_attack(away_team) + home_state.get_concede(home_team) - gap
    )
    return UefaMatchPrediction(
        distribution=MatchGoalDistribution(
            expected_home_goals=lam_home,
            expected_away_goals=lam_away,
            grid=_build_grid(lam_home, lam_away),
        ),
        home_league=home_league,
        away_league=away_league,
        strength_gap=gap,
    )
