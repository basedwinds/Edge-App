"""Soccer team rating engine -- deliberately NOT a logistic win-probability
Elo like every other sport's elo_<sport>.py (elo.py/elo_nba.py/elo_mlb.py/
elo_mma.py) or even elo_tennis.py's win_prob() formula. Soccer's market scope
here is moneyline (3-way Home/Draw/Away) + spread + totals, all three of
which are naturally answered by a GOALS distribution, not a single win
probability -- so this is a walk-forward attack/defense Poisson rating
(Dixon-Coles family), same "sport-specific by design" spirit as elo.py's own
docstring calls for, just a different underlying technique because soccer's
market shape is genuinely different (a first-class draw outcome; spread/
total both being goals-denominated).

Ratings are stored in LOG space (attack_log/concede_log, both 0.0 = league-
average team) so a team can never rate as "negative goals" and the update
rule is a simple, standard stochastic-gradient step on a Poisson log-link
(`d(loglik)/d(log mu) = actual - mu`, so `log_rating += K * (actual_goals -
expected_goals)` is a legitimate online approximation of a Poisson GLM's
score function, not an invented formula) -- exactly the same "walk-forward,
predict before update, no leakage" pattern as elo_tennis.py's
predict_and_update, just applied to two goal counts instead of one binary
outcome.

K_ATTACK/K_DEFENSE/HOME_ADVANTAGE_LOG below were GRID-SEARCHED fresh in this
app's own harness (2026-07-19, scripts/grid_search_soccer_constants.py)
against the same 61,144-match walk-forward backtest scripts/
backtest_moneyline_soccer.py itself uses, same discipline as elo_tennis.py's
own SURFACE_MATCH_CAP/MAX_SURFACE_WEIGHT search. UNLIKE that search (which
only affected the prediction blend), these three affect walk-forward
TRAINING dynamics directly, so every grid cell needed its own full retrain
-- no cheap one-pass-then-rescore shortcut available.

Real result: a genuinely smooth basin for both parameters (not a noisy
single-cell spike) -- HOME_ADVANTAGE_LOG's pooled 3-way Brier moves
0.5999 (0.05) -> 0.5957 (0.20, the minimum) -> 0.6090 (0.45), a clean U-shape;
K's Brier moves 0.6040 (0.01) -> 0.5939 (0.03, the minimum) -> 0.6529 (0.25),
also a clean, monotonic-ish trend either side of the minimum. K_ATTACK and
K_DEFENSE were then also grid-searched INDEPENDENTLY (7x7 grid) starting
from that joint optimum -- no further improvement found, confirming a
single shared K is not leaving real signal on the table by being forced
symmetric.

Borrowed-starting-point Brier (HOME_ADVANTAGE_LOG=0.25, K=0.05): 0.5964
pooled. Grid-searched Brier (HOME_ADVANTAGE_LOG=0.20, K=0.03): 0.5939
pooled -- a real, validated improvement to the baseline's OWN internal
quality (same category of finding as Tennis's surface-weight fix), but NOT
enough to flip the standing GO/NO-GO conclusion: the market's own Brier is
still ~0.577, so this remains a NO-GO baseline, re-run with these new
constants (see backtest_moneyline_soccer.py's own module docstring for the
full per-league numbers)."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("elo_soccer")

BASE_GOALS_PER_TEAM = 1.35  # rough prior (commonly-published European top-flight average), overridden by the running empirical mean below almost immediately
K_ATTACK = 0.03
K_DEFENSE = 0.03
HOME_ADVANTAGE_LOG = 0.20
SEASON_REGRESSION = 1.0 / 3.0  # fraction of the way back to league-average between seasons -- same constant/rationale as elo.py's NFL SEASON_REGRESSION (squads turn over every transfer window, not a fresh derivation for soccer specifically). NOT included in the 2026-07-19 grid search (only affects the between-season carryover, not scored by this app's own walk-forward Brier the same direct way K/home-advantage are) -- still a borrowed starting point, flagged honestly.
MAX_GOALS = 10  # joint distribution grid cap per side -- P(11+ goals) is negligible for any real team-strength gap this rating produces

# PER-LEAGUE home advantage overrides, fitted and held-out-validated by
# scripts/fit_soccer_home_advantage.py. A league absent from this file uses
# HOME_ADVANTAGE_LOG above, which the fit script's era table shows is already
# unbiased on modern football -- the file is deliberately near-empty, and that
# emptiness is the RESULT, not a gap. Do not hand-edit it; re-run the fitter.
_HOME_ADVANTAGE_PATH = Path(__file__).with_name("soccer_home_advantage.json")


def _load_home_advantage() -> dict[str, float]:
    try:
        raw = json.loads(_HOME_ADVANTAGE_PATH.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in raw.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        # A malformed override file must not silently re-tilt every league --
        # fall back to the validated global constant and say so.
        log.exception("could not read %s -- falling back to the global home advantage", _HOME_ADVANTAGE_PATH.name)
        return {}


HOME_ADVANTAGE_BY_LEAGUE: dict[str, float] = _load_home_advantage()


def home_advantage_for_league(league: str | None) -> float:
    """The home term to build a league's rating state with. Falls back to the
    global constant for every league without a validated override."""
    return HOME_ADVANTAGE_BY_LEAGUE.get(league or "", HOME_ADVANTAGE_LOG)


def _poisson_pmf(k: int, lam: float) -> float:
    lam = max(lam, 1e-6)
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def _build_grid(expected_home: float, expected_away: float) -> list[list[float]]:
    """Independent-Poisson joint grid[h][a] = P(home=h, away=a) -- factored
    out of predict_match (2026-07-19) so predict_half below can build a
    real, derated-expected-goals grid through the exact same math, not a
    re-derived copy of it."""
    home_pmf = [_poisson_pmf(h, expected_home) for h in range(MAX_GOALS + 1)]
    away_pmf = [_poisson_pmf(a, expected_away) for a in range(MAX_GOALS + 1)]
    return [[home_pmf[h] * away_pmf[a] for a in range(MAX_GOALS + 1)] for h in range(MAX_GOALS + 1)]


@dataclass
class SoccerRatingState:
    """One instance per league (see elo_service_soccer.py) -- an EPL team's
    attack rating isn't comparable to a Ligue 1 team's without cross-league
    goal-scoring-rate normalization, which is out of scope for v1 (see the
    approved build plan)."""

    attack_log: dict = field(default_factory=dict)  # team -> log scoring strength, 0.0 = league average
    concede_log: dict = field(default_factory=dict)  # team -> log conceding tendency, 0.0 = league average, HIGHER = leakier defense
    match_counts: dict = field(default_factory=dict)  # team -> n matches rated
    goals_sum: float = 0.0  # running total goals scored (both sides, every match) -- for the walk-forward league-average prior
    goals_n: int = 0  # running total team-match observations backing goals_sum
    current_season: str | None = None
    # PER-LEAGUE home advantage, defaulting to the global grid-searched value.
    # Home advantage genuinely differs by country and a single constant left 17
    # of 28 leagues systematically tilted (Greece and Brazil by +6.3pp, Japan
    # -2.5pp the other way) -- see scripts/audit_soccer_leagues.py. Carried on
    # the STATE rather than passed to predict_match because there is already one
    # state per league, so it needs no call-site changes and cannot be forgotten
    # by a caller.
    home_log: float = HOME_ADVANTAGE_LOG

    def get_attack(self, team: str) -> float:
        return self.attack_log.get(team, 0.0)

    def get_concede(self, team: str) -> float:
        return self.concede_log.get(team, 0.0)

    def get_count(self, team: str) -> int:
        return self.match_counts.get(team, 0)

    def league_avg_goals(self) -> float:
        if self.goals_n == 0:
            return BASE_GOALS_PER_TEAM
        return self.goals_sum / self.goals_n

    def start_season_if_new(self, season: str) -> None:
        if self.current_season is not None and season != self.current_season:
            for team in list(self.attack_log.keys()):
                self.attack_log[team] *= (1 - SEASON_REGRESSION)
            for team in list(self.concede_log.keys()):
                self.concede_log[team] *= (1 - SEASON_REGRESSION)
        self.current_season = season


@dataclass
class MatchGoalDistribution:
    """Joint P(home_goals=h, away_goals=a) grid, independent Poisson --
    STILL no Dixon-Coles low-score correlation adjustment, now for a real,
    checked reason rather than just deferral. scripts/investigate_soccer_
    dixon_coles.py tested the exact condition this app's own build plan set
    for adding it (2026-07-19, walk-forward against the real 61,144-match
    cache): actual/predicted ratios for the 4 scorelines Dixon-Coles
    corrects were (0,0)=1.125, (1,0)=1.046, (0,1)=0.841, (1,1)=1.065. Three
    of four move in the direction Dixon-Coles predicts (low scores under-
    predicted by independent Poisson) -- but (0,1) moves the OPPOSITE
    direction, which the standard Dixon-Coles tau correction can't produce
    (it moves (1,0) and (0,1) the SAME direction, scaled by each side's own
    expected goals -- there's no tau parameterization that pushes them
    apart). A real, mixed signal, not a clean confirmation -- adding the
    correction without first root-causing the (0,1) anomaly (possibly a
    small home-advantage calibration artifact instead, not a real scoreline
    correlation) risks fitting the wrong thing. Deferred, not built, same
    "don't force in an ambiguous fix" discipline as this app's other
    declined signals (e.g. MMA's rejected referee-tendency/stance features)."""

    expected_home_goals: float
    expected_away_goals: float
    grid: list[list[float]]  # grid[h][a] = P(home=h, away=a), h/a in [0, MAX_GOALS]

    def prob_home_win(self) -> float:
        return sum(self.grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if h > a)

    def prob_draw(self) -> float:
        return sum(self.grid[h][h] for h in range(MAX_GOALS + 1))

    def prob_away_win(self) -> float:
        return sum(self.grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if h < a)

    def prob_total_over(self, line: float) -> float:
        """`line` is a half-integer (e.g. 2.5) -- matches Kalshi/football-data.co.uk's own O/U convention, so no push case to handle."""
        return sum(self.grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if (h + a) > line)

    def prob_total_under(self, line: float) -> float:
        return 1.0 - self.prob_total_over(line)

    def prob_home_spread_cover(self, line: float) -> float:
        """`line` follows this app's own spread convention (see game_lines.py
        in other sports): home team's margin (home_goals - away_goals) must
        exceed -line to cover a line of `line` (e.g. line=-1.5 means home
        must win by 2+)."""
        return sum(self.grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if (h - a) > -line)

    def prob_btts(self) -> float:
        """Both Teams To Score -- P(home_goals >= 1 AND away_goals >= 1).
        Real, live Kalshi market confirmed for MLS (KXMLSBTTS, 2026-07-19) --
        answered directly by the same joint grid every other market type
        here uses, no new model needed."""
        return sum(self.grid[h][a] for h in range(1, MAX_GOALS + 1) for a in range(1, MAX_GOALS + 1))

    def prob_correct_score(self, home_score: int, away_score: int) -> float:
        """Real, live Kalshi/Polymarket market confirmed for MLS (KXMLSSCORE
        / "-exact-score", 2026-07-19, a real 30-rung ladder e.g. "2-1") --
        this is directly grid[h][a], already computed for every match, never
        exposed as its own market before now. Clamped to MAX_GOALS since a
        real market can in principle post a scoreline above this grid's cap
        (astronomically unlikely, P(11+) is negligible for any real
        team-strength gap, same reasoning MAX_GOALS's own comment gives)."""
        h = min(home_score, MAX_GOALS)
        a = min(away_score, MAX_GOALS)
        return self.grid[h][a]

    def prob_team_total_over(self, side: str, line: float) -> float:
        """Real, live Kalshi market confirmed for MLS (KXMLSTEAMTOTAL,
        2026-07-19) -- one side's OWN goal total, not the combined total
        prob_total_over already answers. A marginal of the same joint grid
        (summing out the OTHER side), not a new model."""
        if side == "home":
            return sum(self.grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if h > line)
        return sum(self.grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if a > line)

    def prob_first_to_score(self) -> tuple[float, float, float]:
        """Real, live Kalshi/Polymarket market confirmed for MLS (KXMLSFTTS
        / "-first-to-score", 2026-07-19: Home / Away / "No Goal", a real
        3-way shape). Standard competing-exponentials result: if goals from
        each side arrive as independent Poisson processes with real rates
        expected_home_goals/expected_away_goals, then conditional on at
        least one goal being scored, P(home scores first) is exactly the
        share of the two combined rates belonging to home -- a textbook
        result, not an invented formula. P(no goal at all) is already
        grid[0][0], the same joint grid every other market type here uses.
        Returns (p_home_first, p_away_first, p_no_goal)."""
        p_no_goal = self.grid[0][0]
        total_rate = self.expected_home_goals + self.expected_away_goals
        if total_rate <= 0:
            return 0.0, 0.0, p_no_goal
        p_at_least_one = 1.0 - p_no_goal
        p_home_first = (self.expected_home_goals / total_rate) * p_at_least_one
        p_away_first = (self.expected_away_goals / total_rate) * p_at_least_one
        return p_home_first, p_away_first, p_no_goal


def predict_match(state: SoccerRatingState, home_team: str, away_team: str) -> MatchGoalDistribution:
    # concede_log uses a LEAKINESS convention (higher = weaker defense =
    # concedes MORE), matching how update_ratings naturally moves it (a
    # team that just conceded more than expected gets its concede_log
    # nudged UP) -- so a leaky opponent's concede_log must be ADDED here,
    # not subtracted. REAL BUG this fixes: an earlier version subtracted
    # concede_log, which silently flipped the sign relative to
    # update_ratings' own convention -- ratings still trained "successfully"
    # (no crash) but every prediction ended up ANTI-correlated with the
    # trained signal. Caught via scripts/backtest_moneyline_soccer.py's real
    # numbers, not inspection: favorite-accuracy of ~29% pooled across
    # 12,459 real scored matches (worse than always guessing "away win",
    # let alone the market's own 54%) is far below what even a purely
    # noise-level model would produce, which is what made this a proven sign
    # bug rather than just "no real edge" (2026-07-19).
    avg_goals = state.league_avg_goals()
    expected_home = avg_goals * math.exp(state.get_attack(home_team) + state.get_concede(away_team) + state.home_log)
    expected_away = avg_goals * math.exp(state.get_attack(away_team) + state.get_concede(home_team))
    grid = _build_grid(expected_home, expected_away)
    return MatchGoalDistribution(expected_home_goals=expected_home, expected_away_goals=expected_away, grid=grid)


# Real, data-grounded first-half goal share -- derived 2026-07-19 from
# 110,566 real matches in this app's own football-data.co.uk cache (every
# match with a real recorded half-time score): 43.94% of a match's total
# goals happen in the first half overall, with home (44.32%) and away
# (43.43%) close enough that using two separate constants (rather than one
# pooled number) is a real, if small, refinement, not noise -- both splits
# came from the same 110,566-match sample. A flat share applied uniformly
# to any match's own full-match expected goals is a real structural
# SIMPLIFICATION, not a per-team-fitted model (a team's own real scoring
# tempo could plausibly skew earlier/later than this league-wide average --
# no per-team first-half-share signal has been tested), flagged the same
# "auditable, not precisely estimated" way as this app's other situational
# constants.
FIRST_HALF_SHARE_HOME = 0.4432
FIRST_HALF_SHARE_AWAY = 0.4343


def predict_half(state: SoccerRatingState, home_team: str, away_team: str, half: int) -> MatchGoalDistribution:
    """Real, live Kalshi/Polymarket markets confirmed for First Half (both
    platforms) and Second Half (Kalshi: EPL/La Liga only; Polymarket: MLS)
    -- see kalshi_soccer_client.py/polymarket_soccer_client.py's own
    docstrings for which platform/league combinations actually have real
    open inventory. `half` is 1 or 2; derates the SAME full-match
    expected-goals numbers predict_match produces by FIRST_HALF_SHARE_HOME/
    AWAY (half=1) or their complement (half=2), then rebuilds the grid
    through the identical _build_grid helper -- not a separately-trained
    model, since this app has no real half-by-half TRAINING signal (attack/
    defense ratings are fit on full-match results only)."""
    full = predict_match(state, home_team, away_team)
    if half == 1:
        expected_home = full.expected_home_goals * FIRST_HALF_SHARE_HOME
        expected_away = full.expected_away_goals * FIRST_HALF_SHARE_AWAY
    else:
        expected_home = full.expected_home_goals * (1 - FIRST_HALF_SHARE_HOME)
        expected_away = full.expected_away_goals * (1 - FIRST_HALF_SHARE_AWAY)
    grid = _build_grid(expected_home, expected_away)
    return MatchGoalDistribution(expected_home_goals=expected_home, expected_away_goals=expected_away, grid=grid)


RATING_CLAMP = 2.5  # log-space bound (exp(2.5) = 12.2x a league-average team) -- see REAL BUG note below, generous enough it should never bind for an actual real-strength gap, only as a hard safety rail
MAX_LOG_STEP = 0.5  # per-match rating-delta cap, same safety-rail role as RATING_CLAMP


def _pearson_residual(actual: int, expected: float) -> float:
    """REAL BUG this fixes: an earlier version of this function used the RAW
    goal residual (actual - expected) directly as the update signal. Under a
    log-link, expected_goals grows EXPONENTIALLY with a team's rating, so a
    team on a real hot/cold scoring streak (confirmed live: some MLS/lower-
    division sequences in this app's own cache) could enter a positive-
    feedback spiral -- higher rating -> higher expected_goals -> next
    real-if-unusual blowout produces an even larger raw residual -> even
    bigger rating jump -- that reproduced as a genuine `OverflowError` in
    `math.exp()` when running scripts/backtest_moneyline_soccer.py against
    the real 61,144-match cache (2026-07-19), not a hypothetical concern.
    The Pearson residual (raw residual scaled by sqrt(variance), and Poisson
    variance = its own mean) is the STANDARD way to normalize a GLM update so
    its scale doesn't blow up with the mean -- this is the textbook fix, not
    an invented workaround. RATING_CLAMP/MAX_LOG_STEP below are additional
    hard safety rails on top of this real fix, not a replacement for it."""
    return (actual - expected) / math.sqrt(max(expected, 0.5))


def _clamped_step(current: float, raw_step: float) -> float:
    step = max(-MAX_LOG_STEP, min(MAX_LOG_STEP, raw_step))
    return max(-RATING_CLAMP, min(RATING_CLAMP, current + step))


def update_ratings(state: SoccerRatingState, home_team: str, away_team: str, home_goals: int, away_goals: int) -> None:
    dist = predict_match(state, home_team, away_team)
    home_attack_resid = _pearson_residual(home_goals, dist.expected_home_goals)
    away_attack_resid = _pearson_residual(away_goals, dist.expected_away_goals)

    state.attack_log[home_team] = _clamped_step(state.get_attack(home_team), K_ATTACK * home_attack_resid)
    state.concede_log[away_team] = _clamped_step(state.get_concede(away_team), K_DEFENSE * home_attack_resid)
    state.attack_log[away_team] = _clamped_step(state.get_attack(away_team), K_ATTACK * away_attack_resid)
    state.concede_log[home_team] = _clamped_step(state.get_concede(home_team), K_DEFENSE * away_attack_resid)

    state.match_counts[home_team] = state.get_count(home_team) + 1
    state.match_counts[away_team] = state.get_count(away_team) + 1
    state.goals_sum += home_goals + away_goals
    state.goals_n += 2


def predict_and_update(state: SoccerRatingState, match: dict) -> MatchGoalDistribution | None:
    """Returns the PRE-match goal distribution (walk-forward, no leakage),
    then updates ratings with the actual result if the match has a real
    final score. `match` is the soccer_data.py match-dict shape. Returns
    None (no prediction, no update) for a not-yet-played match."""
    home_team, away_team = match["home_team"], match["away_team"]
    season = match.get("season")
    if season is not None:
        state.start_season_if_new(season)
    dist = predict_match(state, home_team, away_team)

    home_goals, away_goals = match.get("home_goals_ft"), match.get("away_goals_ft")
    if home_goals is None or away_goals is None:
        return None
    update_ratings(state, home_team, away_team, home_goals, away_goals)
    return dist
