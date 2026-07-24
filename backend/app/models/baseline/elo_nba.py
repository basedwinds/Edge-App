"""NBA Elo rating engine -- same walk-forward pattern as elo.py (NFL), but
every constant below is either derived from this app's own cached 12-season
ESPN dataset (2014-2025, data/nba_schedule_cache.json, 14,441 REG games with
a final score, after fixing a real contamination bug -- see nba_data.py's
_parse_event docstring) or explicitly flagged as a borrowed public number.
Sport-specific by design, same reasoning as elo.py's own docstring.

CHECKED AND REJECTED, not silently skipped: an NFL-style "divisional-squeeze"
structural correction (elo.py/combine.py pulls divisional-game predictions
10% back toward 50/50). Real data across division-rival (n=2,789),
same-conference-non-division (n=6,307), and inter-conference (n=5,345) games
shows essentially NO difference in average margin (11.60 vs 11.71 vs 11.60)
or home win rate (56.8% vs 57.4% vs 56.0%) -- unlike the NFL, where division
rivals meet only twice a year and familiarity is meaningfully concentrated,
NBA teams already play everyone with real frequency (each team plays every
other team multiple times regardless of division), so there's no comparable
familiarity asymmetry to correct for. Not built.
"""
import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0
# Matches FiveThirtyEight's published NBA K-factor, but not taken on faith --
# grid-searched {10,15,18,20,22,25,30} against this app's own backtest Brier
# score (see backtest_moneyline_nba.py) and confirmed 20 is tied-optimal
# (18 and 20 both scored 0.2139, everything else worse in both directions).
K = 20.0

# Derived from real data, NOT borrowed: raw home win rate across 14,441 REG
# games (2014-2025) is 56.76%. Inverting the standard logistic
# (400*log10(p/(1-p))) gives an implied home-court Elo edge of ~47.9 points.
# Notably, FiveThirtyEight's own public NBA methodology uses a flat 100-point
# home-court constant -- a real, checked discrepancy, not an oversight. Two
# plausible reasons: (1) their constant may reflect an earlier era before the
# NBA's well-documented decline in home-court advantage (better travel,
# rest-management norms, officiating changes), and (2) it was fit jointly
# with their specific K/MOV/season-carryover choices via full model
# optimization, not back-solved from raw win rate the way this number was.
# Using OUR OWN dataset-derived value rather than the public one, since it's
# internally consistent with the exact games this app walks forward over --
# revisit if Phase 6 backtesting shows a different constant performs better.
HOME_COURT_ADV = 48.0
# Borrowed from elo.py's NFL value, but grid-searched {0, .15, .25, 1/3, .5,
# .67, 1.0} against this app's own backtest Brier score before trusting it --
# 1/3 came out tied-best (0.2147 at both 1/3 and 1/2, everything else worse).
SEASON_REGRESSION = 1.0 / 3.0

NEUTRAL_SITE_HOME_ADV = 0.0

# NOTE (2026-07-22): a box-score-derived Elo injury penalty was built and then
# REVERTED the same day -- it double-counted injuries, which are already handled
# (more richly: player-value + position + severity) by the news-adjustment
# layer, injury_rules_nba.py/situational_nba.py. The genuine salvage from that
# work is the VALIDATION DATA (scripts/build_nba_boxscore_probe.py +
# derive_nba_availability_penalty.py): a real ~50-Elo (~7pp for a top-3 player
# out) injury effect, out-of-sample -0.006 across 2024+2025 -- the "free
# historical injury-outcome dataset" injury_rules_nba.py's own docstring said
# didn't exist. Use it to CALIBRATE that layer's currently-guessed weights,
# not to add a second, competing mechanism here.

# Real, checked altitude effects (raw home-win-rate gap vs. the other 28
# teams' 56.37%, n=13,494), each converted to an Elo-point equivalent via the
# same logistic inversion then halved -- same "conservative fraction of a
# team-quality-confounded raw gap" treatment as elo.py's DENVER_ALTITUDE_
# BONUS_ELO. Denver (~5,280ft): 64.80% home win rate (n=483), ~61.5 Elo-point
# raw excess -> 30.0 conservative. Utah (~4,226ft, lower elevation than
# Denver, smaller effect is physiologically consistent): 59.04% (n=481),
# ~19.0 Elo-point raw excess -> 10.0 conservative.
DEN_ALTITUDE_BONUS_ELO = 30.0
UTAH_ALTITUDE_BONUS_ELO = 10.0

