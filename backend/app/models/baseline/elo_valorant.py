"""Valorant team-level Elo rating engine -- same walk-forward architecture
as elo_mma.py (no home/away side, no discrete season structure to regress
between -- VCT runs across multiple concurrent regional leagues/circuits
with no single "offseason"), but market shape here is genuinely different:
Valorant's real, live inventory (confirmed 2026-07-19 -- see
market_catalog_valorant.py's own docstring for the live audit) is a Bo1/Bo3/
Bo5 SERIES, not a single game, much closer in structure to Tennis's
sets-within-a-match than to MMA's one-and-done fight. So this module has two
layers: a per-MAP win probability (plain team Elo, logistic, same formula as
elo.py/elo_mma.py) extended to a per-SERIES win probability via the standard
"race to k" best-of-N identity (same technique family the kickoff research
explicitly flagged: a binomial/negative-binomial combination, the same
category of technique Tennis uses to go from a per-set/per-game probability
to a full match-win probability).

K: grid-searched (scripts/derive_valorant_elo_constants.py) against this
app's own walk-forward Brier score on a real historical vlr.gg crawl.
Crawled in THREE passes, all real, all worth understanding: the first pass
covered only the 55 curated main-circuit VCT International/regional events
(1,651 usable matches, 88 teams) and found a real, smooth K=10 minimum
(Brier 0.23522). Extending the crawl to also cover Game Changers (the
women's division -- 204 MORE real events across its own parallel gc-2023..
gc-2026 season hubs, added after live Kalshi markets for GC matches, e.g.
"Gentle Mates GC vs G2 Gozen", were found staying stuck at BASE_RATING even
after the main-circuit-only training) grew the usable dataset to 10,273
matches across 2,454 teams, and the optimal K shifted to K=68 (Brier
0.21702 at the minimum, confirmed a genuine interior basin at the time by
extending the search well past the first grid's own K=64 boundary).

A third pass (2026-07-20) added VCT Challengers League (the regional 2nd
tier below VCT International -- vcl-2024/2025/2026 season hubs, added to
grow the real market-odds backtest sample, see below), nearly doubling the
usable dataset again to 19,644 matches. This shifted K back DOWN to **K=40**
(Brier 0.23876 at K=8 -> 0.23065 at K=40, the minimum -> 0.23157 at K=64, a
real smooth interior basin, not a grid-edge artifact). Also note the
post-warmup Brier itself is HIGHER at this K than the GC-only pass's own
0.21702 -- not a regression, but an honest reflection that Challengers-tier
adds a much larger, more heterogeneous pool of regional/qualifier teams
(more real parity and upsets among less-established rosters) that is
genuinely harder to predict than the GC-only dataset was, same
"the ceiling itself moved, not just the K" caveat this app applies whenever
a K search is re-run on a meaningfully different population. K=40 (per-series
update rule) was shipped at that point (trained on the full combined dataset
-- main circuit + GC + Challengers -- matching what actually serves live
predictions from one shared rating pool per team).

Real accuracy at K=40 (post-warmup, combined dataset, per-series rule):
61.99% -- beat the naive-0.5 baseline's own 0.25000 Brier (0.23065 at K=40),
a real signal, though modestly lower than the GC-only pass's own 65.18% for
the same reason the Brier moved (harder, broader population, not weaker
signal).

REAL IMPROVEMENT shipped 2026-07-20 (user-requested model-quality pass):
update_ratings now updates per real MAP played (using the real
maps_won_a/maps_won_b score split), not once per series -- see that
function's own docstring. Re-derived K under this new rule against the same
19,644-match combined dataset (scripts/derive_valorant_elo_constants.py):
**K=36** (Brier 0.22506 at the minimum, a real smooth basin -- 0.22513 at
K=32, 0.22516 at K=40, not a grid-edge artifact), a real, measured
IMPROVEMENT over the old per-series K=40's own 0.23065. Accuracy also
improved to **63.38%** (post-warmup) from 61.99%. **K=36, per-map, is what's
shipped now.** This is a real, title-specific result -- the identical change
was tried and REJECTED for CS2 (regressed Brier there, see elo_cs2.py's own
docstring), so it should not be assumed to transfer to any other title
without its own real measurement.

REAL MARKET-ODDS BACKTEST now exists (scripts/backtest_valorant_market_odds.py,
2026-07-20), same real Kalshi trade-history technique as elo_cs2.py's own
backtest -- see that module's docstring for the full methodology and the
real occurrence_datetime-leakage bug it originally caught and fixed.
Deliberately scoped to MAP 1 only (not the whole series): Kalshi's real
Valorant inventory (KXVALORANTMAP) is map-level, and this app's own
historical cache only has one start time per SERIES, which is only
genuinely lookahead-free as a "before this map" cutoff for Map 1 -- Map 2+
would need each map's own real start time, not scraped anywhere yet.

Original main+GC-only crawl matched only 9 of 439 real settled Map-1 events
to this app's own historical cache (most Kalshi-traded teams were
Challengers-tier, not in the crawl yet). After adding VCT Challengers
League (see K derivation above), the match rate jumped to **283/439** --
confirming the original gap was real coverage, not a naming/matching bug --
but pre-match liquidity is still thin for map-specific sub-markets
(confirmed live: only 19/283 matched events had any real trade before the
match's own start at all), giving a real, still-small **18-match** priced
sample (2026-05-14 through 2026-07-20). On this sample: **Model Brier
0.26119 (72.2% accuracy) vs. Market Brier 0.17688 (77.8% accuracy) -- the
MARKET BEATS THE MODEL**, same direction as CS2 and every other sport, on a
sample now 2x the original size, though still too small to be statistically
confident on its own -- treat as suggestive, not conclusive. model_validated
stays False -- see elo_service_valorant.py.

vlr.gg's own match-listing pages (both live and the historical crawl's
per-event pages) never state best_of directly -- confirmed live when the
historical crawl and this grid search both initially loaded ZERO usable
matches; see valorant_data.py::infer_best_of_from_score for the real,
deducible fix (a decided series' own winning map count IS its clinch
threshold), not a per-match extra page fetch. Also filters out match_date <
2020 -- one real, isolated vlr.gg data quirk (a forfeit whose timer decoded
to Unix epoch 0 / "1969-12-31") found live among the 13,036 raw crawled
rows, not a systemic issue.

MAP-LEVEL SIMPLIFICATION (flagged, not fixed): a single team Elo rating is
used for every map (no per-map-pick/agent-comp/attack-defense-side
adjustment) -- real esports team strength genuinely varies by map pool (a
team can be excellent on Ascent, weak on Lotus), which this rating cannot
capture yet. Same category of honest simplification as elo_soccer.py's
independent-Poisson grid deferring Dixon-Coles low-score correlation --
noted, not silently assumed away.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0
K = 36.0  # grid-searched against a real historical vlr.gg crawl UNDER THE PER-MAP UPDATE RULE (see update_ratings' own docstring and derive_valorant_elo_constants.py) -- shifted from the old per-series K=40 since per-map updates compound differently


@dataclass
class ValorantEloState:
    ratings: dict = field(default_factory=dict)
    # Real per-map observation count per team (2026-07-20 addition) -- lets
    # elo_service_valorant.py::get_series_distribution require a real
    # minimum sample size before trusting a rating. See update_ratings' own
    # docstring for why this counts real MAPS here (unlike CS2, which
    # counts real SERIES -- see elo_cs2.py's own Cs2EloState.games field).
    games: dict = field(default_factory=dict)
    # Real patch-recency tracking (2026-07-20 addition, see update_ratings'
    # own docstring for the full validated finding): last_patch_era records
    # which real Valorant patch a team's most recent map was played on;
    # maps_since_patch_change counts real map observations since that
    # team's own last detected patch change (reset to 0 the next time this
    # team plays after its own last_patch_era no longer matches the
    # match's real patch era).
    last_patch_era: dict = field(default_factory=dict)
    maps_since_patch_change: dict = field(default_factory=dict)

    # Real head-to-head SERIES record between a specific team pair (2026-07-20
    # addition, see elo_cs2.py's own Cs2EloState.h2h field for the identical
    # rationale -- Elo assumes transitivity, h2h captures a real non-transitive
    # matchup effect Elo can't). Updated once per real settled SERIES (in
    # update_ratings, same granularity as CS2), NOT once per map -- unlike
    # Elo's own per-map update rule here, the head-to-head signal was
    # validated at the series-outcome level (scripts/test_valorant_h2h_signal.py).
    h2h: dict = field(default_factory=dict)

    # Real PLAYER-level ratings (2026-07-21) -- see K_PLAYER's own module
    # comment. Same structural idea as elo_cs2.py's own player_ratings, but
    # fed by vlr.gg's real PER-MATCH scoreboards rather than CS2's per-event
    # roster approximation.
    player_ratings: dict = field(default_factory=dict)

    def player_strength(self, lineup: list[str]) -> float | None:
        """Mean rating of the real lineup that played -- mean (not sum) keeps
        K_PLAYER directly comparable to the team model's own K. None for an
        unknown lineup, never a guessed default."""
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
    """Full P(final series score = (maps_a, maps_b)) for a best-of-`best_of`
    series where each map is an iid Bernoulli(map_p) draw for team A, series
    stopping the instant either side clinches a majority. Standard "race to
    k" / negative-binomial (Banach matchbox family) result, not an invented
    formula: for a series requiring k = (best_of + 1) // 2 wins to clinch,
    P(A wins k-j, B wins j) = C(k-1+j, j) * map_p^k * (1-map_p)^j for
    j = 0..k-1 (A's clinching map must be the LAST one played), and
    symmetrically for B with (1-map_p). This exact identity is what lets
    every downstream market (series winner, map handicap, total maps) derive
    from one distribution, same "one grid, many markets" pattern as
    elo_soccer.py's MatchGoalDistribution.

    best_of=1 is the degenerate k=1 case (a single Bernoulli draw)."""
    k = (best_of + 1) // 2
    dist: dict[tuple[int, int], float] = {}
    for j in range(k):
        dist[(k, j)] = math.comb(k - 1 + j, j) * (map_p ** k) * ((1 - map_p) ** j)
        dist[(j, k)] = math.comb(k - 1 + j, j) * ((1 - map_p) ** k) * (map_p ** j)
    return dist


@dataclass
class SeriesDistribution:
    map_p: float  # team A's per-map win probability
    best_of: int
    dist: dict[tuple[int, int], float]  # (maps_a, maps_b) -> probability

    def prob_series_win_a(self) -> float:
        return sum(p for (a, b), p in self.dist.items() if a > b)

    def prob_series_win_b(self) -> float:
        return sum(p for (a, b), p in self.dist.items() if b > a)

    def prob_map_n_win_a(self, map_number: int) -> float | None:
        """P(team A wins map `map_number`), MAP-POOL-BLIND (see module
        docstring's map-level simplification) -- every map in the series
        uses the same map_p, so this is just map_p itself as long as the
        series can actually reach that map (P(series reaches map N) < 1 is
        irrelevant to a conditional "if this map is played" question, which
        is what the market asks -- a map_winner market only exists/settles
        for maps that actually get played). Returns None if map_number
        exceeds this best_of (shouldn't happen for a real market, but never
        guessed)."""
        if map_number < 1 or map_number > self.best_of:
            return None
        return self.map_p

    def prob_total_maps_over(self, line: float) -> float:
        """`line` is a half-integer (e.g. 2.5 in a Bo3) -- matches Kalshi/
        Polymarket's own O/U convention, no push case."""
        return sum(p for (a, b), p in self.dist.items() if (a + b) > line)

    def prob_total_maps_under(self, line: float) -> float:
        return 1.0 - self.prob_total_maps_over(line)

    def prob_handicap_cover_a(self, line: float) -> float:
        """Same spread convention as every other sport's game_lines module in
        this app: team A's map margin (maps_a - maps_b) must exceed -line to
        cover a line of `line` (e.g. line=-1.5 means A must win the series
        2-0 in a Bo3)."""
        return sum(p for (a, b), p in self.dist.items() if (a - b) > -line)

    def prob_handicap_cover_b(self, line: float) -> float:
        """Team B's own side of the same handicap market (each team gets its
        own Market row with its own line, e.g. "AG (-1.5) vs TEC (+1.5)" --
        see market_catalog_valorant.py) -- symmetric to prob_handicap_cover_a
        but from B's margin (maps_b - maps_a)."""
        return sum(p for (a, b), p in self.dist.items() if (b - a) > -line)


# HOW MUCH OF THE RATING GAP TO BELIEVE WHEN PRICING (#197, 2026-08-15).
#
# Same defect and same remedy as CS2 (#196), but a DIFFERENT constant, fitted on
# Valorant's own data. Measured walk-forward over 9,603 predictions where both
# sides clear MIN_GAMES, harness verified identical to production at lambda=1 on
# all 19,644 replayed matches:
#
#       gap        n   claimed   actual     miss   CI excludes claim?
#      0-49    3314    0.5504   0.5549  +0.0046   no
#     50-99    2406    0.6555   0.6272  -0.0283   YES
#   100-149    1625    0.7467   0.7194  -0.0273   YES
#   150-199     948    0.8218   0.7542  -0.0676   YES
#   200-299     834    0.8944   0.8369  -0.0575   YES
#      300+     329    0.9647   0.8784  -0.0862   YES
#
# WHY THE CONSTANT DIFFERS FROM CS2's 0.80. Valorant KEPT per-map Elo updates (a
# Bo3 won 2-1 is three observations, not one) where CS2 REJECTED them on its own
# data. More updates per match means a different rating spread, so the same
# underlying defect needs a different multiplier. Copying CS2's number across
# would have been the exact mistake this repo's esports findings keep warning
# about -- per-map Elo, patch adjustment and idle decay all failed to transfer
# between titles.
#
# LOL WAS TESTED AT THE SAME TIME AND SHIPS NOTHING: its own sweep chose
# lambda=1.00 on train Brier, so no shrink applies there. Three titles, three
# different answers, each from its own data.
#
# Fitted on TRAIN Brier alone (earlier 70% by date), then the held-out 30%:
#
#     brier    0.22537 -> 0.22340   BETTER
#     ece      0.05201 -> 0.03467   BETTER
#     logloss  0.64945 -> 0.64099   BETTER
#
# meeting calibration_temp.py's rule that a fit ships only if it improves BOTH
# ECE and Brier out of sample. Test-set miss by gap: 50-99 -0.053 -> -0.033,
# 100-149 -0.030 -> -0.000, 150-199 -0.131 -> -0.097, 200-299 -0.112 -> -0.079,
# 300+ -0.075 -> -0.056. The 0-49 bucket pays a small cost (+0.013 -> +0.020,
# slightly more UNDER-confident) which is the expected price of a proportional
# correction and stays well inside its interval.
#
# APPLIED AT PREDICTION TIME ONLY -- the update loop is untouched, so ratings
# keep their meaning and the fit cannot move under its own feet.
# See scripts/fit_esports_elo_gap_shrink.py.
GAP_SHRINK = 0.86


def predict_series(state: ValorantEloState, team_a: str, team_b: str, best_of: int) -> SeriesDistribution:
    map_p = map_win_prob((state.get(team_a) - state.get(team_b)) * GAP_SHRINK, 0.0)
    dist = series_score_distribution(map_p, best_of)
    return SeriesDistribution(map_p=map_p, best_of=best_of, dist=dist)


RATING_CLAMP = 800.0  # generous hard safety rail, same defensive role as elo_soccer.py's RATING_CLAMP -- should never bind for a real team-strength gap


# REAL IMPROVEMENT #2 shipped 2026-07-20 (user-requested model-quality
# pass, "flesh these out" follow-up to the per-map update change): a real
# per-title investigation into whether a Valorant patch change makes a
# team's PRE-patch rating partially stale, and whether the first few
# post-patch results should count for more. Real patch history from
# liquipedia.net/valorant/Patches (153 patches, 2020-2025, one cheap page
# fetch -- see elo_service_valorant.py's own docstring) matched to each
# historical match by real date. Grid-searched a K-multiplier x
# games-boosted grid (scripts/test_valorant_patch_signal.py) against the
# same real 19,644-match walk-forward: a SMALL multiplier for a SHORT
# window is a real, smooth basin (1.05x/2 games: -0.00008 vs baseline;
# 1.2-1.35x/3 games: -0.00029 to -0.00031, the real minimum; degrading
# smoothly on both sides) -- LARGER boosts genuinely hurt (2.0x/5 games:
# +0.00213, a real regression from overreacting to patch-change noise, not
# a data problem). **1.3x for a team's first 3 real map observations since
# its own last detected patch change is what's shipped** -- modest (~0.14%
# relative Brier improvement) but real, not noise.
PATCH_BOOST_MULTIPLIER = 1.3
PATCH_BOOST_GAMES = 3

# PLAYER-level rating (2026-07-21), the same structural change that worked
# well for CS2 (see elo_cs2.py::K_PLAYER) -- but it is MUCH WEAKER here, and
# that gap is the honest headline, not a footnote.
#
# Lineups are real vlr.gg PER-MATCH scoreboards (7,760 scraped across the
# 12 months from 2025-07-01, 6,188 joined to team_a/team_b by name) -- higher
# quality than CS2's per-event roster approximation. Despite that, on the
# lineup-covered subset the blend gains only -0.00322 vs CS2's -0.00819, and
# the PURE player model is WORSE than the team model here (it beat it for
# CS2).
#
# Diagnosed cause: player observation density. Valorant's crawl spans
# Challengers/Game-Changers tiers with thousands of teams, giving 7,367
# players over 6,188 matches (~8 observations each) vs CS2's 766 over 3,357
# (~44 each). Gating on >=5 observations per player recovers most of the
# effect (-0.00763, near CS2's), which confirms the METHOD is sound and the
# data is thin -- not that player-level rating fails for this title.
#
# Re-scoping to denser subsets was tried and REJECTED: excluding qualifiers,
# or requiring established teams, does raise the per-match gain (up to
# -0.00702) but loses coverage at almost exactly the same rate, so measured
# across ALL 19,144 post-warmup matches every scope lands within noise of
# each other (-0.00104 ungated vs -0.00113 best-scoped). Shipped UNGATED as
# the simplest option that keeps full Challengers/GC coverage.
#
# NOT market-validated, unlike CS2's: Valorant's real Kalshi closing-price
# sample is 19 events vs CS2's 78, which cannot resolve an effect of this
# size. Treat this as a small, honestly-measured walk-forward improvement
# only. model_validated stays False.
#
# SECOND, DEEPER LIMITATION found on a face-validity check (2026-07-21), and
# the reason to distrust these ratings as a "best players" list: the top-rated
# players are NOT the well-known VCT International stars, they are
# Challengers/Game-Changers-tier players -- and NOT because of small samples
# (the top 10 have 34-53 real games each). The cause is that Elo cannot
# calibrate across DISCONNECTED competitive pools: a team that dominates its
# own regional/GC tier accrues a very high rating beating weak opposition, but
# those tiers rarely play VCT International teams, so there is almost no
# cross-pool result to anchor the two scales against each other. CS2's crawl
# is S+A-Tier -- a far more interconnected pool -- which is part of why its
# ratings ranked the genuine elite correctly (NiKo/m0NESY/ZywOo/donk).
# Median observations per player here is just 4; only 1,000 of 7,367 players
# have >=20.
#
# The measured -0.00104 walk-forward gain is still real (it is scored against
# real outcomes, so it already prices in all of the above), which is why this
# ships -- but these player_ratings must NOT be surfaced or interpreted as an
# absolute skill ranking. They encode "dominance within that player's own
# competitive pool", which is a different quantity.
#
# IMPORTANT REFINEMENT (2026-07-21): -0.00104 UNDERSTATES this feature's real
# value, because it averages over 19,144 matches most of which this app would
# never bet. Restricted to matchups between teams Kalshi actually lists --
# the only matches that can become a real position -- the same shipped
# ungated config gains **-0.00380**, ~3.7x the headline number.
#
# k-CORE GATING TRIED AND REJECTED (2026-07-21): the tier-disconnection
# diagnosis above suggested restricting the blend to a densely-connected pool
# of teams (k-core of the team co-play graph). Within such a pool the gain
# looks excellent -- -0.01060 on the 8-core, LARGER than CS2's own -0.00819.
# But that pool is not the one this app bets: the densest core turns out to be
# Game Changers (long round-robin regional leagues where ~11 teams play each
# other repeatedly), not VCT International, and on Kalshi-listed matchups the
# gated variants are indistinguishable from ungated (-0.00373 / -0.00380 vs
# ungated's -0.00380). The impressive core number simply does not transfer to
# the matches that matter, so the extra machinery was not worth it. Kept
# ungated.
K_PLAYER = 40.0
PLAYER_BLEND_WEIGHT = 0.4


def _effective_k(state: ValorantEloState, team: str) -> float:
    if state.maps_since_patch_change.get(team, PATCH_BOOST_GAMES) < PATCH_BOOST_GAMES:
        return K * PATCH_BOOST_MULTIPLIER
    return K


def _apply_one_map_update(state: ValorantEloState, team_a: str, team_b: str, actual_a: float) -> None:
    a_r = state.get(team_a)
    b_r = state.get(team_b)
    p_a = map_win_prob(a_r, b_r)
    k_a, k_b = _effective_k(state, team_a), _effective_k(state, team_b)
    delta_a = k_a * (actual_a - p_a)
    delta_b = k_b * (actual_a - p_a)
    state.ratings[team_a] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, a_r + delta_a))
    state.ratings[team_b] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, b_r - delta_b))
    state.games[team_a] = state.games.get(team_a, 0) + 1
    state.games[team_b] = state.games.get(team_b, 0) + 1
    state.maps_since_patch_change[team_a] = state.maps_since_patch_change.get(team_a, 0) + 1
    state.maps_since_patch_change[team_b] = state.maps_since_patch_change.get(team_b, 0) + 1


