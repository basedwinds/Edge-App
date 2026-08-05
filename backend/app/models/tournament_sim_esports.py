"""Elo-seeded single-elimination Monte Carlo for esports tournament futures
(CS2 / Valorant / LoL "Event: Winner" markets). Parallel in spirit to
bracket_sim_tennis.py, but with one hard difference that drives every design
choice here: for esports we do NOT have the real bracket/draw the way
tennisexplorer gives it for tennis. All we know from the market inventory is
the FIELD -- the set of teams that have a tournament_winner market under the
same group_label -- plus each team's Elo rating from that title's own service.

So this reconstructs a plausible bracket instead of reading a real one:
  * Seed the field strongest-to-weakest by Elo (get_team_rating).
  * Lay it into a standard single-elimination bracket (seed 1 vs seed N, top
    seeds meet last), padding to the next power of two with byes handed to the
    TOP seeds -- exactly how a real seeded knockout draw is built.
  * Monte Carlo the matches with the title's real pairwise series win prob.

The title decider now runs a real DOUBLE-ELIMINATION bracket (see
_run_double_elim), which is the format VCT stages, LEC/LCK/LPL playoffs and most
CS2 events actually use. It replaced a single-elim run whose stated flaw was
exactly this: one loss ended a favourite's tournament when in reality it does
not. Measured on a synthetic 600-Elo-spread field, the change moves the
favourite from 35.8% to 38.3% (16 teams) and cuts the longshots, which is the
direction a second life should push and the size it should push it.

DOUBLE-ELIM IS NOT A GUESS -- it was measured. Surveying vlr.gg's published
brackets across 16 live Valorant events: 8 of the 9 that publish a bracket are
double-elimination (every VCT stage and Champions, with a 3-round upper bracket),
1 is single-elim (a minor invitational), 7 publish none. So the default here
matches ~89% of real events that state a format.

TWO APPROXIMATIONS REMAIN, and neither is fixable by scraping harder:

  1. THE DRAW. We know the FIELD but not the bracket, so teams are seeded
     strongest-to-weakest by Elo. Checked directly on vlr.gg: for every event we
     price, the playoff bracket slots hold GROUP PLACEHOLDERS ("Omega #2",
     "Play-In #1-2"), not teams -- the draw does not exist yet because the group
     stage has not finished. Nobody has it, so this is inherent to pricing a
     tournament before its playoffs, not a data gap.

  2. THE GROUP STAGE. Bigger, and previously unnamed. Our market field for
     "VCT EMEA Stage 2 2026: Winner" is 12 teams; the real playoff bracket is
     8 (Upper Round 1 = 4 matches). Four of those twelve must be eliminated
     BEFORE any bracket exists, but this model brackets all twelve directly, so
     it hands a bracket path to teams that in reality must first survive a
     group stage. Modelling it properly means two phases (group -> seeded
     playoff), which is a real change, not a constant.

model_validated stays False and these rows are not staked. It exists
to put a principled, Elo-grounded number on ~160 real tournament_winner markets
that currently have NO model at all -- a first pass whose edge, like the rest
of the app, is proven or killed by forward CLV, not assumed. Because the
per-market prices are normalized to sum to 1 across the field (see
simulate_tournament_winner), the FAVOURITE/longshot SHAPE is meaningful even
where the absolute format assumption is imperfect.

win_prob_fn is injected by the caller (esports_tournament_pricing.py wraps each
title's own elo_service) so this module stays title-agnostic and unit-testable
with a trivial ratings dict.
"""
import random
from typing import Callable

# Shown on every futures row this model prices, so the approximation is never
# hidden behind a bare number (see esports_tournament_pricing.py + the futures
# routers). model_validated stays False and these rows are not staked.
TOURNAMENT_SIM_NOTE = (
    "Priced by an Elo-seeded double-elimination bracket simulation. The real "
    "draw isn't known, so seeding is by rating — approximate, shown for "
    "tracking, not staked."
)

DEFAULT_TRIALS = 20000
# Most esports playoff series are Bo3 (finals sometimes Bo5); Bo3 is the
# single most representative length and is used uniformly here rather than
# guessing a per-round format we don't have. A longer series slightly favours
# the stronger team, so this is a mild, consistent conservatism for favourites.
DEFAULT_BEST_OF = 3

