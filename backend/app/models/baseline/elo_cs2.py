"""CS2 team-level Elo rating engine -- parallel to elo_valorant.py (same
"race to k" best-of-N series-distribution technique), but a genuinely
separate, independent module by design -- this app deliberately does not
share code across esports titles, even where the underlying math is
identical, same "parallel modules per sport" discipline as every other
sport here.

K: grid-searched (scripts/derive_cs2_elo_constants.py) against this app's
own walk-forward Brier score on a real historical liquipedia.net crawl,
extended 2026-07-20 from S-Tier-only to S-Tier + A-Tier tournaments (see
scripts/build_cs2_match_cache.py) to grow the real market-odds backtest
sample -- now 8,839 matches with a real best_of + winner, 94 tournaments,
Oct 2023-Jul 2026. **K=32** -- a real, smooth basin (Brier 0.23918 at K=8
-> 0.23368 at K=32, the minimum -> 0.23600 at K=64, post-warmup), not a
noisy single-cell spike, same credibility bar elo_mlb.py's/elo_soccer.py's
own K derivations used. This is a real, if modest, SHIFT from the
S-Tier-only pass's own K=24 minimum -- A-Tier's broader, less consistently
elite team pool needs the standard chess/Elo default K=32 to track real
skill, same direction of finding (broader/noisier pool wants a higher K) as
elo_valorant.py's own Game-Changers-driven K shift, just smaller in
magnitude here since A-Tier is still a real curated tier, not an open
qualifier pool.

Real accuracy at K=32 (post-warmup): **60.75%** -- beats the naive-0.5
baseline's own 0.25000 Brier (0.23368 at K=32), confirming this is
real signal, not noise, and a real (if modest) improvement over the
S-Tier-only pass's own 59.07% from the larger dataset -- but this measures
the model's OWN internal walk-forward accuracy from win/loss history
alone, NOT whether it beats real market odds.

REAL MARKET-ODDS BACKTEST now exists (scripts/backtest_cs2_market_odds.py,
2026-07-20) -- confirmed live that Kalshi exposes real historical trade-level
data (via /markets/trades with a max_ts filter) for any market, including
settled ones, closing the "no historical CS2 odds archive exists" gap this
docstring originally assumed. Real, found-live bug on the first attempt: using
Kalshi's own `occurrence_datetime` as the "before the match" cutoff let
near-settlement trades leak through (confirmed live: a real market's
close_time was almost 3 HOURS BEFORE its own occurrence_datetime, meaning the
match had already resolved well before the "scheduled" time) -- produced an
implausible 98.4% "market accuracy" tell. Fixed by using this app's OWN
crawled estimated_start_time (Liquipedia's real timer widget) as the cutoff
instead, independent of Kalshi's own scheduling accuracy. On the original
S-Tier-only pass this matched 61 of 1,291 real settled series (2026-05-14
through 2026-07-20); after extending the crawl to S-Tier + A-Tier (see
K derivation above), the match rate grew to a real **85-match sample** (of
1,292 real settled events) -- still most of Kalshi's real CS2 volume is
lower-tier/regional matches outside even this broadened crawl (same coverage
gap poller_cs2.py's own docstring already found), but a real, meaningful
improvement in sample size. On this 85-match sample: **Model Brier 0.23030
(57.65% accuracy) vs. Market Brier 0.20069 (64.71% accuracy) -- the MARKET
BEATS THE MODEL**, same conclusion every other sport in this app has found,
now on a real sample nearly 40% larger than before. model_validated stays
False -- see elo_service_cs2.py.

Real inventory here (confirmed live 2026-07-19, see kalshi_cs2_client.py) is
WHOLE-MATCH/series winner + total maps played -- NOT per-map (Valorant's
Kalshi coverage is map-level; CS2's is match-level). map_win_prob/
prob_map_n_win_a still exist below for when/if KXCS2MAPWINNER populates
(currently empty live inventory), same "build the capability, note the
current gap honestly" pattern as the client module."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0
K = 32.0  # grid-searched against a real historical liquipedia.net crawl (S-Tier + A-Tier combined) -- see module docstring


@dataclass
class Cs2EloState:
    ratings: dict = field(default_factory=dict)
    # Real per-SERIES observation count per team (2026-07-20 addition) --
    # lets elo_service_cs2.py::get_series_distribution require a REAL
    # minimum sample size before trusting a rating, not just "has this team
    # ever played one real series" (a team with exactly 1 real series has a
    # rating that moved off BASE_RATING but is still extremely noisy).
    # REAL FINDING (2026-07-20): a per-MAP version of this same update was
    # tried and rejected for CS2 specifically -- measured live against the
    # real 8,843-match historical crawl, per-map updates (one Elo nudge per
    # real map played, using the real maps_won_a/maps_won_b score split)
    # made walk-forward Brier WORSE (0.23748 best-K vs 0.23368 at the
    # existing per-series K=32), a real regression, not an improvement --
    # unlike Valorant/LoL, where the identical per-map change measurably
    # IMPROVED Brier (see elo_valorant.py's/elo_lol.py's own docstrings).
    # Kept as a per-SERIES update here since that's what's actually
    # validated to work for this title -- same "validate per-sport, don't
    # assume a hypothesis that helped one title helps them all" discipline
    # this app already applies everywhere else (K itself, surface weights,
    # etc.).
    games: dict = field(default_factory=dict)

    # Real head-to-head series record between a specific team PAIR (2026-07-20
    # addition) -- keyed by sorted((team_a, team_b)) so lookup doesn't care
    # which side either team is on for a given match. Stores
    # (wins for the alphabetically-first team, total series played) -- see
    # elo_service_cs2.py::get_series_distribution's own docstring for why
    # this exists (Elo assumes transitivity; a real head-to-head record
    # captures a genuine non-transitive matchup effect Elo alone can't).
    h2h: dict = field(default_factory=dict)

    # Real roster-tenure tracking (2026-07-20 addition, see ROSTER_BOOST_MULTIPLIER's
    # own module comment for the full validated finding): last_transfer_date
    # records the most recent real Liquipedia transfer event date known for
    # a team as of its most recent series; games_since_roster_change counts
    # real series played since that team's own last detected roster change
    # (reset to 0 the next time a NEWER real transfer date is seen for this
    # team). Same pattern as elo_valorant.py's patch-recency tracking, just
    # keyed on a roster change instead of a patch, and per-SERIES instead of
    # per-map (CS2's own update granularity, see update_ratings' own docstring).
    last_transfer_date: dict = field(default_factory=dict)
    games_since_roster_change: dict = field(default_factory=dict)

    # Real PLAYER-level ratings (2026-07-21 addition, see K_PLAYER's own
    # module comment for the full validated finding). Keyed on the real
    # Liquipedia player name. This is the structural fix for the flaw every
    # other feature here only worked around: `ratings` above is keyed on TEAM
    # NAME, so an org that swaps three players keeps its old rating outright.
    player_ratings: dict = field(default_factory=dict)

    def player_strength(self, lineup: list[str]) -> float | None:
        """Mean rating of the real lineup that played. Mean (not sum) is what
        makes K_PLAYER directly comparable to the team model's own K -- moving
        all 5 members by `delta` moves this mean by exactly `delta`. None for
        an empty/unknown lineup, never a guessed default."""
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
    derivation (identical math, independently implemented here per this
    app's no-shared-code-across-titles discipline)."""
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