def _apply_player_update(state: ValorantEloState, lineup_a, lineup_b, actual_a: float) -> None:
    """One shared-credit update per real settled SERIES (not per map -- the
    player signal was measured at series granularity). Skipped entirely when
    either real lineup is unknown; never invents membership."""
    a_str = state.player_strength(lineup_a)
    b_str = state.player_strength(lineup_b)
    if a_str is None or b_str is None:
        return
    delta = K_PLAYER * (actual_a - map_win_prob(a_str, b_str))
    for p in lineup_a:
        state.player_ratings[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, state.player_ratings.get(p, BASE_RATING) + delta))
    for p in lineup_b:
        state.player_ratings[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, state.player_ratings.get(p, BASE_RATING) - delta))


def update_ratings(state: ValorantEloState, team_a: str, team_b: str, winner: str | None,
                    maps_won_a: int | None = None, maps_won_b: int | None = None,
                    patch_era: str | None = None,
                    lineup_a: list[str] | None = None, lineup_b: list[str] | None = None) -> None:
    """REAL IMPROVEMENT this makes (2026-07-20, user-requested model-quality
    pass, validated against the real 19,644-match historical crawl): updates
    per REAL MAP played, not once per series -- a Bo3 won 2-1 is 3 real
    Bernoulli observations of relative map strength, not 1. The OLD
    series-level update (a single K-scaled nudge regardless of whether the
    series was a 2-0 sweep or a 2-1 nail-biter) discarded that extra signal;
    switching to per-map updates (re-deriving K under the new granularity,
    see derive_valorant_elo_constants.py) measurably IMPROVED walk-forward
    Brier (0.22506 vs the old per-series K=40's own 0.23065) and accuracy
    (63.38% vs 61.99%) on this exact dataset. NOT a universal verdict on the
    technique -- the identical change was tried and REJECTED for CS2 (real
    regression there, see elo_cs2.py's own docstring) -- this is a real,
    title-specific finding, not assumed from Valorant working.
    maps_won_a/maps_won_b (the real final score) give the real win/loss
    COUNT split; the actual per-map ORDER isn't scraped anywhere in this
    app, so A's real wins are applied first, then B's, a fixed deterministic
    tie-break rather than a guess at real sequencing. Falls back to a single
    series-level update only when maps_won_a/maps_won_b aren't known (a
    real, if rare, data gap, not a guessed score).

    `patch_era` (a real patch identifier, e.g. "Patch 11.11") tracks whether
    THIS match is the first time either team has appeared since a real
    patch change since their own last-seen era -- see PATCH_BOOST_MULTIPLIER's
    own module comment for the full validated finding. None (patch data not
    resolved for this match) simply skips the boost, same "don't guess"
    default as everywhere else."""
    if winner not in ("team_a", "team_b"):
        return
    key = tuple(sorted((team_a, team_b)))
    wins_first, total = state.h2h.get(key, (0, 0))
    first_won = (winner == "team_a") if team_a == key[0] else (winner == "team_b")
    state.h2h[key] = (wins_first + (1 if first_won else 0), total + 1)
    _apply_player_update(state, lineup_a or [], lineup_b or [], 1.0 if winner == "team_a" else 0.0)
    if patch_era is not None:
        for team in (team_a, team_b):
            if state.last_patch_era.get(team) is not None and state.last_patch_era[team] != patch_era:
                state.maps_since_patch_change[team] = 0
            state.last_patch_era[team] = patch_era
    if maps_won_a is not None and maps_won_b is not None and (maps_won_a + maps_won_b) > 0:
        for _ in range(maps_won_a):
            _apply_one_map_update(state, team_a, team_b, 1.0)
        for _ in range(maps_won_b):
            _apply_one_map_update(state, team_a, team_b, 0.0)
        return
    actual_a = 1.0 if winner == "team_a" else 0.0
    _apply_one_map_update(state, team_a, team_b, actual_a)


def predict_and_update(state: ValorantEloState, match: dict) -> SeriesDistribution | None:
    """Returns the PRE-match series distribution (walk-forward, no leakage),
    then updates ratings with the actual result if known. `match` is
    ValorantMatch-shaped (see valorant_data.py). Returns None if best_of
    isn't known yet (can't build a series distribution without it -- see
    ValorantMatch.best_of's own docstring on why this is sometimes
    unbackfilled)."""
    best_of = match.get("best_of")
    if not best_of:
        return None
    team_a, team_b = match["team_a"], match["team_b"]
    dist = predict_series(state, team_a, team_b, best_of)
    winner = match.get("winner")
    if winner is None:
        return dist
    update_ratings(
        state, team_a, team_b, winner,
        match.get("maps_won_a"), match.get("maps_won_b"), match.get("patch_era"),
        match.get("lineup_a"), match.get("lineup_b"),
    )
    return dist
