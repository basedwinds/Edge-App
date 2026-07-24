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
import random
from dataclasses import dataclass, field

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
    unrated_teams: list[str] = field(default_factory=list)
    n_simulations: int = 0


def simulate_season(
    state: SoccerRatingState,
    teams: list[str],
    league: str,
    n_simulations: int = 3000,
    seed: int | None = None,
    second_tier_state: SoccerRatingState | None = None,
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

    sim_state = _SimState(state)

    pairings: dict[tuple[str, str], tuple[list[tuple[int, int]], list[float]]] = {}
    for home in teams:
        for away in teams:
            if home == away:
                continue
            dist = predict_match(sim_state, home, away)  # type: ignore[arg-type]
            pairings[(home, away)] = _cumulative_weights(dist.grid)

    champion_count = {t: 0 for t in teams}
    top4_count = {t: 0 for t in teams}
    top2_count = {t: 0 for t in teams}
    top_half_count = {t: 0 for t in teams}
    relegation_count = {t: 0 for t in teams}
    zone_size = RELEGATION_ZONE_SIZE.get(league)
    half_size = len(teams) // 2

    for _ in range(n_simulations):
        results = {t: TeamSeasonResult(team=t) for t in teams}
        for (home, away), (outcomes, cum_weights) in pairings.items():
            idx = bisect.bisect_left(cum_weights, rng.random() * cum_weights[-1])
            idx = min(idx, len(outcomes) - 1)
            h_goals, a_goals = outcomes[idx]
            results[home].goals_for += h_goals
            results[home].goals_against += a_goals
            results[away].goals_for += a_goals
            results[away].goals_against += h_goals
            if h_goals > a_goals:
                results[home].points += 3
            elif h_goals < a_goals:
                results[away].points += 3
            else:
                results[home].points += 1
                results[away].points += 1

        ranked = sorted(results.values(), key=lambda r: (-r.points, -r.goal_diff, -r.goals_for))
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
        unrated_teams=unrated_teams,
        n_simulations=n_simulations,
    )
