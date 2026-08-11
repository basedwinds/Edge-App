"""Liga MX Liguilla Monte Carlo -- the model behind Kalshi's KXLIGAMX futures.

WHY THIS IS A SEPARATE MODULE FROM season_sim_soccer.py. That module answers
"who finishes top of the table", and until now Liga MX was DELIBERATELY excluded
from LEAGUE_WINNER_SERIES for exactly that reason: Kalshi's markets ask "Will
<team> win the Liga MX Clausura?", and a torneo is won in the LIGUILLA -- a
knockout played after the table is decided -- not by topping the table. Pointing
the season model at it would have answered a different question, confidently.
This module answers the traded one.

THE FORMAT WAS DERIVED FROM DATA, NOT ASSUMED -- the same discipline
playoff_sim_mls.py used, and the reason task #145 (NASCAR's secondary titles)
is still blocked: a remembered rulebook is not a source. Read out of
football-data's Liga MX history (4,682 matches, 2012-2026) over the four most
recent completed torneos, measured 2026-08-11:

  - Every torneo is 18 teams and ~170 matches. A SINGLE round-robin of 18 teams
    is 153 games, so ~17 of them are postseason. Confirmed exactly on
    2025-CLAU: 153 regular + 17 postseason.
  - Games-per-pairing histogram is stable at {1: ~143, 2: ~3, 3: ~7}. A pairing
    that plays 3 times is one regular-season meeting plus a TWO-LEGGED tie;
    seven such pairings is 4 quarterfinals + 2 semifinals + 1 final. That is an
    8-team knockout in which every tie is home-and-away.
  - The handful of 2s are SINGLE-leg play-in games. Their seed pattern is
    consistent: 7v8 and 9v10 play first, then the 9v10 winner plays the 7v8
    loser. So seeds 7-10 contest the last two Liguilla places.
  - Quarterfinal seedings observed: 1v8, 2v7, 3v6, 4v5 (2025-APER exactly).
  - Semifinals RE-SEED rather than follow a fixed bracket: observed 1v5 / 2v3
    and 1v6 / 2v4, i.e. highest surviving seed against lowest.
  - AGGREGATE TIES GO TO THE HIGHER SEED. Verified by following level ties into
    the NEXT round rather than by assuming: 2025-CLAU 1v7, 3v5 and 2v4 all
    finished level and seeds 1, 3 and 2 are the ones that appear in the
    following round.

TWO TORNEOS PER YEAR, AND THEY NEED DIFFERENT TREATMENT. Kalshi lists both
(KXLIGAMX-27APER and KXLIGAMX-27CLA, 18 teams each). The Apertura is under way,
so it must be simulated from the points ALREADY BANKED -- replaying it from 0-0
would price a runaway leader level with everyone, the same error
playoff_sim_mls.py exists to avoid. The Clausura has not kicked off, so an empty
table is the correct input for it, exactly as the European leagues are priced
pre-season.

DRAWS. A two-legged tie level on aggregate is resolved by seed, which is a real
rule and needs no coin flip. A level SINGLE-leg play-in game has no aggregate to
fall back on and is resolved by seed as well -- the higher seed advances. Only
that branch is a modelling choice, it fires rarely, and it favours the side the
real competition also favours.

Ships model_validated=False like every other model in this app.
"""
from __future__ import annotations

import bisect
import random
from dataclasses import dataclass, field

from app.models.baseline.elo_soccer import SoccerRatingState, predict_match
from app.models.season_sim_soccer import _cumulative_weights

# Derived from the games-per-pairing histogram -- see module docstring.
LIGUILLA_TEAMS = 8
# Seeds 7..10 contest the final two places through single-leg play-in games.
PLAY_IN_LOW_SEED = 10

# Standard soccer tiebreak, same order season_sim_soccer and playoff_sim_mls
# use: points, goal difference, goals for. Liga MX's real list continues past
# this, but the remainder only ever moves a seed, never a champion.
def _tiebreak(r) -> tuple:
    return (-r.points, -r.goal_diff, -r.goals_for)