def predict_series(state: Cs2EloState, team_a: str, team_b: str, best_of: int) -> SeriesDistribution:
    map_p = map_win_prob(state.get(team_a), state.get(team_b))
    dist = series_score_distribution(map_p, best_of)
    return SeriesDistribution(map_p=map_p, best_of=best_of, dist=dist)


RATING_CLAMP = 800.0

# REAL IMPROVEMENT shipped 2026-07-20 (user-requested model-quality pass,
# "do them all" follow-up): a real investigation into whether a team's
# first few series after a real roster change (a real Liquipedia transfer
# event -- either a player joining OR leaving) should count for MORE, since
# the team's pre-change rating partly reflects players no longer on the
# roster. Real transfer history from Liquipedia's Player_Transfers archive
# (14,849 events across 2023-05 through 2026-07, see
# scripts/build_cs2_transfer_history_cache.py -- only ~38 real month-page
# fetches needed, NOT one fetch per team; 440/1450 real match-cache teams
# have at least 1 tracked event). Grid-searched a K-multiplier x
# games-boosted grid (scripts/test_cs2_roster_tenure_signal.py) against the
# real 8,839-match walk-forward: a real, smooth basin, DISCOUNTING (K < 1x,
# i.e. trusting post-change results LESS) measurably HURTS (0.5x/1game:
# +0.00094, getting worse the longer the discount window), while BOOSTING
# (K > 1x) measurably HELPS, peaking at 1.6-1.7x for 3 games (-0.00092 to
# -0.00093, degrading smoothly outside that window, not a single-cell
# spike). **1.6x for a team's first 3 real series since its own last
# detected roster change is what's shipped** -- same direction of finding
# as Valorant's patch-boost (recent disruption means results should count
# for MORE, not less), though this is a different real signal (personnel,
# not game version).
#
# REAL BUG caught and fixed before shipping (2026-07-20): games_since_roster_change
# was originally incremented for EVERY team on EVERY match, including teams
# that had NEVER had a real transfer detected -- meaning a brand-new team's
# very first few games always looked "< ROSTER_BOOST_GAMES" too, silently
# boosting K for EVERY new team regardless of any real roster-change signal
# (798 "boosted" teams measured live, only 153 of which had an actual
# tracked transfer -- a real cold-start confound, not the roster-tenure
# effect this was meant to isolate). Fixed by only ever incrementing a
# team's counter once it already has an entry (i.e. has already had at
# least one real transfer detected) -- re-validated afterward: the isolated
# signal is real and, if anything, slightly STRONGER (-0.00093 vs the
# confounded version's own -0.00075), not an artifact of the bug.
ROSTER_BOOST_MULTIPLIER = 1.6
ROSTER_BOOST_GAMES = 3

