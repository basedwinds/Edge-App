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

This is deliberately an APPROXIMATION and is flagged as such wherever it's
surfaced (model_validated stays False, same as everything else in this app):
real esports events are often double-elimination or Swiss-into-playoff, which
this single-elim model can't capture and which would generally give strong
teams a somewhat HIGHER title chance (a second life after one loss). It exists
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
    "Priced by an Elo-seeded single-elimination bracket simulation. Real events "
    "are usually double-elim/Swiss and the true draw isn't known, so this is an "
    "approximation — shown for tracking, not staked."
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
        field = list(slots)
        while len(field) > 1:
            field = _play_round(field, mat, rng)
        champ = field[0] if field else None
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