@dataclass
class LigaMxTeamState:
    """One team's REAL current table position in the torneo being priced.

    For a torneo that has not kicked off (the Clausura, priced months ahead)
    every field is 0 and the whole 153-game round-robin is simulated.
    """
    team: str
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
class LigaMxPlayoffResult:
    teams: list[str]
    # P(win the torneo) -- this is what KXLIGAMX resolves on.
    champion_prob: dict = field(default_factory=dict)
    # P(reach the 8-team Liguilla), including via the play-in. Not a Kalshi
    # market, but it is what makes a wrong champion number diagnosable: a bad
    # bracket is usually a bad seeding.
    liguilla_prob: dict = field(default_factory=dict)
    # P(finish top of the regular-season table). Liga MX does NOT award the
    # title for this, which is the whole reason this module exists -- exposed so
    # the difference between the two is visible rather than implied.
    table_top_prob: dict = field(default_factory=dict)
    unrated_teams: list[str] = field(default_factory=list)
    n_simulations: int = 0


def balanced_round_robin(teams: list[str]) -> list[tuple[str, str]]:
    """A single round-robin where every team hosts as close to half its games as
    the count allows -- for 18 teams that is 8 or 9 of 17.

    NEEDED because a torneo that has not kicked off has no published calendar,
    so the remaining-fixture list has to be synthesised. Balance is not cosmetic:
    the goal model prices home advantage explicitly, so a lopsided synthetic
    calendar hands real probability to whoever happens to host more. A naive
    greedy pass produced a 1-to-13 spread across 18 teams when this was written,
    which is why the counts are REPAIRED below rather than trusted.
    """
    out: list[list[str]] = []
    hosts: dict[str, int] = {t: 0 for t in teams}
    for i, a in enumerate(teams):
        for b in teams[i + 1:]:
            out.append([a, b])
    # Repair pass: flip the orientation of a game belonging to the team that
    # hosts most, in favour of a team that hosts least, until no pair of teams
    # differs by more than one. Terminates because each flip strictly reduces
    # the spread and the loop is bounded.
    for _ in range(len(out) * 4):
        for h, a in out:
            hosts[h] += 1
        hi = max(hosts, key=lambda t: hosts[t])
        lo = min(hosts, key=lambda t: hosts[t])
        if hosts[hi] - hosts[lo] <= 1:
            break
        for g in out:
            if g[0] == hi and g[1] == lo:
                g[0], g[1] = g[1], g[0]
                break
        else:
            # No direct hi-hosts-lo game to flip; take any game hi hosts.
            for g in out:
                if g[0] == hi:
                    g[0], g[1] = g[1], g[0]
                    break
        hosts = {t: 0 for t in teams}
    return [(h, a) for h, a in out]


def _play_once(rng, pairings, home: str, away: str) -> tuple[int, int]:
    """One game's goals as (home_goals, away_goals), resampled from the
    precomputed grid. Returns goals rather than a winner because a two-legged
    tie is decided on AGGREGATE, so the caller needs the scoreline."""
    outcomes, cum = pairings[(home, away)]
    idx = min(bisect.bisect_left(cum, rng.random() * cum[-1]), len(outcomes) - 1)
    return outcomes[idx]


def _play_tie(rng, pairings, higher: str, lower: str) -> str:
    """A two-legged Liguilla tie. The LOWER seed hosts the first leg and the
    higher seed hosts the second -- the real Liga MX pattern, and it matters
    because the goal model prices home advantage explicitly, so reversing it
    would hand the wrong side an edge in every tie.

    Level on aggregate -> the higher seed advances (derived, see docstring).
    Away goals are deliberately NOT applied: Liga MX abolished the away-goals
    rule, and the histogram cannot tell us either way, so the rule that IS
    confirmed by the next-round evidence is the one used.
    """
    h1, a1 = _play_once(rng, pairings, lower, higher)     # leg 1 at the lower seed
    h2, a2 = _play_once(rng, pairings, higher, lower)     # leg 2 at the higher seed
    higher_goals = a1 + h2
    lower_goals = h1 + a2
    if higher_goals != lower_goals:
        return higher if higher_goals > lower_goals else lower
    return higher


def _play_single(rng, pairings, higher: str, lower: str) -> str:
    """A single-leg play-in game, hosted by the higher seed. A draw goes to the
    higher seed -- see the docstring's note on this being the one branch that is
    a choice rather than a derived rule."""
    h, a = _play_once(rng, pairings, higher, lower)
    if h != a:
        return higher if h > a else lower
    return higher