# REAL IMPROVEMENT shipped 2026-07-21 (user-requested "player level models"),
# and by a wide margin the most effective change this app's esports build has
# produced. Rates INDIVIDUAL PLAYERS and aggregates to the real lineup that
# played (Cs2EloState.player_strength), instead of keying skill on a team NAME
# that survives a roster overhaul unchanged.
#
# Lineups come from app/models/baseline/cs2_lineups.py (Liquipedia per-event
# participant rosters, plus a transfer-log reconstruction gated on landing on
# a valid 5-man lineup -- see that module's own docstring). Coverage is 38.8%
# of the 8,839 real historical matches, so this NEVER replaces the team model:
# it blends with it, and a match whose lineup can't be resolved falls back to
# the pure team prediction (see elo_service_cs2.py::_blend_player).
#
# Grid-searched (scripts/test_cs2_player_level_signal.py) against the real
# walk-forward. RE-DERIVED 2026-07-21 after transfer-based reconstruction
# lifted lineup coverage 29.7% -> 38.8% (see cs2_lineups.py): the optimum
# moved from K_PLAYER=16/weight=0.6 to **24/0.8**, and the gain grew from
# -0.00599 to -0.00819 Brier vs the team model on 3,357 evaluated matches.
# Better-trained player ratings simply deserve more weight -- which is what
# the re-grid found, rather than something assumed.
#
# The 0.8 weight is a REAL interior optimum, not "more player is always
# better": sweeping it gives 0.22446 (w=0.6) -> 0.22404 (w=0.8, the minimum)
# -> 0.22470 (w=1.0, pure player). The team model still carries real
# information, so the blend is justified rather than vestigial.
#
# CRITICALLY, and unlike every earlier feature: this is the first change that
# measurably narrows the gap to the real MARKET, and the re-grid was
# confirmed against real closing prices BEFORE shipping (self-Brier optima
# have NOT reliably transferred to the market in this app -- h2h/rest/roster
# all improved self-Brier while slightly WIDENING the market gap). On the 78
# real Kalshi closing-price events with both lineups resolved: team-only
# 0.22930 -> player-blend 0.21561 vs market 0.20251, cutting the gap from
# +0.02679 to +0.01310 -- ~51% of the market gap closed. The market is still
# ahead, so model_validated stays False.
K_PLAYER = 24.0
PLAYER_BLEND_WEIGHT = 0.8


