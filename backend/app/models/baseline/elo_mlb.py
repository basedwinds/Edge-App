"""MLB Elo rating engine -- same walk-forward pattern as elo.py (NFL)/
elo_nba.py, every constant grid-searched or derived from this app's own
cached 10-season MLB Stats API dataset (2016-2025,
data/mlb_schedule_cache.json, 23,257 REG games, 22,796 with a final score
excluding 2026's still-in-progress season).

HOME_FIELD_ADV: raw home win rate across all 22,796 games is 53.22% --
notably smaller than NFL's 55-point HFA or NBA's 48-point HFA. Matches
baseball's well-documented reputation as the major US sport with the
smallest home-field edge (the starting-pitcher matchup, not home crowd/
travel, dominates single-game variance -- see pitcher_ratings_mlb.py).
Inverting the standard logistic gives ~22.4 Elo points, rounded to 22.0.

K and SEASON_REGRESSION: grid-searched (K in {2..10,15,18,20,25}, regression
in {0,.1,.15,...,.5,.67,1.0}) against this app's own walk-forward Brier
score. K=5 is a clean, well-behaved minimum (0.24265 Brier) -- a smooth basin
around K=4-7/regression=0.2-0.35, not a noisy spike, and MUCH lower than
NFL's/NBA's shared K=20 starting point. This is a real, expected difference,
not a bug: a 162-game MLB season is roughly 10x an NFL season's length, so
each individual game should move a team's rating far less than in a
16-20-game sport for the rating to still converge sensibly across a season.

MOV multiplier: explicitly built and TESTED, then REJECTED on real data, same
"real signal doesn't automatically survive contact" discipline as this
project's NFL turnover-margin-regression experiment (see
[[project_unified_prediction_market_app]] memory). At matched (K=20,
regression=1/3), the flat-K (no MOV) version beat the MOV version (0.2461 vs
0.2485 Brier) outright, and grid-searching MOV to its OWN best K/regression
(K=5, regression=1/3, Brier=0.24244) only ties flat-K's own best (K=5,
regression=0.25, Brier=0.24265) within noise -- not a real improvement,
just a different point in the same basin. Confirms the intuition behind
538's own public MLB methodology skipping a MOV multiplier: a single bullpen
implosion or extra-innings walk-off run differential doesn't reliably reflect
the true talent gap the way an NFL/NBA scoring margin does. use_mov defaults
to False; the formula is kept below only so a future re-check has a concrete
starting point, not because it's recommended.
"""
import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0
K = 5.0
HOME_FIELD_ADV = 22.0
SEASON_REGRESSION = 0.25
NEUTRAL_SITE_HOME_FIELD_ADV = 0.0  # MLB plays a handful of real neutral-site games/season (London, Mexico City, Korea)


@dataclass
class EloState:
    ratings: dict = field(default_factory=dict)
    current_season: int | None = None

    def get(self, team: str) -> float:
        return self.ratings.get(team, BASE_RATING)

    def start_season_if_new(self, season: int):
        if self.current_season is not None and season != self.current_season:
            for team in list(self.ratings.keys()):
                self.ratings[team] = BASE_RATING + (1 - SEASON_REGRESSION) * (self.ratings[team] - BASE_RATING)
        self.current_season = season


def win_prob(home_rating: float, away_rating: float, home_field_adv: float = HOME_FIELD_ADV) -> float:
    diff = (home_rating + home_field_adv) - away_rating
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def implied_elo_diff(prob: float) -> float:
    """Inverse of win_prob -- same role as elo.py's/elo_nba.py's identical
    helper (feeds a future news-adjusted margin-space run-line model without
    hand-building a second version of every situational factor)."""
    prob = min(max(prob, 1e-6), 1 - 1e-6)
    return 400.0 * math.log10(prob / (1.0 - prob))


def mov_multiplier(run_diff: float, elo_diff_winner_perspective: float) -> float:
    """Tested and REJECTED on real data -- see module docstring. Kept only as
    a documented starting point for a future re-check, not recommended
    (use_mov defaults to False everywhere below)."""
    return math.log(abs(run_diff) + 1.0) * (2.2 / ((elo_diff_winner_perspective * 0.001) + 2.2))


def update_ratings(
    state: EloState,
    home: str,
    away: str,
    home_score: int,
    away_score: int,
    home_field_adv: float = HOME_FIELD_ADV,
    use_mov: bool = False,
):
    home_r = state.get(home)
    away_r = state.get(away)
    p_home = win_prob(home_r, away_r, home_field_adv)

    if home_score > away_score:
        actual_home = 1.0
    elif home_score < away_score:
        actual_home = 0.0
    else:
        actual_home = 0.5  # rare in MLB (extra-innings games always resolve) but kept for symmetry/safety

    if use_mov:
        run_diff = home_score - away_score
        elo_diff_winner_perspective = (
            (home_r + home_field_adv - away_r) if run_diff >= 0 else (away_r - home_field_adv - home_r)
        )
        mult = mov_multiplier(run_diff if run_diff != 0 else 1, elo_diff_winner_perspective)
    else:
        mult = 1.0

    delta = K * mult * (actual_home - p_home)
    state.ratings[home] = home_r + delta
    state.ratings[away] = away_r - delta


def predict_and_update(state: EloState, game: dict, use_mov: bool = False, pitcher_adj: float = 0.0) -> float | None:
    """Returns the PRE-game home win probability (walk-forward, no leakage),
    then updates ratings with the actual result if the game has a final
    score. `pitcher_adj` (Elo points, see pitcher_ratings_mlb.py) shifts only
    the RETURNED prediction, never the rating UPDATE below -- team Elo stays
    a pure team-strength signal regardless of which pitcher started, the same
    way this project keeps EPA-mismatch/turnover-margin-style signals out of
    the NFL Elo update itself. Blending them into the update would let a
    great start by a scrub pitcher permanently inflate a bad team's rating."""
    state.start_season_if_new(game["season"])
    home_field_adv = NEUTRAL_SITE_HOME_FIELD_ADV if game.get("location") == "Neutral" else HOME_FIELD_ADV
    home_r = state.get(game["home_team"])
    away_r = state.get(game["away_team"])
    p_home = win_prob(home_r + pitcher_adj, away_r, home_field_adv)

    if game.get("home_score") is not None and game.get("away_score") is not None:
        update_ratings(
            state, game["home_team"], game["away_team"], game["home_score"], game["away_score"],
            home_field_adv, use_mov=use_mov,
        )

    return p_home
