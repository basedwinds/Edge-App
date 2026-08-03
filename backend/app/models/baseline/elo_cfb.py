"""College Football Elo rating engine -- parallel to elo_wnba.py / elo_nba.py.

Constants DERIVED FROM THIS APP'S OWN DATA (data/cfb_game_cache.json, 4,836 FBS
games, seasons 2021-2025), not borrowed from another sport. Every value below was
grid-searched and then confirmed on HELD-OUT SEASONS -- 2024 and 2025 were each
scored while the ratings walked forward through everything before them, so no
season scores itself.

    K sweep (REG=0, HA=80), Brier / accuracy on each held-out season:

        K      2024            2025            2024+25
        48     0.1996/68.5%    0.1820/71.1%    0.1908/69.8%
        64     0.1974/69.2%    0.1790/71.5%    0.1882/70.4%
        80     0.1963/69.1%    0.1773/72.4%    0.1868/70.8%
       100     0.1959/69.5%    0.1766/73.3%    0.1863/71.4%   <- min, both years
       130     0.1968/69.8%    0.1770/74.0%    0.1870/71.9%
       160     0.1990/70.3%    0.1788/73.9%    0.1889/72.1%
       260     0.2107/69.4%    0.1891/71.8%    0.1999/70.6%

K=100 is far above the 20-32 typical of pro leagues, and that is REAL, not a
fitting artifact: it is the independent minimum on two separate held-out seasons.
The reason is structural -- ~130 FBS teams play only ~12 games each against a
huge talent range, so ratings have to move fast to separate the field before the
season ends. A pro-league K here would leave Alabama and a bottom-tier MAC team
still bunched near 1500 in November. The first grid tried topped out at K=48 and
reported it as "best"; widening the grid is what exposed the true optimum.

HOME_FIELD_ADV = 80 is a clean INTERIOR optimum (63 -> 0.1767, 80 -> 0.1766,
97 -> 0.1771, 110 -> 0.1778, 130 -> 0.1796 on 2025). Note it is deliberately NOT
the 97 implied by the raw 63.6% home win rate: some of that raw edge is schedule
(strong teams host more cupcakes), and fitting it directly would double-count.

SEASON_REGRESSION = 0.0 -- no regression to the mean between seasons, which is
the opposite of the NBA/NFL/WNBA convention. Measured on held-out 2025:
0.0 -> 0.1766, 0.25 -> 0.1778, 1/3 -> 0.1791, 0.5 -> 0.1824, 2/3 -> 0.1865.
Monotone, and interpretable: college football's blue-bloods persist year over
year far more strongly than pro parity allows, so pulling everyone back toward
1500 each August destroys real information rather than removing stale bias.

NOT VALIDATED AGAINST THE MARKET, and cannot be yet: Kalshi has ZERO settled
KXNCAAFGAME markets, so there are no historical closing prices to backtest edge
against (unlike WNBA, which had 159). The ~0.186 Brier / 71% accuracy above looks
much stronger than WNBA's 0.222 / 65.4%, but that reflects CFB's enormous talent
gaps making many games near-certain -- it is NOT evidence of an edge over a
market. Ships model_validated=False like every other model here; forward CLV
remains the only judge.

A measured temperature calibration exists for this sport in calibration_temp.py
(T=1.26, CFB is the only sport in the app needing one). Note the DIRECTION: this
model is systematically OVER-confident -- on held-out seasons the favourite wins
less often than the raw curve says, at every Elo gap. T>1 softens.

That value replaced a T=0.83 on 2026-08-03. The old one sharpened, which was
backwards: it scored worse than applying no calibration at all, and roughly
doubled the favourite-side error in the 200-300 gap band. See
scripts/cfb_calibration_audit.py, which reproduces the whole comparison.
"""
from dataclasses import dataclass, field

BASE_RATING = 1500.0

# See the K sweep in the module docstring: independent minimum on both held-out
# seasons. High by pro-league standards for a real, structural reason.
K = 100.0

# Interior optimum on held-out data, NOT the 97 implied by the raw home win rate.
HOME_FIELD_ADV = 80.0

# Zero on purpose -- measured monotone better than every positive value tried.
SEASON_REGRESSION = 0.0

NEUTRAL_SITE_HOME_ADV = 0.0


def effective_home_field_adv(neutral: bool) -> float:
    """Bowl games, kickoff classics and conference championships are played at
    neutral sites, where the home designation is a coin-flip of the bracket
    rather than a real advantage. The cache carries this per game."""
    return NEUTRAL_SITE_HOME_ADV if neutral else HOME_FIELD_ADV


@dataclass
class EloState:
    ratings: dict = field(default_factory=dict)
    current_season: int | None = None

    def get(self, team: str) -> float:
        return self.ratings.get(team, BASE_RATING)

    def start_season_if_new(self, season: int):
        # With SEASON_REGRESSION = 0.0 this is a no-op on the ratings, but the
        # season is still tracked so the constant can be revisited without
        # restructuring callers.
        if self.current_season is not None and season != self.current_season and SEASON_REGRESSION:
            for team in list(self.ratings.keys()):
                self.ratings[team] = BASE_RATING + (1 - SEASON_REGRESSION) * (self.ratings[team] - BASE_RATING)
        self.current_season = season


def win_prob(home_rating: float, away_rating: float, home_field_adv: float = HOME_FIELD_ADV) -> float:
    diff = (home_rating + home_field_adv) - away_rating
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def update_ratings(state: EloState, home: str, away: str, home_score: int, away_score: int,
                   home_field_adv: float = HOME_FIELD_ADV) -> None:
    """Plain Elo update -- no margin-of-victory multiplier, deliberately. The
    constants above were validated for THIS update rule, so adding MOV would ship
    a model that was never the one measured. CFB blowouts are routine (a 49-0 win
    over an overmatched opponent says little), which is exactly the case where an
    unvalidated MOV term would do damage."""
    rh, ra = state.get(home), state.get(away)
    p_home = win_prob(rh, ra, home_field_adv)
    actual = 1.0 if home_score > away_score else (0.5 if home_score == away_score else 0.0)
    delta = K * (actual - p_home)
    state.ratings[home] = rh + delta
    state.ratings[away] = ra - delta