# Rest effect, revised after a deliberate "have we considered everything"
# audit pass caught that an EARLIER version of this file only checked
# back-to-backs (0 days rest, n=88, needed a 50% shrink to trust) and MISSED
# a much bigger, much better-powered effect at 1 day of rest -- confirmed via
# a clean 2x2 isolating "short rest" (<=1 day) for each side independently
# (n in the thousands per cell, not tens):
#   both sides normal rest (>=2 days, baseline):  56.90% home win, n=8,837
#   HOME short only (away normal):                 49.89% home win, n=1,385
#   AWAY short only (home normal):                 61.52% home win, n=2,552
#   both sides short:                              54.41% home win, n=1,667
# Converted to Elo-point equivalents via the same logistic inversion used
# throughout this file, then shrunk by a lighter 25% (vs. the 50% used for
# the smaller-sample altitude bonuses above) -- these samples are large
# enough, and the direction consistent enough across three independent
# cells, to warrant less caution than a 483-game single-team comparison.
# NOTE: unlike NFL's rest_rules.py (which lives in the situational/news
# layer), this is treated as a BASELINE/structural correction, same
# reasoning as elo.py's neutral-site and Denver-altitude handling -- rest is
# a certain, already-known fact by the time a game is priced, not an
# uncertain "news" signal that needs confidence-scaled blending.
HOME_SHORT_REST_PENALTY_ELO = 37.0  # 75% of the raw 49.0-point home-short-only gap
AWAY_SHORT_REST_BONUS_ELO = 25.0  # 75% of the raw 33.3-point away-short-only gap
BOTH_SHORT_REST_PENALTY_ELO = 13.0  # 75% of the raw 17.5-point both-short gap (partial cancellation, not zero)
SHORT_REST_DAYS = 1  # rest <= this many days counts as "short" for both sides


def _is_short_rest(rest: int | None) -> bool:
    return rest is not None and rest <= SHORT_REST_DAYS


def effective_home_court_adv(
    home_team: str, location: str | None, home_rest: int | None, away_rest: int | None = None
) -> float:
    if location == "Neutral":
        return NEUTRAL_SITE_HOME_ADV
    adv = HOME_COURT_ADV
    if home_team == "DEN":
        adv += DEN_ALTITUDE_BONUS_ELO
    elif home_team == "UTAH":
        adv += UTAH_ALTITUDE_BONUS_ELO

    home_short, away_short = _is_short_rest(home_rest), _is_short_rest(away_rest)
    if home_short and away_short:
        adv -= BOTH_SHORT_REST_PENALTY_ELO
    elif home_short:
        adv -= HOME_SHORT_REST_PENALTY_ELO
    elif away_short:
        adv += AWAY_SHORT_REST_BONUS_ELO
    return adv


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


def implied_elo_diff(prob: float) -> float:
    """Inverse of win_prob -- see elo.py's identical helper for how this
    feeds a future margin-space (spread/total) model from the same
    news-adjusted probability, without hand-building a second version of
    every situational factor."""
    prob = min(max(prob, 1e-6), 1 - 1e-6)
    return 400.0 * math.log10(prob / (1.0 - prob))


def mov_multiplier(point_diff: float, elo_diff_winner_perspective: float) -> float:
    """FiveThirtyEight's published NBA formula (verified live via web search
    2026-07-16, not from memory) -- borrowed as a starting point, same
    "public formula, not yet locally re-derived" status as elo.py's own NFL
    MOV multiplier. NOT yet validated against this app's own backtest;
    Phase 6 is where that validation happens."""
    return ((abs(point_diff) + 3) ** 0.8) / (7.5 + 0.006 * elo_diff_winner_perspective)


def update_ratings(
    state: EloState, home: str, away: str, home_score: int, away_score: int, home_court_adv: float = HOME_COURT_ADV
):
    home_r = state.get(home)
    away_r = state.get(away)
    p_home = win_prob(home_r, away_r, home_court_adv)

    if home_score > away_score:
        actual_home = 1.0
    elif home_score < away_score:
        actual_home = 0.0
    else:
        actual_home = 0.5  # no ties in the NBA, kept only for symmetry with elo.py

    point_diff = home_score - away_score
    elo_diff_winner_perspective = (
        (home_r + home_court_adv - away_r) if point_diff >= 0 else (away_r - home_court_adv - home_r)
    )
    mult = mov_multiplier(point_diff if point_diff != 0 else 1, elo_diff_winner_perspective)

    delta = K * mult * (actual_home - p_home)
    state.ratings[home] = home_r + delta
    state.ratings[away] = away_r - delta


def predict_and_update(state: EloState, game: dict) -> float | None:
    """Returns the PRE-game home win probability (walk-forward, no leakage),
    then updates ratings with the actual result if the game has a final
    score."""
    state.start_season_if_new(game["season"])
    home_court_adv = effective_home_court_adv(
        game["home_team"], game.get("location"), game.get("home_rest"), game.get("away_rest")
    )
    home_r = state.get(game["home_team"])
    away_r = state.get(game["away_team"])
    p_home = win_prob(home_r, away_r, home_court_adv)

    if game.get("home_score") is not None and game.get("away_score") is not None:
        update_ratings(
            state, game["home_team"], game["away_team"], game["home_score"], game["away_score"], home_court_adv
        )

    return p_home
