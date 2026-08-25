"""League-table Monte Carlo season simulator for Soccer -- the season-long
equivalent of season_sim.py (NFL/NBA/MLB), but built for a genuinely
different real structure: a soccer league table has no bracket/seeding step,
just a full DOUBLE ROUND-ROBIN (every team plays every other team home AND
away) resolved purely by points -- so unlike NFL/NBA/MLB's own season
simulators, this one does NOT need a real game-by-game schedule/calendar at
all. The full fixture LIST for a round-robin is completely determined by
which teams are IN the league (confirmed live 2026-07-19 against Kalshi's
own real EPL/La Liga/Serie A/Bundesliga/Ligue 1 winner-futures markets: 20
teams for EPL/La Liga/Serie A, 18 for Bundesliga/Ligue 1, matching the
standard, well-known double round-robin format every one of these 5 leagues
actually uses) -- which real GAMEWEEK each pairing happens in doesn't affect
final standings, only WHO plays WHOM, so this sidesteps the real blocker
this app's own build plan flagged (football-data.co.uk's fixtures.csv being
too thin to serve as a real calendar) rather than needing to solve it.

Scope: League Winner, Top-4 (Champions League qualification), and
Relegation. Confirmed live 2026-07-19: Kalshi's own relegation markets
(KXEPLRELEGATION-27 etc) resolve on a plain "is this team relegated Y/N"
per-team basis, no sub-market for the playoff mechanics themselves -- so
this module computes P(finish in the AUTOMATIC drop zone: bottom 3 for
EPL/La Liga/Serie A, bottom 2 for Bundesliga/Ligue 1) via
RELEGATION_ZONE_SIZE below. For EPL/La Liga/Serie A this is the real,
complete answer (no playoff exists there). For Bundesliga/Ligue 1, this is
an explicitly-flagged LOWER BOUND -- both use a real relegation PLAYOFF for
the team that finishes just above the automatic zone (16th plays a 2-legged
tie against a lower-division team), which this module does NOT simulate, so
the true relegation probability for teams near that boundary is somewhat
HIGHER than this number for 16th place and somewhat LOWER for 15th (some of
16th's real relegation risk isn't captured; the boundary itself is fuzzier
than a clean cutoff). MLS is out of scope for all three market types -- it's
conference-based with an unbalanced schedule (each team does NOT play every
other team the same number of times), so the round-robin assumption this
whole module depends on is genuinely wrong there, not just unverified.

Newly-PROMOTED teams with genuinely ZERO top-flight history (confirmed live
2026-07-19 via a systematic scan across all 5 leagues' real Kalshi winner
markets against football-data.co.uk's own team list: only Bundesliga's
Elversberg has no historical top-flight rating at all -- every OTHER
promoted/rejoining team this scan initially flagged, e.g. EPL's Hull City/
Coventry City/Leeds United/Ipswich Town, turned out to be a real NAMING
mismatch against football-data.co.uk's own shorthand, not a genuine data
gap, and is now fixed via market_matcher_soccer.py's TEAM_ALIASES -- see
that file's own comment on how this was actually caught: the real EPL/
Ligue 1 market favorite landing at an impossible exact 0.0 simulated-
champion probability) now get a REAL rating derived from their own
second-tier form: their final second-tier attack/concede rating (see
scripts/build_soccer_match_cache.py's second-division fetch, added
2026-07-19) shifted by PROMOTED_TEAM_ATTACK_LOG_DISCOUNT/
PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT below -- a real, data-grounded
conversion factor derived from 476 genuine historical promotion events
across all 5 leagues (scripts/derive_soccer_promotion_discount.py), not a
guess. Re-verified live 2026-07-19 after the second-tier cache extension:
Elversberg -- the one team this app's own earlier live scan found with
ZERO top-flight rating -- DOES have real second-tier (Bundesliga 2)
history once that data was actually fetched, so it now gets this real
rating too, not the placeholder below. That placeholder (average of the
CURRENT bottom-quartile of rated top-flight teams -- a defensible "a
promoted team is more likely to struggle than thrive" prior, not a real
estimate) is kept as a fallback for the theoretical case of a team with
NEITHER a top-flight NOR a second-tier rating, but no such real team has
been found in this app's own data."""
from __future__ import annotations