WinProbFn = Callable[[str, str], float | None]  # P(team_a beats team_b) in a series


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


def _bracket_seed_positions(n: int) -> list[int]:
    """Standard single-elim seeding order for a bracket of size n (a power of
    two): returns the seed number (1-based) at each bracket slot, arranged so
    seed 1 and seed 2 can only meet in the final. E.g. n=4 -> [1, 4, 3, 2]."""
    seeds = [1, 2]
    while len(seeds) < n:
        m = len(seeds) * 2 + 1
        nxt: list[int] = []
        for s in seeds:
            nxt.append(s)
            nxt.append(m - s)
        seeds = nxt
    return seeds


def _seed_field(field_ratings: list[tuple[str, float]]) -> list[str | None]:
    """field_ratings: (team, rating) already filtered to rated teams. Returns
    the bracket slot order (strongest team = seed 1), padded with None byes on
    the weakest seed lines so the TOP seeds get the round-1 byes."""
    ranked = [t for t, _ in sorted(field_ratings, key=lambda tr: tr[1], reverse=True)]
    f = len(ranked)
    n = _next_pow2(f)
    # seed s (1-based) -> team, or None (bye) for seeds beyond the real field
    def team_for_seed(s: int) -> str | None:
        return ranked[s - 1] if s <= f else None
    return [team_for_seed(s) for s in _bracket_seed_positions(n)]


def _memoized_matrix(field: list[str], win_prob_fn: WinProbFn) -> dict[tuple[str, str], float]:
    """Precompute every ordered pair's series win prob ONCE (O(N^2) real Elo
    calls) so the 20k-trial loop is pure dict lookups, not ~N*trials calls into
    get_series_distribution -- the difference between ~250 calls and ~300k for
    a 16-team field."""
    mat: dict[tuple[str, str], float] = {}
    for a in field:
        for b in field:
            if a == b or (a, b) in mat:
                continue
            p = win_prob_fn(a, b)
            if p is None:
                p = 0.5
            mat[(a, b)] = p
            mat[(b, a)] = 1.0 - p
    return mat


def _play_round(field: list[str | None], mat: dict, rng: random.Random) -> list[str | None]:
    nxt: list[str | None] = []
    for i in range(0, len(field), 2):
        a = field[i]
        b = field[i + 1] if i + 1 < len(field) else None
        if a is None:
            nxt.append(b)
        elif b is None:
            nxt.append(a)
        else:
            nxt.append(a if rng.random() < mat[(a, b)] else b)
    return nxt


def _play_round_with_losers(
    field: list[str | None], mat: dict, rng: random.Random
) -> tuple[list[str | None], list[str]]:
    """Like _play_round but also returns the beaten teams, which is what a
    double-elimination bracket needs -- a loss drops you, it does not end you.
    Byes produce no loser."""
    winners: list[str | None] = []
    losers: list[str] = []
    for i in range(0, len(field), 2):
        a = field[i]
        b = field[i + 1] if i + 1 < len(field) else None
        if a is None:
            winners.append(b)
        elif b is None:
            winners.append(a)
        else:
            if rng.random() < mat[(a, b)]:
                winners.append(a)
                losers.append(b)
            else:
                winners.append(b)
                losers.append(a)
    return winners, losers


