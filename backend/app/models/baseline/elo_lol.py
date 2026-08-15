"""League of Legends team-level Elo rating engine -- parallel to
elo_valorant.py/elo_cs2.py (same "race to k" best-of-N series-distribution
technique), independently implemented per this app's no-shared-code-across-
esports-titles discipline.

K: grid-searched (scripts/derive_lol_elo_constants.py) against this app's
own walk-forward Brier score on a real historical Leaguepedia crawl (5,604
matches with a real best_of + winner, Leaguepedia's own "Primary"
tournament tier only -- LCK/LPL/LEC/LCS-LTA/Worlds/MSI and their real
regional-league equivalents, 2023-mid 2026 -- see
scripts/build_lol_match_cache.py). **K=36** -- a real, smooth basin (Brier
0.21961 at K=8 -> 0.21232 at K=36, the minimum -> 0.21450 at K=64), not a
noisy single-cell spike, same credibility bar CS2's/Valorant's own K
derivations used. Notably close to the borrowed K=32 starting point
(0.21235, barely worse) -- same "grid search still worth doing even when the
borrowed default was already close" finding as elo_soccer.py's own K
derivation.

Real accuracy at K=36 (post-warmup): **67.13%** -- the STRONGEST of all 3
esports titles in this app (CS2: 59.07%, Valorant: 65.18%), plausibly
because this crawl is exclusively Leaguepedia's "Primary" tier -- the most
competitively stable, well-scouted top leagues, with no equivalent to
Valorant's noisier combined main+Game-Changers pool or CS2's broader
S-Tier-but-still-varied circuit pulling the signal down. Beats the naive-0.5
baseline's own 0.25000 Brier, confirming real signal from win/loss history
alone.

REAL MARKET-ODDS BACKTEST now exists (scripts/backtest_lol_market_odds.py,
2026-07-20), same real Kalshi trade-history technique and same Map-1-only
scoping as backtest_valorant_market_odds.py (see that module's docstring for
the full methodology, including the real occurrence_datetime-leakage bug
elo_cs2.py's own backtest first caught and fixed). Sample is real but SMALL
(12 matches, of 403 real settled Map-1 events, 2026-05-14 through
2026-07-20) -- same thin-pre-match-liquidity reality as Valorant's own
Map-1 backtest, even though LoL's own estimated_start_time (Leaguepedia's
real Cargo DateTime_UTC field, not an estimate) is MORE trustworthy than
vlr.gg's -- confirming the low match rate here is about real market
liquidity, not timestamp quality. On this 12-match sample: **Model Brier
0.19876 (83.3% accuracy) vs. Market Brier 0.16862 (75.0% accuracy) -- the
MARKET BEATS THE MODEL on Brier despite the model calling MORE individual
matches correctly** (a real, genuine divergence between calibration and raw
accuracy on this small a sample -- Brier is this app's own standard
decision metric everywhere else, so that's what the go/no-go call follows).
Same direction as CS2/Valorant, but this sample is too small to be
statistically confident on its own -- treat as suggestive, not conclusive.
model_validated stays False -- see elo_service_lol.py.

This crawl needed real, unusually heavy rate-limit resilience to build (see
lol_data.py::cargoquery's own docstring) -- Leaguepedia's Cargo endpoint is
far stricter than Liquipedia's plain page views or vlr.gg.

Real inventory here (confirmed live 2026-07-19, see kalshi_lol_client.py) is
MAP-LEVEL (KXLOLMAP, 24 real open markets) + game-level total maps
(KXLOLTOTALMAPS, 12 open) -- same map-level shape as Valorant's Kalshi
coverage, unlike CS2's whole-series-level Kalshi coverage."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0
K = 24.0  # grid-searched against a real historical Leaguepedia crawl UNDER THE PER-MAP UPDATE RULE (see update_ratings' own docstring and derive_lol_elo_constants.py) -- shifted from the old per-series K=36 since per-map updates compound differently


@dataclass
class LolEloState:
    ratings: dict = field(default_factory=dict)
    # Real per-map observation count per team (2026-07-20 addition) -- lets
    # elo_service_lol.py::get_series_distribution require a real minimum
    # sample size before trusting a rating (see update_ratings' own
    # docstring for the full story).
    games: dict = field(default_factory=dict)

    # Real head-to-head SERIES record between a specific team pair (2026-07-20
    # addition, see elo_cs2.py's own Cs2EloState.h2h field for the identical
    # rationale). Updated once per real settled SERIES (in update_ratings,
    # same granularity as CS2/Valorant), NOT once per map -- the head-to-head
    # signal was validated at the series-outcome level
    # (scripts/test_lol_h2h_signal.py).
    h2h: dict = field(default_factory=dict)

    # Real PLAYER-level ratings (2026-07-21) -- see K_PLAYER's own module
    # comment. Fed by gol.gg per-game lineups (lol_lineups.py), the source
    # that finally bypassed Leaguepedia's rate limit.
    player_ratings: dict = field(default_factory=dict)

    def player_strength(self, lineup: list[str]) -> float | None:
        if not lineup:
            return None
        return sum(self.player_ratings.get(p, BASE_RATING) for p in lineup) / len(lineup)

    def get(self, team: str) -> float:
        return self.ratings.get(team, BASE_RATING)

    def games_played(self, team: str) -> int:
        return self.games.get(team, 0)

    def h2h_record(self, team_a: str, team_b: str) -> tuple[int, int]:
        """Returns (real prior series wins for team_a, total real prior
        series between this exact pair), reoriented from the stored
        alphabetical-order record onto whichever team is "team_a" for THIS
        query."""
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
    """Full P(final series score = (maps_a, maps_b)) -- standard "race to k"
    identity, see elo_valorant.py::series_score_distribution for the full
    derivation (identical math, independently implemented per this app's
    no-shared-code-across-titles discipline)."""
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


def predict_series(state: LolEloState, team_a: str, team_b: str, best_of: int) -> SeriesDistribution:
    # NO GAP_SHRINK HERE, AND THAT IS A MEASURED RESULT, NOT AN OVERSIGHT
    # (#197, 2026-08-15). CS2 ships GAP_SHRINK=0.80 and Valorant 0.86, both
    # because their Elo gaps are too steep. LoL was tested identically -- same
    # harness, same protocol, verified against production on all 5,604 replayed
    # matches -- and its own sweep chose lambda=1.00 on TRAIN Brier:
    #
    #     lam 1.00  train brier 0.20212  train ece 0.02313   <- best on both
    #     lam 0.80  train brier 0.20321  train ece 0.04241
    #     lam 0.60  train brier 0.20700  train ece 0.07270
    #
    # Only the 200+ buckets were significantly overconfident (-0.036, -0.053),
    # 16.9% of gated predictions; everything below sat inside its interval and
    # 0-49 was mildly UNDER-confident. Shrinking a title whose error is confined
    # to a tail drags a correctly calibrated majority toward 0.5 -- the tennis
    # temperature failure (#192).
    #
    # WORTH RE-TESTING LATER, for a specific reason: the HELD-OUT period shows
    # overconfidence at every gap (-0.024 to -0.079) while train does not
    # (train ece 0.02313 vs test 0.04078). That looks like recent drift a
    # train-fitted constant structurally cannot see. If it persists, re-run
    # scripts/fit_esports_elo_gap_shrink.py once more of that period is history.
    map_p = map_win_prob(state.get(team_a), state.get(team_b))
    dist = series_score_distribution(map_p, best_of)
    return SeriesDistribution(map_p=map_p, best_of=best_of, dist=dist)


RATING_CLAMP = 800.0

# PLAYER-level rating (2026-07-21), the same structural change shipped for
# CS2/Valorant. Lineups come from gol.gg per-game scoreboards (lol_lineups.py).
#
# Result is REAL but MODEST -- Valorant-tier, not CS2-tier. Grid-searched
# (scripts/test_lol_player_level_signal.py) on the 769 lineup-covered
# post-warmup matches: best blend K_PLAYER=32 / weight=0.4 gains -0.00331
# Brier vs the team model, a smooth interior optimum (k=40 already worse).
# The player pool is healthy here (425 rated, median 19 games each -- far
# better than Valorant's median 4), so this is NOT a data-thinness result:
# it's that LoL's own TEAM Elo is already the strongest of the 3 titles
# (0.20757 Brier), leaving little for player-level to add. The PURE player
# model is WORSE than the team model here (like Valorant, unlike CS2),
# confirming the team model already captures most of the signal.
#
# Coverage is 16.4% of the 5,604-match cache (0% pre-2025 -- the gol.gg crawl
# was scoped to game ids 70000-80000, ~2025-07 on; 68% in 2026), so like the
# other two titles this NEVER replaces the team model -- it blends, and any
# match without a resolvable lineup falls back to pure team Elo. NOT
# market-validated (LoL's real Kalshi closing-price sample is 12 events).
# model_validated stays False.
K_PLAYER = 32.0
PLAYER_BLEND_WEIGHT = 0.4


def _apply_player_update(state: LolEloState, lineup_a, lineup_b, actual_a: float) -> None:
    """One shared-credit update per real settled SERIES; skipped when either
    lineup is unknown, never invents membership."""
    a_str = state.player_strength(lineup_a)
    b_str = state.player_strength(lineup_b)
    if a_str is None or b_str is None:
        return
    delta = K_PLAYER * (actual_a - map_win_prob(a_str, b_str))
    for p in lineup_a:
        state.player_ratings[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, state.player_ratings.get(p, BASE_RATING) + delta))
    for p in lineup_b:
        state.player_ratings[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, state.player_ratings.get(p, BASE_RATING) - delta))


def _apply_one_map_update(state: LolEloState, team_a: str, team_b: str, actual_a: float) -> None:
    a_r = state.get(team_a)
    b_r = state.get(team_b)
    p_a = map_win_prob(a_r, b_r)
    delta = K * (actual_a - p_a)
    state.ratings[team_a] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, a_r + delta))
    state.ratings[team_b] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, b_r - delta))
    state.games[team_a] = state.games.get(team_a, 0) + 1
    state.games[team_b] = state.games.get(team_b, 0) + 1


def update_ratings(state: LolEloState, team_a: str, team_b: str, winner: str | None,
                    maps_won_a: int | None = None, maps_won_b: int | None = None,
                    lineup_a: list[str] | None = None, lineup_b: list[str] | None = None) -> None:
    """REAL IMPROVEMENT this makes (2026-07-20, user-requested model-quality
    pass, validated against the real 5,604-match historical crawl): updates
    per REAL MAP played, not once per series -- a Bo3 won 2-1 is 3 real
    Bernoulli observations of relative map strength, not 1. Re-deriving K
    under this new granularity (see derive_lol_elo_constants.py) measurably
    IMPROVED walk-forward Brier (0.20727 vs the old per-series K=36's own
    0.21232) and accuracy (67.86% vs 67.13%) on this exact dataset. Same
    real, title-specific result as Valorant (see elo_valorant.py's own
    docstring) -- NOT assumed to transfer, and in fact REJECTED for CS2 on
    its own data (see elo_cs2.py's own docstring). maps_won_a/maps_won_b
    (the real final score) give the real win/loss COUNT split; the actual
    per-map ORDER isn't scraped anywhere in this app, so A's real wins are
    applied first, then B's, a fixed deterministic tie-break rather than a
    guess at real sequencing. Falls back to a single series-level update
    only when maps_won_a/maps_won_b aren't known (a real, if rare, data
    gap, not a guessed score)."""
    if winner not in ("team_a", "team_b"):
        return
    key = tuple(sorted((team_a, team_b)))
    wins_first, total = state.h2h.get(key, (0, 0))
    first_won = (winner == "team_a") if team_a == key[0] else (winner == "team_b")
    state.h2h[key] = (wins_first + (1 if first_won else 0), total + 1)
    _apply_player_update(state, lineup_a or [], lineup_b or [], 1.0 if winner == "team_a" else 0.0)
    if maps_won_a is not None and maps_won_b is not None and (maps_won_a + maps_won_b) > 0:
        for _ in range(maps_won_a):
            _apply_one_map_update(state, team_a, team_b, 1.0)
        for _ in range(maps_won_b):
            _apply_one_map_update(state, team_a, team_b, 0.0)
        return
    actual_a = 1.0 if winner == "team_a" else 0.0
    _apply_one_map_update(state, team_a, team_b, actual_a)


def predict_and_update(state: LolEloState, match: dict) -> SeriesDistribution | None:
    best_of = match.get("best_of")
    if not best_of:
        return None
    team_a, team_b = match["team_a"], match["team_b"]
    dist = predict_series(state, team_a, team_b, best_of)
    winner = match.get("winner")
    if winner is None:
        return dist
    update_ratings(state, team_a, team_b, winner, match.get("maps_won_a"), match.get("maps_won_b"),
                   match.get("lineup_a"), match.get("lineup_b"))
    return dist