import bisect
import datetime
import math
import random
from dataclasses import dataclass, field

from app.ingestion.market_matcher_soccer import canonical_team_key
from app.models.baseline.elo_soccer import MAX_GOALS, SoccerRatingState, predict_match

# Standard FIFA/UEFA-style tiebreak used (in some exact form) by every one
# of these 5 leagues: points, then goal difference, then goals scored. Real
# leagues have additional tiebreaks (head-to-head record, etc.) this doesn't
# model -- a genuine simplification, only matters for the rare exact tie
# across all three of these, negligible effect on P(champion)/P(top-4).
_TIEBREAK_ORDER = ("points", "goal_diff", "goals_for")

# Real, well-known competition rule (not scraped -- these are stable format
# facts, not time-sensitive live data): how many teams drop via the
# AUTOMATIC relegation zone (see module docstring on why Bundesliga/Ligue 1
# are a lower bound, not the complete real number, for the team right at
# this boundary).
RELEGATION_ZONE_SIZE = {
    "E0": 3,
    "SP1": 3,
    "I1": 3,
    "D1": 2,
    "F1": 2,
}

# NO TEAM_STRENGTH_SIGMA HERE, AND THAT IS DELIBERATE -- BUILT, MEASURED, AND
# REJECTED (2026-08-05). Do not add it back without new evidence.
#
# CFB (225), NFL (100), NBA (75) and WNBA (100) all carry a per-season, per-team
# strength offset, because holding each Elo rating as exactly known and drawing
# every game as an independent coin made their seasons far too narrow. Soccer
# looks like the same shape and is NOT.
#
# Implemented properly (per-scenario strength draws with the pairing grids
# rebuilt per scenario, since they depend on the ratings) and swept against the
# same backtest used for the others -- 5 leagues x 11 seasons, prior-seasons-only
# ratings, zero games played, n=1,072 team-seasons scored on relegation:
#
#   sigma   0.00   gap 1.70pp   Brier 0.0988      <- shipped (no offset)
#   sigma   0.05   gap 1.70pp   Brier 0.0979
#   sigma   0.10   gap 2.08pp   Brier 0.0978
#   sigma   0.15   gap 3.43pp   Brier 0.0983
#   sigma   0.25   gap 5.10pp   Brier 0.1008
#
# Calibration gets monotonically WORSE, and leave-one-season-out made 9 of 11
# held-out seasons worse while fitting 0.00-0.05 in every fold. It also cost 4x
# (4.6s -> 19.0s across the 5 leagues).
#
# WHY soccer differs, which is the part worth remembering: the other four sports
# reduce a game to one Bernoulli draw, so a season is near-binomial and genuinely
# too tight. This model draws a Poisson SCORELINE and awards 3/1/0, which already
# carries far more per-match variance -- the season distribution is wide enough
# without help, so an extra offset only over-widens it. The overall gap was
# already 1.70pp before any change, versus NBA's 8.70pp; there was no narrowness
# to fix.
#
# STILL OPEN, and NOT solved by this: the model overstates its most confident
# relegation calls -- predicted 40-60% happened 35.8% (n=53), predicted 60-80%
# happened 25.0% (n=4). That is a thin tail, not a width problem. Separately, in
# all 1,072 team-seasons this model has never put a historically top-half club
# above 30% relegation, so a live number like that is extrapolation with no
# validation behind it in either direction.
#
# SHRINKING TOWARD THE PRIOR WAS TESTED AND REJECTED TOO (2026-08-05).
# p' = base + lam*(p - base), base = zone_size/n_teams, swept over the same
# 1,072 predictions:
#
#     lam   1.00 gap 1.70pp | 0.95 gap 1.22pp | 0.90 1.63pp | 0.80 1.92pp | 0.75 2.18pp
#
# All 11 folds fitted lam=0.95, but held-out it is a COIN FLIP -- 6 seasons
# better, 5 worse -- and 0.95 barely moves the band it was meant to fix (the
# >=40% gap only closes -10.8pp -> -9.6pp). The lam that DOES fix the tail
# (0.75 takes that gap to -2.9pp) makes pooled calibration clearly WORSE,
# because it drags the large, already-correct low-probability mass off target
# to rescue 57 predictions.
#
# So the tail bias is real but a global shrink is the wrong instrument, and a
# tail-only correction would be fitted on 57 points spread over 11 seasons --
# roughly 5 per fold, which cannot be validated. Left documented rather than
# "fixed" with something unvalidatable. ONLY the four soccer market types
# ABOVE relegation were checked clean (league_winner 1.19pp, top2 1.13pp,
# top4 1.70pp, top_half 2.26pp), so this is specific to relegation, not the sim.