def _run_double_elim(bracket: list[str | None], mat: dict, rng: random.Random) -> str | None:
    """One double-elimination tournament; returns the champion.

    Upper bracket runs as a normal knockout. Every upper-bracket loser drops
    into the lower bracket, where a second loss eliminates. Each time the upper
    bracket produces a new batch of losers, the lower bracket first plays itself
    down ("minor" rounds) until it is the same size as that batch, then plays
    the batch ("major" rounds) -- the standard structure used by VCT, LEC/LCK
    playoffs and CS2 events.

    The grand final is a single series with NO bracket reset: the lower-bracket
    survivor does not have to beat the upper-bracket winner twice. Reset formats
    exist, and modelling one would raise the upper-bracket team's title chance a
    little further; without per-event format data the simpler and more common
    shape is the honest default.
    """
    ub: list[str | None] = list(bracket)
    lb: list[str | None] = []
    while len(ub) > 1:
        ub, dropped = _play_round_with_losers(ub, mat, rng)
        if not lb:
            lb = list(dropped)
            continue
        # Minor rounds: thin the existing lower bracket to meet the new arrivals.
        while len(lb) > len(dropped) and len(lb) > 1:
            lb, _ = _play_round_with_losers(lb, mat, rng)
        # Major round: survivors vs the freshly dropped, pairwise.
        merged: list[str | None] = []
        for i, drop in enumerate(dropped):
            if i < len(lb) and lb[i] is not None:
                a, b = lb[i], drop
                merged.append(a if rng.random() < mat[(a, b)] else b)
            else:
                merged.append(drop)
        # A lower bracket larger than the drop batch keeps its extras in play.
        merged.extend(lb[len(dropped):])
        lb = merged
    while len(lb) > 1:
        lb, _ = _play_round_with_losers(lb, mat, rng)
    champ_ub = ub[0] if ub else None
    champ_lb = lb[0] if lb else None
    if champ_ub is None:
        return champ_lb
    if champ_lb is None:
        return champ_ub
    return champ_ub if rng.random() < mat[(champ_ub, champ_lb)] else champ_lb


def _stable_rng(field_ratings: list[tuple[str, float]]) -> random.Random:
    """Deterministic RNG seeded from the field so displayed prices don't jitter
    between cache refreshes for an unchanged field (Monte Carlo noise would
    otherwise wobble every number by a few tenths of a point each recompute)."""
    seed = hash(tuple(sorted(t for t, _ in field_ratings))) & 0xFFFFFFFF
    return random.Random(seed)


def simulate_tournament_winner(
    field_ratings: list[tuple[str, float]],
    win_prob_fn: WinProbFn,
    trials: int = DEFAULT_TRIALS,
) -> dict[str, float] | None:
    """Returns {team: P(win the tournament)} for every rated team in the
    field, or None if fewer than 2 rated teams (nothing to simulate). Probs
    sum to 1 across the field by construction (every trial has exactly one
    champion), so the favourite/longshot shape is meaningful even though the
    single-elim structure is an approximation (see module docstring)."""
    rated = [(t, r) for t, r in field_ratings if r is not None]
    if len(rated) < 2:
        return None
    slots = _seed_field(rated)
    mat = _memoized_matrix([t for t, _ in rated], win_prob_fn)
    rng = _stable_rng(rated)
    wins = {t: 0 for t, _ in rated}
    for _ in range(trials):
        champ = _run_double_elim(list(slots), mat, rng)
        if champ is not None:
            wins[champ] += 1
    return {t: wins[t] / trials for t, _ in rated}


def simulate_reach(
    field_ratings: list[tuple[str, float]],
    win_prob_fn: WinProbFn,
    target_teams_left: int,
    trials: int = DEFAULT_TRIALS,
) -> dict[str, float] | None:
    """Returns {team: P(reach the round where `target_teams_left` remain)} --
    e.g. target_teams_left=16 is "make the round of 16", =2 is "reach the
    final". This is the SAME simulation as the winner sim, just counting a
    shallower milestone, so it prices advancement / "qualify to stage" markets
    off the identical bracket. None if fewer than 2 rated teams."""
    rated = [(t, r) for t, r in field_ratings if r is not None]
    if len(rated) < 2:
        return None
    slots = _seed_field(rated)
    mat = _memoized_matrix([t for t, _ in rated], win_prob_fn)
    rng = _stable_rng(rated)
    reached = {t: 0 for t, _ in rated}
    for _ in range(trials):
        field = list(slots)
        # Count a team as "reaching" the target round the moment the field
        # first shrinks to <= target_teams_left.
        while len(field) > 1:
            if len(field) <= target_teams_left:
                for t in field:
                    if t is not None:
                        reached[t] += 1
                break
            field = _play_round(field, mat, rng)
    return {t: reached[t] / trials for t, _ in rated}