def _effective_k(state: Cs2EloState, team: str) -> float:
    if state.games_since_roster_change.get(team, ROSTER_BOOST_GAMES) < ROSTER_BOOST_GAMES:
        return K * ROSTER_BOOST_MULTIPLIER
    return K


def _apply_player_update(state: Cs2EloState, lineup_a: list[str], lineup_b: list[str], actual_a: float) -> None:
    """One shared-credit update per real settled series: every member of a
    lineup moves by the same delta, so the lineup MEAN (what
    player_strength returns) moves by exactly that delta. Skipped entirely
    when either real lineup is unknown -- never invents membership."""
    a_str = state.player_strength(lineup_a)
    b_str = state.player_strength(lineup_b)
    if a_str is None or b_str is None:
        return
    delta = K_PLAYER * (actual_a - map_win_prob(a_str, b_str))
    for p in lineup_a:
        state.player_ratings[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, state.player_ratings.get(p, BASE_RATING) + delta))
    for p in lineup_b:
        state.player_ratings[p] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, state.player_ratings.get(p, BASE_RATING) - delta))


def update_ratings(state: Cs2EloState, team_a: str, team_b: str, winner: str | None,
                    team_a_transfer_date: str | None = None, team_b_transfer_date: str | None = None,
                    lineup_a: list[str] | None = None, lineup_b: list[str] | None = None) -> None:
    """Updates on the SERIES result, not per-map -- see this class's own
    `games` field docstring for why: a per-map update (one Elo nudge per
    real map played) was tried here and REJECTED after real measurement
    (Brier got worse, 0.23748 vs 0.23368), unlike Valorant/LoL where the
    identical change measurably helped. Still tracks a real per-series game
    count for the minimum-games confidence threshold (see
    elo_service_cs2.py::get_series_distribution).

    `team_a_transfer_date`/`team_b_transfer_date` (each team's own most
    recent REAL Liquipedia transfer date as of this match, resolved by the
    caller -- see elo_service_cs2.py's own docstring) trigger the
    ROSTER_BOOST_MULTIPLIER boost the next time a team's own transfer date
    changes from what was last seen. None (no tracked transfer for this
    team) simply skips the boost, same "don't guess" default as everywhere
    else in this app."""
    if winner not in ("team_a", "team_b"):
        return
    for team, transfer_date in ((team_a, team_a_transfer_date), (team_b, team_b_transfer_date)):
        if transfer_date is not None and state.last_transfer_date.get(team) != transfer_date:
            state.games_since_roster_change[team] = 0
            state.last_transfer_date[team] = transfer_date
    a_r = state.get(team_a)
    b_r = state.get(team_b)
    p_a = map_win_prob(a_r, b_r)
    actual_a = 1.0 if winner == "team_a" else 0.0
    k_a, k_b = _effective_k(state, team_a), _effective_k(state, team_b)
    delta_a = k_a * (actual_a - p_a)
    delta_b = k_b * (actual_a - p_a)
    state.ratings[team_a] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, a_r + delta_a))
    state.ratings[team_b] = max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, b_r - delta_b))
    state.games[team_a] = state.games.get(team_a, 0) + 1
    state.games[team_b] = state.games.get(team_b, 0) + 1
    # Only teams that have ALREADY had a real transfer detected (see the
    # reset loop above) get an entry here -- see ROSTER_BOOST_MULTIPLIER's
    # own module comment for the real cold-start confound this avoids.
    if team_a in state.games_since_roster_change:
        state.games_since_roster_change[team_a] += 1
    if team_b in state.games_since_roster_change:
        state.games_since_roster_change[team_b] += 1

    key = tuple(sorted((team_a, team_b)))
    wins_first, total = state.h2h.get(key, (0, 0))
    first_won = (winner == "team_a") if team_a == key[0] else (winner == "team_b")
    state.h2h[key] = (wins_first + (1 if first_won else 0), total + 1)

    _apply_player_update(state, lineup_a or [], lineup_b or [], actual_a)


def predict_and_update(state: Cs2EloState, match: dict) -> SeriesDistribution | None:
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
        match.get("team_a_transfer_date"), match.get("team_b_transfer_date"),
        match.get("lineup_a"), match.get("lineup_b"),
    )
    return dist