# Real, data-grounded promotion discount -- derived 2026-07-19 from 476
# genuine historical promotion events across all 5 leagues (see
# scripts/derive_soccer_promotion_discount.py's own printed output): a
# promoted team's real top-flight attack rating averages
# exp(-0.2558) = 0.77x their final second-tier attack rating, and their
# concede ("leakiness", higher = weaker defense) averages
# exp(+0.2444) = 1.28x worse -- both directionally exactly what you'd
# expect (a team that dominated a weaker division regresses hard against
# tougher competition), stdev ~0.20 on both across the 476 events (a real
# spread, not a razor-thin/noisy single-digit-sample result).
PROMOTED_TEAM_ATTACK_LOG_DISCOUNT = -0.2558
PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT = 0.2444


@dataclass
class TeamSeasonResult:
    team: str
    points: int = 0
    goals_for: int = 0
    goals_against: int = 0
    # Matches this team finished without conceding. Counted per simulated match
    # rather than derived from goals_against, because a season total of 0 against
    # says nothing about how many individual matches were shut out.
    clean_sheets: int = 0

    @property
    def goal_diff(self) -> int:
        return self.goals_for - self.goals_against


def _promoted_team_placeholder_rating(state: SoccerRatingState, known_teams: list[str]) -> tuple[float, float]:
    """Bottom-quartile-of-the-top-flight fallback -- see module docstring on
    why this only fires for a team with NEITHER a top-flight NOR a
    second-tier rating (in practice just Elversberg). Falls back further to
    league-average (0.0, 0.0) if fewer than 4 known teams are rated (should
    not happen in practice for any of the 5 real leagues this is built for)."""
    rated = [t for t in known_teams if state.get_count(t) > 0]
    if len(rated) < 4:
        return 0.0, 0.0
    ranked = sorted(rated, key=lambda t: state.get_attack(t) - state.get_concede(t))
    bottom_n = max(1, len(ranked) // 4)
    bottom = ranked[:bottom_n]
    avg_attack = sum(state.get_attack(t) for t in bottom) / len(bottom)
    avg_concede = sum(state.get_concede(t) for t in bottom) / len(bottom)
    return avg_attack, avg_concede


def _promoted_team_rating(
    team: str, state: SoccerRatingState, known_teams: list[str], second_tier_state: SoccerRatingState | None,
) -> tuple[float, float]:
    """Real second-tier rating + PROMOTED_TEAM_*_DISCOUNT if this team has
    any second-tier match history (the normal case -- see module docstring),
    else the old rough bottom-quartile placeholder."""
    if second_tier_state is not None and second_tier_state.get_count(team) > 0:
        return (
            second_tier_state.get_attack(team) + PROMOTED_TEAM_ATTACK_LOG_DISCOUNT,
            second_tier_state.get_concede(team) + PROMOTED_TEAM_CONCEDE_LOG_DISCOUNT,
        )
    return _promoted_team_placeholder_rating(state, known_teams)


def _cumulative_weights(grid: list[list[float]]) -> tuple[list[tuple[int, int]], list[float]]:
    outcomes = []
    weights = []
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            outcomes.append((h, a))
            weights.append(grid[h][a])
    cum = []
    total = 0.0
    for w in weights:
        total += w
        cum.append(total)
    return outcomes, cum


@dataclass
class SeasonSimResult:
    teams: list[str]
    champion_prob: dict = field(default_factory=dict)
    top4_prob: dict = field(default_factory=dict)
    # top2_prob/top_half_prob added 2026-07-19 -- real, live Kalshi
    # inventory found for EPL specifically (KXEPLTOP, three real events
    # under ONE series: "-27TOPHALF"/"-27TOP4"/"-27TOP2", confirmed live --
    # a genuinely different real discovery than the empty KXEPLTOP4 series
    # this app's own earlier live audit checked, see kalshi_soccer_client.py
    # docstring). Computed the exact same way top4_prob already is, just at
    # two more real rank cutoffs.
    top2_prob: dict = field(default_factory=dict)
    top_half_prob: dict = field(default_factory=dict)
    relegation_prob: dict = field(default_factory=dict)
    # team -> {final points: how many simulated seasons ended there}. Kept as a
    # histogram rather than a raw sample list so it stays small (a season spans
    # ~40 distinct point totals) and can be cached alongside the other outputs.
    points_dist: dict = field(default_factory=dict)
    # team -> {final position (1-based): share of simulated seasons}. Read off
    # the SAME `ranked` list champion/top4/relegation already come from, so it is
    # exactly consistent with them by construction -- P(position 1) equals
    # champion_prob, and the bottom `zone_size` positions sum to relegation_prob.
    # That identity is the cheapest available check that this is right, and
    # test_position_identities() in the probe asserts it.
    #
    # Prices Kalshi/Polymarket's "Nth Place Finish" and "Nth Place (Relegation
    # Survivor)" and "Last Place Finisher" markets -- 51 live entries across 30+
    # leagues that were previously, and wrongly, labelled unmodellable.
    position_dist: dict = field(default_factory=dict)
    # team -> P(finishes the season with the most clean sheets). Ties split
    # evenly among the co-leaders, matching how these books settle a tie (see
    # Market.rules_secondary, e.g. h2h_wins "all markets resolve to 50/50").
    #
    # EMPTY when the season is already part-played and the caller did not supply
    # `starting_clean_sheets`. See the docstring of simulate_season: a partial
    # season's clean sheets cannot be recovered from `starting_table`, which
    # carries only points/goals, so the honest output is nothing rather than a
    # count that silently ignores every match already played.
    most_clean_sheets_prob: dict = field(default_factory=dict)
    unrated_teams: list[str] = field(default_factory=list)
    n_simulations: int = 0


# Leagues whose season runs inside ONE calendar year. Everything else is the
# European August-May shape. This matters because the cached `season` field is
# derived with the European rule and therefore labels a Brazilian match played
# in August 2026 as season "2026-2027", which would split one real season in
# half. The date window below is derived per league instead of trusting it.
CALENDAR_YEAR_LEAGUES = frozenset({"BRA1", "CHN1", "JPN1", "NOR1", "SWE1", "MLS", "MEX1"})


# ---------------------------------------------------------------------------
# SEASON-PROGRESS GATE
#
# WHY. The position/clean-sheet outputs below were scored against finished
# seasons for the first time on 2026-08-25
# (scripts/check_soccer_season_sim_calibration.py, 17 leagues, n=6,186
# team-seasons per cutoff). They are OVERCONFIDENT EARLY and fine late -- the
# same failure racing top_n had. Worst case, preseason relegation:
#
#     claimed 0.94  ->  actually went down 45% of the time   (+49.3pp)
#     claimed 0.78  ->  actually 48%                         (+29.7pp)
#
# The error is the model claiming HIGH probabilities that do not land, which
# manufactures false edge on the YES side of favourites -- the worst possible
# direction for staking. It decays as the season is played, and by 75% every
# question is inside a few points.
#
# THE AGGREGATE DOES NOT SHOW THIS. Every question reads gap +0.00pp overall at
# every cutoff, because the simulation normalises positions to sum to 1. Only
# the per-bucket table reveals it. Do not "re-check whether this gate is needed"
# with an aggregate calibration number -- it will always say the model is
# perfect.
#
# MEASURED IN FIXTURES, NOT WEEKS, because that is what was measured and because
# international breaks, midweek rounds and differing season lengths make a
# calendar threshold mean different amounts of football in different leagues.
#
# The gate lifts itself: progress is recomputed per request from the live table,
# so a league starts pricing again the moment it crosses its threshold.
MIN_SEASON_PROGRESS = {
    # ---- DIRECTLY MEASURED (17 leagues, n=6,186 team-seasons per cutoff) ----
    #
    # league_winner (the `champion` question). Overconfidence by cutoff, pooled,
    # on the buckets claiming >= 0.50:
    #     0%   +16.3 / +10.6 / +21.0 pp
    #     25%  +10.0 /  +7.4 /  +8.0 pp
    #     50%   +1.2 / +11.7 /  +0.4 pp   <- still one bad band (n=96)
    #     75%   +4.5 /  -3.1 /  +1.6 pp
    # 50% looks tempting -- two of its three bands are near-perfect. It is set at
    # 0.75 anyway because a SECOND, independent view agrees that 50% is not
    # clean: per league at 50%, D1 reads +11.9pp and T1 +28.6pp. One noisy band
    # would be noise; a pooled band and two leagues is a pattern. This is also
    # the biggest live surface (579 of 735 markets), so it is the wrong place to
    # take the optimistic reading.
    "league_winner": 0.75,
    # relegation, per league at 50%: E0 +9.3, SP1 +13.3, D1 +6.2, F1 +8.7.
    # At 75%: +1.8 / +5.0 / +1.6 / +5.3 / -2.1. Clean only at 75%.
    "relegation": 0.75,
    # top4 is the best-behaved question. Pooled at 25%: +0.8 / +3.8 / +4.1 pp --
    # already inside the noise. NOR1 (+16.6) and SWE1 (+11.0) are still off at
    # 25%, but those cells hold ~10-40 rows each and both are inside +-5pp by
    # 50%, so the pooled reading carries it.
    "top4": 0.25,

    # ---- NOT DIRECTLY MEASURED ----
    # Each takes its stricter measured neighbour, because the error grows as the
    # question gets tighter. Change these by ADDING THEM TO THE BACKTEST, not by
    # argument.
    "top_half": 0.25,   # looser than top4 -> inherits top4's bar
    "top2": 0.50,       # tighter than top4, looser than champion -> between them

    # ---- NOT YET LIVE ----
    # The 23 "Team with Most Clean Sheets" markets are still in the catalog
    # backlog. Per league at 50% every one is inside 10pp; by 75% several flip
    # to -10..-15pp (UNDER-confident, the safe direction but still wrong). Wired
    # ahead of the markets so they cannot land ungated -- update the key if
    # ingestion names the type differently.
    "most_clean_sheets": 0.50,
}

# Not gated: team_points (a scalar total, not a finishing position) and the
# MLS/Liga MX bracket markets, which are not priced from this simulation at all.


def season_progress(n_teams: int, played_pairs) -> float:
    """Fraction of the season's double round-robin already played, 0.0-1.0.

    Returns 0.0 for an unknown or degenerate field rather than raising, so a
    league with no current table reads as preseason -- which is the safe answer,
    because every threshold above then blocks.
    """
    if not n_teams or n_teams < 2:
        return 0.0
    total = n_teams * (n_teams - 1)
    return min(1.0, len(played_pairs or ()) / total)


def season_progress_ok(market_type: str, progress: float) -> bool:
    """True when this market type may be STAKED at this point in the season.

    An ungated market type returns True -- the gate is an allowlist of known
    problems, not a default-deny, so adding a new futures type does not silently
    switch it off.
    """
    threshold = MIN_SEASON_PROGRESS.get(market_type)
    if threshold is None:
        return True
    return (progress or 0.0) >= threshold


def season_progress_note(market_type: str, progress: float) -> str:
    """User-facing reason a row is priced but not stakeable."""
    threshold = MIN_SEASON_PROGRESS.get(market_type, 0.0)
    return (
        f"Not staked yet: this league is {progress*100:.0f}% through its season and "
        f"this market needs {threshold*100:.0f}%. Measured across 17 leagues and "
        f"6,186 team-seasons, the season model is overconfident early -- preseason it "
        f"has claimed 94% for teams that were relegated 45% of the time -- which "
        f"invents edge on favourites. It self-corrects as fixtures are played, and "
        f"this market becomes stakeable automatically."
    )



def current_season_table(league: str, matches, today=None):
    """(starting_table, played_pairs) for the season IN PROGRESS.

    starting_table maps team -> (points, goals_for, goals_against); played_pairs
    holds the (home, away) orderings already contested, so simulate_season knows
    not to resample them. Returns empty structures when the league is between
    seasons, which makes the caller behave exactly as it did before.

    Teams are canonicalized here so the keys match the ones the simulation and
    the market rows use -- passing raw feed spellings would silently seed zero
    points for clubs that actually have some, which is worse than not seeding at
    all because it looks like it worked."""
    today = today or datetime.date.today()
    if league in CALENDAR_YEAR_LEAGUES:
        lo, hi = datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31)
    else:
        start_year = today.year if today.month >= 7 else today.year - 1
        lo, hi = datetime.date(start_year, 7, 1), datetime.date(start_year + 1, 6, 30)

    table: dict[str, list[int]] = {}
    played: set[tuple[str, str]] = set()
    for m in matches:
        if m.get("league") != league or m.get("home_goals_ft") is None:
            continue
        try:
            d = datetime.date.fromisoformat(str(m.get("match_date"))[:10])
        except (ValueError, TypeError):
            continue
        if not (lo <= d <= hi):
            continue
        h = canonical_team_key(m["home_team"])
        a = canonical_team_key(m["away_team"])
        hg, ag = int(m["home_goals_ft"]), int(m["away_goals_ft"])
        played.add((h, a))
        for team, gf, ga in ((h, hg, ag), (a, ag, hg)):
            row = table.setdefault(team, [0, 0, 0])
            row[1] += gf
            row[2] += ga
            if gf > ga:
                row[0] += 3
            elif gf == ga:
                row[0] += 1
    return {t: (v[0], v[1], v[2]) for t, v in table.items()}, played


