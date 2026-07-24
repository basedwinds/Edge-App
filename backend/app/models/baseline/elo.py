"""538-style NFL Elo rating engine.

Sport-specific by design (NFL scoring/margin dynamics are baked into the MOV
multiplier), but the *pattern* -- chronological walk-forward rating updates
feeding a probability model -- is meant to be replicated per-sport, not
special-cased into shared layers. See feedback_edge_finder_scope_and_polish
memory: this file is where "NFL-specific" is supposed to live.
"""
import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0
K = 20.0
HOME_FIELD_ADV = 55.0  # Elo points
SEASON_REGRESSION = 1.0 / 3.0  # fraction of the way back to the mean between seasons

# International-series/relocated games are flagged "Neutral" in nflverse's own
# `location` column (games.csv) -- the nominal "home" team gets zero home-field
# credit there, both when predicting AND when the Elo update learns from the
# result (otherwise a team could bank undeserved rating gain from "winning at
# home" at Wembley/Munich/etc).
NEUTRAL_SITE_HOME_FIELD_ADV = 0.0

# Denver sits at ~5,280ft, the NFL's only real-elevation stadium -- a well-
# documented visiting-team disadvantage. A raw (NOT team-quality-adjusted)
# comparison of DEN's historical home win rate (62.6%, n=219 REG games) vs.
# every other team's (56.0%, n=6682) shows a real gap, but it's confounded
# with DEN's own team strength varying across those seasons, so this bonus is
# a deliberately conservative fraction of that raw gap rather than a fitted
# number -- same "rough, auditable constant" spirit as this project's other
# hand-picked situational constants (e.g. POSITION_WEIGHTS_PP in
# injury_rules.py). Worth revisiting with a team-quality-adjusted estimate in
# Phase 7 backtesting.
DENVER_ALTITUDE_BONUS_ELO = 20.0


def effective_home_field_adv(home_team: str, location: str | None) -> float:
    if location == "Neutral":
        return NEUTRAL_SITE_HOME_FIELD_ADV
    if home_team == "DEN":
        return HOME_FIELD_ADV + DENVER_ALTITUDE_BONUS_ELO
    return HOME_FIELD_ADV


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
    """Inverse of win_prob -- returns the rating-difference-plus-home-field
    quantity that would produce this probability via the standard logistic.
    Used to fold the situational/news layer (defined in win-probability
    space, see combine.py::combine_probability) into game_lines.py's
    margin-space spread model: compute the news-adjusted win probability the
    same way moneyline already does, then invert it back to an effective
    elo_diff, rather than hand-tuning a second, separate margin-space
    version of every situational factor."""
    prob = min(max(prob, 1e-6), 1 - 1e-6)
    return 400.0 * math.log10(prob / (1.0 - prob))


def mov_multiplier(point_diff: float, elo_diff_winner_perspective: float) -> float:
    # 538 NFL formula
    return math.log(abs(point_diff) + 1.0) * (2.2 / ((elo_diff_winner_perspective * 0.001) + 2.2))


def update_ratings(
    state: EloState, home: str, away: str, home_score: int, away_score: int, home_field_adv: float = HOME_FIELD_ADV
):
    home_r = state.get(home)
    away_r = state.get(away)
    p_home = win_prob(home_r, away_r, home_field_adv)

    if home_score > away_score:
        actual_home = 1.0
    elif home_score < away_score:
        actual_home = 0.0
    else:
        actual_home = 0.5

    point_diff = home_score - away_score
    elo_diff_winner_perspective = (
        (home_r + home_field_adv - away_r) if point_diff >= 0 else (away_r - home_field_adv - home_r)
    )
    mult = mov_multiplier(point_diff if point_diff != 0 else 1, elo_diff_winner_perspective)

    delta = K * mult * (actual_home - p_home)
    state.ratings[home] = home_r + delta
    state.ratings[away] = away_r - delta


def predict_and_update(state: EloState, game: dict) -> float | None:
    """Returns the PRE-game home win probability (walk-forward, no leakage),
    then updates ratings with the actual result if the game has a final score."""
    state.start_season_if_new(game["season"])
    home_field_adv = effective_home_field_adv(game["home_team"], game.get("location"))
    home_r = state.get(game["home_team"])
    away_r = state.get(game["away_team"])
    p_home = win_prob(home_r, away_r, home_field_adv)

    if game.get("home_score") is not None and game.get("away_score") is not None:
        update_ratings(
            state, game["home_team"], game["away_team"], game["home_score"], game["away_score"], home_field_adv
        )

    return p_home