def _run_liguilla(rng, pairings, seeds: list[str]) -> tuple[str, list[str]]:
    """`seeds` is the regular-season table, best first. Returns
    (champion, the 8 teams that reached the Liguilla).

    The field is returned rather than re-derived by the caller as "the top 8
    seeds", because that is NOT what it is: the play-in can promote a 9 or 10
    seed over a 7 or 8, so re-deriving it would silently report a field that
    never played.

    Play-in first (seeds 7-10 for the last two places), then an 8-team knockout
    of two-legged ties, RE-SEEDED each round so the highest surviving seed always
    meets the lowest."""
    rank = {t: i for i, t in enumerate(seeds)}

    # Play-in: 7v8 winner takes the 7 seed; the loser plays the 9v10 winner for
    # the 8 seed. Indices are 0-based, so seed 7 is seeds[6].
    seventh_up = _play_single(rng, pairings, seeds[6], seeds[7])
    seventh_down = seeds[7] if seventh_up == seeds[6] else seeds[6]
    ninth_up = _play_single(rng, pairings, seeds[8], seeds[9])
    hi, lo = ((seventh_down, ninth_up) if rank[seventh_down] < rank[ninth_up]
              else (ninth_up, seventh_down))
    eighth = _play_single(rng, pairings, hi, lo)

    field8 = seeds[:6] + [seventh_up, eighth]
    # Re-rank by regular-season position, not by how they arrived.
    field8.sort(key=lambda t: rank[t])
    qualified = list(field8)

    while len(field8) > 1:
        nxt = []
        for i in range(len(field8) // 2):
            higher, lower = field8[i], field8[len(field8) - 1 - i]
            nxt.append(_play_tie(rng, pairings, higher, lower))
        nxt.sort(key=lambda t: rank[t])
        field8 = nxt
    return field8[0], qualified


def simulate_ligamx_torneo(
    state: SoccerRatingState,
    table: list[LigaMxTeamState],
    remaining_fixtures: list[tuple[str, str]],
    n_simulations: int = 3000,
    seed: int | None = None,
) -> LigaMxPlayoffResult:
    """`table` is the torneo's CURRENT standings (all zeros for one that has not
    kicked off). `remaining_fixtures` is the unplayed regular-season games as
    (home, away) canonical keys.

    Every ordered pair's goal grid is computed ONCE and resampled, not recomputed
    per simulation -- the same optimisation season_sim_soccer uses. All ordered
    pairs are precomputed rather than only the scheduled ones, because the
    bracket can pair any two teams.
    """
    rng = random.Random(seed)
    teams = [t.team for t in table]
    unrated = [t for t in teams if state.get_count(t) == 0]
    if unrated:
        # Bail rather than invent a rating: an unrated team in the bracket
        # distorts EVERY other team's number, not only its own. Same posture as
        # playoff_sim_mls.
        return LigaMxPlayoffResult(teams=teams, unrated_teams=unrated, n_simulations=0)
    if len(teams) < LIGUILLA_TEAMS + 2:
        # Fewer teams than the play-in needs; refuse rather than index past the
        # end of the seed list.
        return LigaMxPlayoffResult(teams=teams, unrated_teams=[], n_simulations=0)

    pairings: dict[tuple[str, str], tuple[list[tuple[int, int]], list[float]]] = {}
    for home in teams:
        for away in teams:
            if home != away:
                pairings[(home, away)] = _cumulative_weights(predict_match(state, home, away).grid)

    champ = {t: 0 for t in teams}
    liguilla = {t: 0 for t in teams}
    table_top = {t: 0 for t in teams}

    for _ in range(n_simulations):
        sim = {t.team: _SimTeam(t.team, t.points, t.goal_diff, t.goals_for) for t in table}
        for home, away in remaining_fixtures:
            h, a = _play_once(rng, pairings, home, away)
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

        ranked = sorted((sim[t] for t in teams), key=_tiebreak)
        seeds = [r.team for r in ranked]
        table_top[seeds[0]] += 1

        winner, qualified = _run_liguilla(rng, pairings, seeds)
        champ[winner] += 1
        for t in qualified:
            liguilla[t] += 1

    n = float(n_simulations)
    return LigaMxPlayoffResult(
        teams=teams,
        champion_prob={t: champ[t] / n for t in teams},
        liguilla_prob={t: liguilla[t] / n for t in teams},
        table_top_prob={t: table_top[t] / n for t in teams},
        unrated_teams=[],
        n_simulations=n_simulations,
    )