def simulate_season(
    state: SoccerRatingState,
    teams: list[str],
    league: str,
    n_simulations: int = 3000,
    seed: int | None = None,
    second_tier_state: SoccerRatingState | None = None,
    starting_table: dict[str, tuple[int, int, int]] | None = None,
    played_pairs: set[tuple[str, str]] | None = None,
    starting_clean_sheets: dict[str, int] | None = None,
) -> SeasonSimResult:
    """Monte Carlo double round-robin -- every team plays every other team
    home AND away exactly once (see module docstring on why this is the
    REAL fixture list, not an approximation of one). Each pairing's goal
    distribution is computed ONCE (ratings don't change mid-simulation --
    this is a single preseason/current-strength snapshot, not a walk-forward
    training pass) and resampled `n_simulations` times, not recomputed per
    sim -- the expensive Poisson-grid math only runs O(teams^2), not
    O(teams^2 * n_simulations).

    `second_tier_state` -- this league's own second-tier rating state (e.g.
    E1 for E0), used to give a genuinely-promoted, 0-top-flight-history team
    a real rating instead of a rough placeholder (see module docstring and
    _promoted_team_rating above). Caller (soccer_markets.py) is responsible
    for fetching it via PROMOTION_SOURCE_DIVISION; None is a legitimate
    value (MLS has no second tier in this app's data) and just falls all the
    way back to the old placeholder for every unrated team."""
    rng = random.Random(seed)

    unrated_teams = [t for t in teams if state.get_count(t) == 0]
    placeholder_ratings: dict[str, tuple[float, float]] = {}
    if unrated_teams:
        known_teams = [t for t in teams if t not in unrated_teams]
        for t in unrated_teams:
            placeholder_ratings[t] = _promoted_team_rating(t, state, known_teams, second_tier_state)

    # A shallow stand-in state so predict_match sees the placeholder rating
    # for unrated teams without mutating the REAL trained state (which
    # other callers, e.g. live match pricing, still need untouched).
    class _SimState:
        def __init__(self, real: SoccerRatingState):
            self._real = real

        def get_attack(self, team: str) -> float:
            if team in placeholder_ratings:
                return placeholder_ratings[team][0]
            return self._real.get_attack(team)

        def get_concede(self, team: str) -> float:
            if team in placeholder_ratings:
                return placeholder_ratings[team][1]
            return self._real.get_concede(team)

        def league_avg_goals(self) -> float:
            return self._real.league_avg_goals()

        # MUST mirror the real state's home advantage. predict_match reads
        # `state.home_log` directly (it became per-league on 2026-08-09), and
        # this class only DUCK-TYPES SoccerRatingState -- it is not a subclass,
        # so a new attribute on the real state does not appear here and the
        # whole season sim raised AttributeError. That took every soccer
        # future to a 500 for the rest of the day.
        #
        # Delegating rather than copying is deliberate: a copy taken at
        # construction would silently go stale if the league's fitted term
        # changed, which is the same drift in slower motion.
        @property
        def home_log(self) -> float:
            return self._real.home_log

    sim_state = _SimState(state)

    # MID-SEASON. Anything already played is a FACT, not something to resample:
    # its pairing is dropped from the simulation and its real result is carried
    # in via starting_table instead. Both default to empty, which reproduces the
    # original pre-season behaviour exactly.
    #
    # WHY THIS IS NEEDED AT ALL, given the module docstring argues gameweek order
    # is irrelevant: that argument holds for a season simulated from ZERO, where
    # every fixture is still to come. It silently stops holding the moment a ball
    # is kicked. Simulating a fresh 38-round season in August for a league that
    # has played 20 rounds throws away the table -- a runaway leader is modelled
    # as level with everyone. Measured 2026-08-09: Brasileirao was 20.5 of 38
    # rounds in (54%) and the Chinese Super League 20.5 of 30 (68%), which is
    # what surfaced this. The five original European leagues hid it because they
    # were between seasons every time this code had been exercised.
    played = played_pairs or set()
    pairings: dict[tuple[str, str], tuple[list[tuple[int, int]], list[float]]] = {}
    for home in teams:
        for away in teams:
            if home == away or (home, away) in played:
                continue
            dist = predict_match(sim_state, home, away)  # type: ignore[arg-type]
            pairings[(home, away)] = _cumulative_weights(dist.grid)

    champion_count = {t: 0 for t in teams}
    top4_count = {t: 0 for t in teams}
    top2_count = {t: 0 for t in teams}
    top_half_count = {t: 0 for t in teams}
    relegation_count = {t: 0 for t in teams}
    # Final points per simulated season, per team. The sim already computed this
    # to do the ranking and then discarded it; keeping it is what makes Kalshi's
    # "<team> finishes with N+ points" ladders (KX*TEAMPOINTS, 384 live markets
    # across 5 leagues) priceable, with no second model and no extra simulation.
    points_count: dict[str, dict[int, int]] = {t: {} for t in teams}
    position_count: dict[str, dict[int, int]] = {t: {} for t in teams}
    # Fractional because a tie splits the market evenly among co-leaders.
    clean_sheet_leader: dict[str, float] = {t: 0.0 for t in teams}
    # A part-played season's clean sheets are NOT recoverable from
    # starting_table (points/goals only), so unless the caller supplies them the
    # count would cover simulated matches alone and quietly understate every
    # team. Emit nothing in that case rather than a wrong number.
    clean_sheets_known = not starting_table or starting_clean_sheets is not None
    zone_size = RELEGATION_ZONE_SIZE.get(league)
    half_size = len(teams) // 2

    for _ in range(n_simulations):
        results = {}
        for t in teams:
            pts, gf, ga = (starting_table or {}).get(t, (0, 0, 0))
            r = TeamSeasonResult(team=t)
            r.points, r.goals_for, r.goals_against = pts, gf, ga
            r.clean_sheets = (starting_clean_sheets or {}).get(t, 0)
            results[t] = r
        for (home, away), (outcomes, cum_weights) in pairings.items():
            idx = bisect.bisect_left(cum_weights, rng.random() * cum_weights[-1])
            idx = min(idx, len(outcomes) - 1)
            h_goals, a_goals = outcomes[idx]
            results[home].goals_for += h_goals
            results[home].goals_against += a_goals
            results[away].goals_for += a_goals
            results[away].goals_against += h_goals
            if a_goals == 0:
                results[home].clean_sheets += 1
            if h_goals == 0:
                results[away].clean_sheets += 1
            if h_goals > a_goals:
                results[home].points += 3
            elif h_goals < a_goals:
                results[away].points += 3
            else:
                results[home].points += 1
                results[away].points += 1

        for t, r in results.items():
            pc = points_count[t]
            pc[r.points] = pc.get(r.points, 0) + 1

        ranked = sorted(results.values(), key=lambda r: (-r.points, -r.goal_diff, -r.goals_for))
        for pos, r in enumerate(ranked, start=1):
            pd = position_count[r.team]
            pd[pos] = pd.get(pos, 0) + 1
        if clean_sheets_known:
            best_cs = max(r.clean_sheets for r in results.values())
            leaders = [r.team for r in results.values() if r.clean_sheets == best_cs]
            share = 1.0 / len(leaders)
            for t in leaders:
                clean_sheet_leader[t] += share
        champion_count[ranked[0].team] += 1
        for r in ranked[:2]:
            top2_count[r.team] += 1
        for r in ranked[:4]:
            top4_count[r.team] += 1
        for r in ranked[:half_size]:
            top_half_count[r.team] += 1
        if zone_size:
            for r in ranked[-zone_size:]:
                relegation_count[r.team] += 1

    return SeasonSimResult(
        teams=teams,
        champion_prob={t: champion_count[t] / n_simulations for t in teams},
        top4_prob={t: top4_count[t] / n_simulations for t in teams},
        top2_prob={t: top2_count[t] / n_simulations for t in teams},
        top_half_prob={t: top_half_count[t] / n_simulations for t in teams},
        relegation_prob={t: relegation_count[t] / n_simulations for t in teams} if zone_size else {},
        points_dist=points_count,
        position_dist={t: {p: c / n_simulations for p, c in position_count[t].items()}
                       for t in teams},
        most_clean_sheets_prob=({t: clean_sheet_leader[t] / n_simulations for t in teams}
                                if clean_sheets_known else {}),
        unrated_teams=unrated_teams,
        n_simulations=n_simulations,
    )


def prob_points_at_least(result: "SeasonSimResult", team: str, threshold: float) -> float | None:
    """P(team finishes the season on at least `threshold` points).

    Kalshi states these as a floor_strike of N-0.5 for an "N+ points" market, so
    the comparison is >= ceil(threshold): a 74.5 floor means 75 points or more.
    Returns None for a team the sim doesn't cover (newly promoted sides the
    ratings can't place, which are already tracked in unrated_teams) rather than
    guessing off the league average."""
    dist = (result.points_dist or {}).get(team)
    if not dist or not result.n_simulations:
        return None
    cutoff = math.ceil(threshold)
    hits = sum(c for pts, c in dist.items() if pts >= cutoff)
    return hits / result.n_simulations
