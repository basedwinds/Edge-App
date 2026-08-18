"""Cross-country CONMEBOL pricing -- Copa Libertadores and Copa Sudamericana,
using strength offsets fitted on real CONMEBOL results.

WHY A SECOND OFFSET MODULE RATHER THAN REUSING uefa_match. The mechanism is
identical (per-league ratings need a common scale before two clubs from
different countries can be compared) but the NUMBERS are not interchangeable:
uefa_match's offsets were fitted against a UEFA-wide baseline mu on European
results, with England pinned at zero. Feeding a Brazilian club through them
would ask a model fitted on one question to answer another -- the mistake this
project keeps rediscovering, most recently the CFB margin constants fitted on
the NFL's Elo scale. So this carries its own mu, its own reference league and
its own fitted offsets, exactly as leagues_cup_match.py does for MLS/Liga MX.

THE FIT (scripts/fit_conmebol_league_strength.py, 2026-08-18).
516 CROSS-COUNTRY matches across 5 seasons of Libertadores + Sudamericana, from
ESPN's own conmebol.libertadores / conmebol.sudamericana scoreboards, fetched a
month at a time because ESPN caps a scoreboard response at 100 events and a
season-wide window silently truncates. Same-country ties are excluded from the
fit: they teach nothing about the gap between leagues.

    BRA1  +0.000  (reference, pinned)      ECU1  -0.310
    ARG1  -0.121                           URU1  -0.422
    COL1  -0.310                           VEN1  -0.773

    mu = 1.0002,  home_advantage_log = +0.3727  (see home_advantage() below --
    the global +0.2000 was measurably too small for this competition, and the
    offsets barely moved when it was freed, which is what you want: the home
    term absorbed the residual instead of distorting the league scale)

THE RECOVERED ORDERING WAS NEVER SHOWN TO THE FIT. Brazil > Argentina >>
Colombia/Ecuador > Uruguay > Venezuela is the consensus South American club
hierarchy, and it fell out of goal data alone. That is the same reassurance the
UEFA fit had when it reproduced England > Spain/France/Germany/Italy >>
Portugal/Netherlands.

HELD OUT BY SEASON, AND HONESTLY 4 OF 5, NOT 5 OF 5:

    held-out    n    no offsets   fitted     gain
        2022   87       2.5787    2.1364   +0.4423
        2023  124       2.5588    2.2060   +0.3528
        2024  101       2.7098    2.2542   +0.4557
        2025  109       2.5530    2.6910   -0.1380   <-- does NOT transfer
        2026   95       2.2476    2.0495   +0.1981

Pooled over all 516 held-out matches the offsets still win clearly, mean Poisson
deviance 2.5332 -> 2.2773. The 2025 season going the wrong way is recorded
rather than smoothed over: on a sign test alone 4 of 5 is P=0.19, so the case
rests on the pooled improvement and the recovered ordering, not on the count.

Freeing home advantage on top of that takes the pooled held-out figure to
2.2615 (again 4 of 5 seasons). That is a SMALL gain and is reported as small.

WHAT THIS DELIBERATELY WILL NOT PRICE.

  * TWO-LEGGED TIES. CONMEBOL knockout rounds are two legs plus penalties, so an
    "advance" market depends on an aggregate across two matches this module
    knows nothing about. predict_conmebol_match returns a SINGLE-match
    distribution; callers must not derive an advance probability from it. Same
    rule, same reason, as uefa_match.
  * ANY CLUB WITHOUT AN OFFSET. Only six South American leagues are both rated
    here and present in CONMEBOL often enough to fit. Chile, Paraguay, Bolivia
    and Peru have no rating pool at all, so their clubs return None rather than
    being priced off an assumed-average league. That is roughly half the live
    field in a given round, and it is the correct answer for them.
  * NO AWAY-GOALS RULE. CONMEBOL abolished it in 2023. Nothing here implements
    one, and nothing should.

model_validated stays False. The offsets are CALIBRATED -- they predict goals
better than pretending the six leagues are equal -- which is a different and
weaker claim than beating a market price.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from app.models.baseline.elo_soccer import (
    HOME_ADVANTAGE_LOG, MatchGoalDistribution, SoccerRatingState, _build_grid,
)

log = logging.getLogger(__name__)

_STRENGTH_PATH = Path(__file__).resolve().parents[3] / "data" / "soccer_conmebol_strength.json"
_cache: dict = {"loaded": False, "mu": None, "offsets": {}, "hfa": None}


def load_strength(force: bool = False) -> tuple[float | None, dict]:
    """(mu, offsets). mu is None when the file is missing or unreadable, which
    makes every caller return None -- a clearly-flagged refusal rather than a
    silently wrong price off some default scale."""
    if _cache["loaded"] and not force:
        return _cache["mu"], _cache["offsets"]
    _cache["loaded"] = True
    try:
        raw = json.loads(_STRENGTH_PATH.read_text(encoding="utf-8"))
        _cache["mu"] = float(raw["mu"])
        _cache["offsets"] = {str(k): float(v) for k, v in raw["offsets"].items()}
        # Falls back to the global constant if an older file has no fitted term,
        # so a stale JSON degrades to the previous behaviour rather than to zero
        # home advantage.
        _cache["hfa"] = float(raw.get("home_advantage_log", HOME_ADVANTAGE_LOG))
    except (OSError, ValueError, KeyError, TypeError) as exc:  # noqa: B014
        log.error("CONMEBOL strength file unreadable at %s (%s) -- Libertadores and "
                  "Sudamericana will price as unrated", _STRENGTH_PATH, exc)
        _cache["mu"], _cache["offsets"], _cache["hfa"] = None, {}, HOME_ADVANTAGE_LOG
    return _cache["mu"], _cache["offsets"]


def home_advantage() -> float:
    """CONMEBOL's own fitted home term.

    THE GLOBAL CONSTANT IS TOO SMALL HERE, measured not assumed: with home
    advantage held at the global +0.2000, the model under-predicted the home
    side by +0.152 goals/game across 388 CONMEBOL home matches. Re-fitting it as
    a free parameter gives +0.3727 -- a 1.19x home goal multiplier -- and
    improves pooled held-out Poisson deviance 2.2773 -> 2.2615 on 516 matches,
    4 of 5 seasons.

    NOT AN ALTITUDE TERM, and that was the first hypothesis. Quito (2,850m) does
    show a high home residual (+0.60/game) but so do sea-level Racing Club
    (+0.82), Fortaleza (+0.81) and Internacional (+0.73), while Tolima at 1,285m
    is NEGATIVE (-0.25). The effect is competition-wide -- travel, hostile
    crowds, refereeing -- not venue elevation, and fitting one number is
    therefore the honest shape. A per-venue altitude term would need its own
    evidence; this data does not provide it."""
    load_strength()
    hfa = _cache.get("hfa")
    return HOME_ADVANTAGE_LOG if hfa is None else float(hfa)


@dataclass
class ConmebolMatchPrediction:
    distribution: MatchGoalDistribution
    home_league: str
    away_league: str
    strength_gap: float

    def prob_home_win(self) -> float:
        return self.distribution.prob_home_win()

    def prob_draw(self) -> float:
        return self.distribution.prob_draw()

    def prob_away_win(self) -> float:
        return self.distribution.prob_away_win()

    def prob_total_over(self, line: float) -> float:
        return self.distribution.prob_total_over(line)


def predict_conmebol_match(
    home_team: str, home_league: str,
    away_team: str, away_league: str,
    states_by_league: dict[str, SoccerRatingState],
) -> ConmebolMatchPrediction | None:
    """A SINGLE Libertadores/Sudamericana match.

    Returns None unless BOTH clubs are rated in a league carrying a fitted
    offset. The lambdas are built exactly as the fit defined them rather than
    through predict_match, because predict_match multiplies by that league's own
    league_avg_goals() while these offsets were estimated against one shared
    CONMEBOL baseline -- applying them on a different scale than they were fitted
    on is the same class of error as the CFB margin constants."""
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
        + home_advantage() + gap
    )
    lam_away = mu * math.exp(
        away_state.get_attack(away_team) + home_state.get_concede(home_team) - gap
    )
    return ConmebolMatchPrediction(
        distribution=MatchGoalDistribution(
            expected_home_goals=lam_home,
            expected_away_goals=lam_away,
            grid=_build_grid(lam_home, lam_away),
        ),
        home_league=home_league,
        away_league=away_league,
        strength_gap=gap,
    )
