"""WNBA spread pricing -- same shape as game_lines_nba, but with constants
MEASURED FROM REAL WNBA GAMES rather than borrowed from the NBA module.

Why a separate module (checked 2026-08-02 against 237 finished WNBA games in our
own WnbaGame table, the 2026 season):

    metric              WNBA (measured)   NBA (game_lines_nba)
    avg total points    174.1             218.30
    margin stdev        14.27             13.20

Reusing NBA's numbers would price every WNBA total as if ~44 more points were
scored, manufacturing huge fake edges on a market this app already knows it has
no proven edge in. So the spread model below uses the real WNBA margin spread,
and TOTALS ARE DELIBERATELY NOT MODELLED HERE -- see the note at the bottom.

MARGIN_SLOPE is derived, not fitted: fitting margin-on-Elo needs each game's
Elo diff AS IT STOOD BEFORE that game (walk-forward), and regressing on today's
ratings would leak the season's results into the fit. Instead the slope is set so
the margin model's implied win probability has the same slope at elo_diff=0 as
the Elo curve itself:

    P(win) = Phi(slope * elo_diff / MARGIN_STD)  ->  d/dx at 0 = slope / (STD*sqrt(2pi))
    Elo:     P(win) = 1/(1+10^(-elo_diff/400))   ->  d/dx at 0 = ln(10)/1600
    =>  slope = ln(10)/1600 * MARGIN_STD * sqrt(2*pi)

That gives 0.0515 for WNBA. Sanity check: the same derivation on NBA's own
MARGIN_STD yields 0.0476 against their empirically fitted 0.04224 -- same
ballpark, slightly steep, so treat this as a reasonable prior rather than a
calibrated number. model_validated stays False; forward CLV is the judge.
"""
import math

# Measured over 237 finished 2026 WNBA games (home_score - away_score).
MARGIN_STD = 14.27
# Home margin averaged +1.76 in the same sample; that home edge already lives in
# the Elo (elo_wnba.HOME_COURT_ADV), so it is NOT re-added here -- doing so would
# double-count home advantage.
MARGIN_SLOPE = math.log(10) / 1600.0 * MARGIN_STD * math.sqrt(2 * math.pi)

# Recorded for whoever builds the totals model (see bottom note).
LEAGUE_AVG_TOTAL = 174.1
LEAGUE_AVG_TEAM_POINTS = LEAGUE_AVG_TOTAL / 2
NAIVE_TOTAL_STD = 22.11


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def expected_margin(elo_diff: float) -> float:
    """Expected HOME margin (points) for a given home-minus-away Elo diff."""
    return MARGIN_SLOPE * elo_diff


def prob_team_covers(team_is_home: bool, line: float, elo_diff: float) -> float:
    """P(team wins by MORE than `line`) -- same convention as every other spread
    model in this app (see game_lines.py::prob_team_covers): a positive line is
    the favourite needing to win by more than it, a negative line is the underdog
    needing not to lose by more than |line|."""
    mu = expected_margin(elo_diff)
    if not team_is_home:
        mu = -mu
    return 1.0 - _norm_cdf(line, mu, MARGIN_STD)


# TOTALS ARE NOT MODELLED HERE, on purpose. NBA's prob_over needs per-team
# offensive/defensive scoring ratings (scoring_ratings_service_nba); WNBA has no
# equivalent service, and a totals model using only the league average would
# return the SAME price for every game -- any "edge" it produced would just be
# the market's line differing from 174.1, which is not a real read on the game.
# Building it means a WNBA scoring-ratings service first (per-team points
# for/against off the same WnbaGame scores used above).
