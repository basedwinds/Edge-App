"""MLS Cup Playoffs Monte Carlo -- the model behind Kalshi's KXMLSCUP /
KXMLSEAST / KXMLSWEST futures.

WHY THIS IS A SEPARATE MODULE FROM season_sim_soccer.py. That module's own
docstring says MLS is out of scope, and it is right for the reason it gives:
its whole design assumes a DOUBLE ROUND-ROBIN, so it needs no fixture calendar
at all (who plays whom is determined by who is in the league). MLS breaks that
assumption twice over:

  1. Unbalanced schedule -- 30 teams, 34 games. A team plays its own conference
     far more often than the other one, so "every team plays every other home
     and away" is not an approximation of the MLS season, it is a different
     season. The REAL remaining fixture list is required.
  2. Mid-season pricing -- MLS runs Feb->Oct, so unlike the five European
     leagues (whose futures are priced before a ball is kicked, where a
     from-scratch replay IS correct), an MLS futures price in August must carry
     the points ALREADY BANKED. Replaying the season from 0-0 would price a
     runaway leader as if it were level on points with everyone.

So this module simulates only what is left, on top of the real current table,
and then runs the actual bracket.

THE FORMAT WAS DERIVED FROM DATA, NOT ASSUMED. Rather than hard-code a
remembered format, the 2025 postseason was read back out of this app's own
ESPN match cache (data/espn_mls_matches_cache.json) and the structure fell out
of it (measured 2026-08-07):

  - 18 distinct teams appear after the regular season ends -> 9 per conference
    qualify, which is what PLAYOFF_TEAMS_PER_CONFERENCE encodes.
  - Games-per-pairing histogram across the postseason: {1 game: 11 pairings,
    2 games: 3, 3 games: 2}. A pairing that can play 2 OR 3 times and never
    more is a best-of-three; a pairing that plays exactly once is single
    elimination. That mixture is the signature of exactly one format -- a
    best-of-3 Round One sitting inside an otherwise single-elimination bracket
    -- and it is why ROUND_ONE_WINS_NEEDED is 2 while every other round here
    resolves in one game.
  - The bracket ends on a single December fixture (the MLS Cup final).

DRAWS. Soccer knockouts cannot end level, and MLS resolves a level knockout
game (including each individual Round One game) on penalty kicks with no extra
time. Penalties are modelled as a coin flip. That is not a shrug: shootouts are
the one part of soccer with no established, data-supported skill signal at this
sample size, and a 50/50 is the honest prior. It only fires on the draw branch,
which the Poisson grid already prices explicitly.

Everything here ships model_validated=False like every other model in this app.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.models.baseline.elo_soccer import SoccerRatingState, predict_match

# Reuses season_sim_soccer's grid-flattener rather than re-deriving it. Private
# by name, but this is the same package and the same Poisson grid -- copying it
# would mean two places to fix if the goal model ever changes shape.
from app.models.season_sim_soccer import _cumulative_weights

EAST = "east"
WEST = "west"

# Derived from the 2025 postseason (18 teams, 9 a side) -- see module docstring.
PLAYOFF_TEAMS_PER_CONFERENCE = 9

# Round One is best-of-3 (derived from the games-per-pairing histogram).
ROUND_ONE_WINS_NEEDED = 2

# Standard soccer tiebreak, same as season_sim_soccer: points, goal difference,
# goals for. MLS's real tiebreak list continues past this (wins, then further
# rules), which this does not model -- it only matters on an exact three-way tie
# and moves a seed, not a champion.
_TIEBREAK = lambda r: (-r.points, -r.goal_diff, -r.goals_for)  # noqa: E731


@dataclass
class MlsTeamState:
    """One team's REAL current table position, plus which conference it sits
    in. Fed from the live ESPN standings feed, not from a simulation."""
    team: str
    conference: str
    points: int = 0
    goal_diff: int = 0
    goals_for: int = 0


@dataclass
class _SimTeam:
    team: str
    points: int
    goal_diff: int
    goals_for: int


@dataclass
class MlsPlayoffResult:
    teams: list[str]
    # P(win the MLS Cup) -- prices KXMLSCUP.
    cup_champion_prob: dict = field(default_factory=dict)
    # P(win your conference's playoff bracket) -- prices KXMLSEAST/KXMLSWEST.
    # Confirmed against Kalshi's own rules_primary for KXMLSEAST-26-TOR
    # ("...is the 2026 MLS Eastern Conference champion"): these resolve on the
    # PLAYOFF bracket, not on the regular-season conference table. Pricing them
    # off the table would be a different question with a different answer.
    conference_champion_prob: dict = field(default_factory=dict)
    # P(make the playoffs at all). Not currently a Kalshi market, but it is a
    # free by-product of stage one and is what makes the sim inspectable -- a
    # bracket number that looks wrong is usually a seeding number that is wrong.
    playoff_berth_prob: dict = field(default_factory=dict)
    unrated_teams: list[str] = field(default_factory=list)
    n_simulations: int = 0


def _play_once(rng, pairings, home: str, away: str) -> str:
    """One knockout game. Returns the winner. A draw goes to penalties, modelled
    as a coin flip (see module docstring)."""
    outcomes, cum = pairings[(home, away)]
    idx = min(int(_bisect(cum, rng.random() * cum[-1])), len(outcomes) - 1)
    h, a = outcomes[idx]
    if h > a:
        return home
    if a > h:
        return away
    return home if rng.random() < 0.5 else away


def _bisect(cum, x):
    import bisect as _b
    return _b.bisect_left(cum, x)


def _play_series(rng, pairings, higher: str, lower: str) -> str:
    """Round One best-of-3. The higher seed hosts games 1 and 3, the lower seed
    hosts game 2 -- the real MLS home pattern, and it matters because the goal
    model prices home advantage explicitly, so getting it backwards would hand
    the wrong side an edge in every Round One series."""
    hw = lw = 0
    for game in range(3):
        home, away = (higher, lower) if game != 1 else (lower, higher)
        if _play_once(rng, pairings, home, away) == higher:
            hw += 1
        else:
            lw += 1
        if hw == ROUND_ONE_WINS_NEEDED or lw == ROUND_ONE_WINS_NEEDED:
            break
    return higher if hw > lw else lower


def _run_conference_bracket(rng, pairings, seeds: list[str]) -> str:
    """seeds[0] is the 1 seed. Returns the conference champion.

    Bracket shape (real MLS): a single-game Wild Card between the 8 and 9 seeds,
    then best-of-3 Round One as 1v8 / 2v7 / 3v6 / 4v5, then single-game
    Conference Semifinals pairing the 1/8 winner with the 4/5 winner and the 2/7
    winner with the 3/6 winner, then a single-game Conference Final. The higher
    seed hosts every single-game round."""
    rank = {t: i for i, t in enumerate(seeds)}

    def higher_of(a: str, b: str) -> tuple[str, str]:
        return (a, b) if rank[a] < rank[b] else (b, a)

    def single(a: str, b: str) -> str:
        hi, lo = higher_of(a, b)
        return _play_once(rng, pairings, hi, lo)

    # Wild Card: 8 vs 9, hosted by the 8 seed. Winner takes the 8 slot.
    eight = _play_once(rng, pairings, seeds[7], seeds[8])

    r1 = [
        _play_series(rng, pairings, seeds[0], eight),
        _play_series(rng, pairings, seeds[3], seeds[4]),
        _play_series(rng, pairings, seeds[1], seeds[6]),
        _play_series(rng, pairings, seeds[2], seeds[5]),
    ]
    semi_top = single(r1[0], r1[1])
    semi_bot = single(r1[2], r1[3])
    return single(semi_top, semi_bot)


def simulate_mls_postseason(
    state: SoccerRatingState,
    table: list[MlsTeamState],
    remaining_fixtures: list[tuple[str, str]],
    n_simulations: int = 3000,
    seed: int | None = None,
) -> MlsPlayoffResult:
    """`table` is the REAL current standings (one row per team, with the points
    already banked). `remaining_fixtures` is the REAL list of unplayed regular
    season games as (home, away) canonical team keys -- both come from ESPN, and
    both are required: see the module docstring on why a round-robin stand-in
    does not work for MLS.

    Every pairing's goal distribution is computed ONCE and resampled, not
    recomputed per simulation -- same optimisation season_sim_soccer uses. The
    playoff bracket can pair any two teams in a conference (and any two across
    conferences in the final), so this precomputes all ordered pairs rather than
    only the fixtures actually scheduled."""
    rng = random.Random(seed)
    teams = [t.team for t in table]
    unrated = [t for t in teams if state.get_count(t) == 0]
    if unrated:
        # MLS measured 30/30 rated on 2026-08-07, so this is a guard against a
        # future expansion club, not a live condition. Bail rather than invent a
        # rating: an unrated team in the bracket silently distorts EVERY other
        # team's probability, not just its own.
        return MlsPlayoffResult(teams=teams, unrated_teams=unrated, n_simulations=0)

    pairings: dict[tuple[str, str], tuple[list[tuple[int, int]], list[float]]] = {}
    for home in teams:
        for away in teams:
            if home == away:
                continue
            pairings[(home, away)] = _cumulative_weights(predict_match(state, home, away).grid)

    conference = {t.team: t.conference for t in table}
    cup = {t: 0 for t in teams}
    conf_champ = {t: 0 for t in teams}
    berth = {t: 0 for t in teams}

    for _ in range(n_simulations):
        sim = {t.team: _SimTeam(t.team, t.points, t.goal_diff, t.goals_for) for t in table}

        for home, away in remaining_fixtures:
            outcomes, cum = pairings[(home, away)]
            idx = min(_bisect(cum, rng.random() * cum[-1]), len(outcomes) - 1)
            h, a = outcomes[idx]
            sim[home].goals_for += h
            sim[away].goals_for += a
            sim[home].goal_diff += h - a
            sim[away].goal_diff += a - h
            if h > a:
                sim[home].points += 3
            elif a > h:
                sim[away].points += 3
            else:
                sim[home].points += 1
                sim[away].points += 1

        champs = {}
        for conf in (EAST, WEST):
            ranked = sorted((sim[t] for t in teams if conference[t] == conf), key=_TIEBREAK)
            seeds = [r.team for r in ranked[:PLAYOFF_TEAMS_PER_CONFERENCE]]
            for t in seeds:
                berth[t] += 1
            champs[conf] = _run_conference_bracket(rng, pairings, seeds)
            conf_champ[champs[conf]] += 1

        # MLS Cup: the finalist with the better regular-season record hosts.
        e, w = champs[EAST], champs[WEST]
        hi, lo = (e, w) if _TIEBREAK(sim[e]) <= _TIEBREAK(sim[w]) else (w, e)
        cup[_play_once(rng, pairings, hi, lo)] += 1

    n = float(n_simulations)
    return MlsPlayoffResult(
        teams=teams,
        cup_champion_prob={t: cup[t] / n for t in teams},
        conference_champion_prob={t: conf_champ[t] / n for t in teams},
        playoff_berth_prob={t: berth[t] / n for t in teams},
        unrated_teams=[],
        n_simulations=n_simulations,
    )
