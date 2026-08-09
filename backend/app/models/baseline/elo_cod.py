"""Call of Duty Elo -- ratings and the series-score distribution.

Same shape as elo_cs2.py / elo_lol.py, independently implemented per this
app's no-shared-code-across-titles discipline, and DELIBERATELY thinner than
CS2's. CS2 carries a player-level blend, transfer-aware K and map-pool
ratings because per-match lineups and a transfer archive exist for it. The
Call of Duty source (breakingpoint.gg, see scripts/build_cod_match_cache_bp.py)
carries team, date, series score and best_of -- so this file models exactly
that and no more. Inventing the rest would be fitting structure the data does
not contain.

VALIDATED BEFORE BEING WRITTEN, which is the reason it exists at all:
walk-forward over 3,614 real matches (2020-2026) scored 0.6479 accuracy on
2,508 predictions, z = 14.8 -- between CS2 (0.6075) and LoL (0.6713). Accuracy
was flat across K = 16..40, so K here is not a tuned knob standing on one lucky
value. See scripts/check_cod_walkforward.py for the full table.

K = 24 rather than the best-accuracy K = 16, because accuracy was flat while
LOG-LOSS preferred the middle of the range (0.6334 at K=24 vs 0.6374 at K=16).
A market price is a probability, not a side, so the calibration measure breaks
the tie.

MAP-LEVEL vs SERIES-LEVEL. Ratings are trained on SERIES outcomes (who won the
match), and map_win_prob is then inverted to the per-map probability that
reproduces the observed series win rate. Same approach as the other titles.
Call of Duty series are Bo5 (CDL majors) and Bo7 (Esports World Cup, and every
currently-listed Kalshi market), so best_of is never assumed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0

# See module docstring: flat accuracy across 16..40, log-loss best mid-range.
K_TEAM = 24.0

# How far a single series may move a rating. Same guard the other titles use --
# one blowout against a badly-mismatched opponent should not relocate a team.
RATING_CLAMP = 800.0


@dataclass
class CodEloState:
    ratings: dict[str, float] = field(default_factory=dict)
    games: dict[str, int] = field(default_factory=dict)
    h2h: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)

    def get(self, team: str) -> float:
        return self.ratings.get(team, BASE_RATING)

    def games_played(self, team: str) -> int:
        return self.games.get(team, 0)

    def h2h_record(self, team_a: str, team_b: str) -> tuple[int, int]:
        """(prior series wins for team_a, total prior series between this exact
        pair), reoriented from the alphabetically-stored record onto whichever
        team is team_a for THIS query."""
        key = tuple(sorted((team_a, team_b)))
        wins_first, total = self.h2h.get(key, (0, 0))
        wins_a = wins_first if team_a == key[0] else (total - wins_first)
        return wins_a, total


def map_win_prob(team_a_rating: float, team_b_rating: float) -> float:
    diff = team_a_rating - team_b_rating
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def implied_elo_diff(prob: float) -> float:
    prob = min(max(prob, 1e-6), 1 - 1e-6)
    return 400.0 * math.log10(prob / (1.0 - prob))


def series_score_distribution(map_p: float, best_of: int) -> dict[tuple[int, int], float]:
    """Full P(final series score = (maps_a, maps_b)) -- the standard race-to-k
    identity: the last map is by definition won by the winner, so the other
    k-1+j maps are a free binomial arrangement."""
    k = (best_of + 1) // 2
    dist: dict[tuple[int, int], float] = {}
    for j in range(k):
        dist[(k, j)] = math.comb(k - 1 + j, j) * (map_p ** k) * ((1 - map_p) ** j)
        dist[(j, k)] = math.comb(k - 1 + j, j) * ((1 - map_p) ** k) * (map_p ** j)
    return dist


@dataclass
class SeriesDistribution:
    map_p: float
    best_of: int
    dist: dict[tuple[int, int], float]

    def prob_series_win_a(self) -> float:
        return sum(p for (a, b), p in self.dist.items() if a > b)

    def prob_series_win_b(self) -> float:
        return sum(p for (a, b), p in self.dist.items() if b > a)

    def prob_map_n_win_a(self, map_number: int) -> float | None:
        """DELIBERATELY the same number for every map, and that is why per-map
        markets must not be staked off it -- the identical flaw already found
        and gated in the other three titles, recorded here so nobody wires a
        CoD map market to it by assuming it varies."""
        if map_number < 1 or map_number > self.best_of:
            return None
        return self.map_p

    def prob_total_maps_over(self, line: float) -> float:
        return sum(p for (a, b), p in self.dist.items() if (a + b) > line)

    def prob_total_maps_under(self, line: float) -> float:
        return 1.0 - self.prob_total_maps_over(line)

    def prob_handicap_cover_a(self, line: float) -> float:
        return sum(p for (a, b), p in self.dist.items() if (a - b) > -line)

    def prob_handicap_cover_b(self, line: float) -> float:
        return sum(p for (a, b), p in self.dist.items() if (b - a) > -line)


def predict_series(state: CodEloState, team_a: str, team_b: str, best_of: int) -> SeriesDistribution:
    map_p = map_win_prob(state.get(team_a), state.get(team_b))
    return SeriesDistribution(map_p=map_p, best_of=best_of,
                              dist=series_score_distribution(map_p, best_of))


def series_p_from_map_p(map_p: float, best_of: int) -> float:
    d = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in d.items() if a > b)


def map_p_for_series_prob(target_prob: float, best_of: int, iterations: int = 60) -> float:
    """Invert series_p_from_map_p by bisection.

    Needed because ratings are trained on SERIES results while the score
    distribution is built from a per-MAP probability. Bisection rather than a
    closed form because the race-to-k series function has no clean inverse, and
    60 iterations puts the answer well inside float precision."""
    lo, hi = 0.0, 1.0
    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        if series_p_from_map_p(mid, best_of) < target_prob:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def predict_and_update(state: CodEloState, match: dict) -> float | None:
    """Score one match, then update. Returns the pre-match P(team_a wins the
    series), or None when the row cannot be scored.

    ORDER MATTERS AND IS THE WHOLE POINT: the probability is read BEFORE the
    ratings move, so replaying the archive in date order never lets a match
    inform its own prediction."""
    team_a, team_b = match.get("team_a"), match.get("team_b")
    if not team_a or not team_b or team_a == team_b:
        return None
    best_of = match.get("best_of") or 5
    ra, rb = state.get(team_a), state.get(team_b)
    series_p = series_p_from_map_p(map_win_prob(ra, rb), best_of)

    winner = match.get("winner")
    if winner is None:
        return series_p  # scheduled but unplayed: predictable, not scoreable

    # `winner` is the SIDE label ("team_a"/"team_b"), which is what CodMatch.winner
    # stores and what elo_service_cod's loader normalises the crawl's winner NAME
    # into. Accepting the raw name too, because a caller passing the name is the
    # easy mistake and silently scoring nothing is how it would present: this
    # function returning early leaves every rating at 1500 and every market
    # unpriced, with no error anywhere.
    if winner == "team_a" or winner == team_a:
        actual = 1.0
    elif winner == "team_b" or winner == team_b:
        actual = 0.0
    else:
        return series_p

    delta = max(-RATING_CLAMP, min(RATING_CLAMP, K_TEAM * (actual - series_p)))
    state.ratings[team_a] = ra + delta
    state.ratings[team_b] = rb - delta
    state.games[team_a] = state.games.get(team_a, 0) + 1
    state.games[team_b] = state.games.get(team_b, 0) + 1

    key = tuple(sorted((team_a, team_b)))
    wins_first, total = state.h2h.get(key, (0, 0))
    a_won = actual == 1.0
    first_won = a_won if team_a == key[0] else (not a_won)
    state.h2h[key] = (wins_first + (1 if first_won else 0), total + 1)
    return series_p
