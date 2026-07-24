"""WNBA Elo rating engine -- parallel to elo_nba.py, but the constants come
from this app's own WNBA build (scripts/derive_wnba_elo.py over
data/wnba_game_cache.json, 1,540 games 2021-2026) and the model is kept
PLAIN (no margin-of-victory multiplier, no altitude/rest corrections) so the
shipped model is exactly the one that was walk-forward validated:
K=32, home-court adv 30 Elo pts -> Brier 0.222 / 65.4% accuracy.

Measured edge vs the market (scripts/backtest_wnba_market.py, 159 KXWNBAGAME
games with real pre-tip closing prices): the market beats this model by 0.008
Brier -- same modest no-average-edge result as every other sport in this app.
Ships model_validated=False accordingly; real edge only from selective spots +
forward CLV, never a flat allocation off model-vs-market disagreement. MOV /
short-rest / neutral-site-strength refinements are deliberately deferred (the
plain model is what's validated) -- add only if a re-derivation shows a real
Brier gain, same discipline as the NBA build.
"""
from dataclasses import dataclass, field

BASE_RATING = 1500.0
# Grid-searched on this app's own walk-forward Brier (derive_wnba_elo.py):
# K=32 was tied-best across {8,12,16,20,24,28,32,40}.
K = 32.0
# Measured, not borrowed: raw home win rate 0.543 across 1,540 non-neutral
# games -> 400*log10(p/(1-p)) = 30.1 Elo pts. Much smaller than the NBA's
# ~48 (shorter WNBA travel, single-game home stands), a real difference, not
# an oversight.
HOME_COURT_ADV = 30.0
# Same 1/3 default as NBA/NFL -- not re-grid-searched for WNBA yet (flagged,
# not silently borrowed); the validated Brier above used this value.
SEASON_REGRESSION = 1.0 / 3.0
NEUTRAL_SITE_HOME_ADV = 0.0


def effective_home_court_adv(home_team: str, location: str | None) -> float:
    if location == "Neutral":
        return NEUTRAL_SITE_HOME_ADV
    return HOME_COURT_ADV


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


def win_prob(home_rating: float, away_rating: float, home_court_adv: float = HOME_COURT_ADV) -> float:
    diff = (home_rating + home_court_adv) - away_rating
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def update_ratings(state: EloState, home: str, away: str, home_score: int, away_score: int, home_court_adv: float = HOME_COURT_ADV):
    home_r = state.get(home)
    away_r = state.get(away)
    p_home = win_prob(home_r, away_r, home_court_adv)
    actual_home = 1.0 if home_score > away_score else (0.0 if home_score < away_score else 0.5)
    delta = K * (actual_home - p_home)
    state.ratings[home] = home_r + delta
    state.ratings[away] = away_r - delta
