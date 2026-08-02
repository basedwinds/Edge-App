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

FITTING THIS WAS TRIED AND REJECTED (2026-08-02). A proper walk-forward fit was
built -- replaying the season and rating each game on the Elo AS IT STOOD BEFORE
it, so nothing leaks -- and the slope does NOT converge on 237 games:

    first 40% of season (n= 94)  slope 0.06814
    first 50%           (n=118)  slope 0.07776
    first 60%           (n=142)  slope 0.07874
    first 70%           (n=165)  slope 0.05503
    first 80%           (n=189)  slope 0.05048
    full season         (n=237)  slope 0.04456

Any "fitted" value is an artifact of where the data is cut. Scored out of sample
on a 60/40 time split (Brier over a grid of spread lines), the train-half fit
(0.07874) was 8.4% WORSE than the derived constant shipped below. The full-season
fit only "wins" by 1.2%, and that number is contaminated -- it was fit on the test
half too. Root cause is weak signal: corr(elo_diff, margin) is just 0.298, and
early-season ratings sit near 1500 so the elo_diff spread is small and noisy.

Conditioning sigma on Elo was tested the same way and also rejected: residual std
(13.7-14.1) is below the raw 14.27, but out-of-sample Brier moved -0.00001,
-0.00006 and +0.00009 across three splits -- noise, and inconsistent in sign.

So: DO NOT replace these with a regression fit until there are materially more
games (multiple seasons). The derived slope sits mid-range of the unstable fits
and has a reason to be the number it is, which a point estimate from 237 games
does not.
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


# --- Totals -------------------------------------------------------------------
# These were previously left unmodelled because a totals model built on the league
# average alone returns the SAME price for every game, so any "edge" would just be
# the market's line differing from 174.1 rather than a read on the matchup. That
# objection is answered by scoring_ratings_wnba, which supplies real per-team
# points scored/allowed -- and which was validated walk-forward against the naive
# league average BEFORE this was wired up (see that module for the table).
#
# Residual std of the validated model, measured on the same walk-forward run.
# Note it is BELOW NAIVE_TOTAL_STD: knowing both teams' scoring rates genuinely
# narrows the distribution, which is the point.
TOTAL_STD = 21.35


def expected_team_points(scoring: dict | None, opponent_scoring: dict | None) -> float:
    """Points this team is expected to score: the average of what it scores and
    what the opponent concedes. Same form as game_lines_nba.expected_team_points."""
    if not scoring or not opponent_scoring:
        return LEAGUE_AVG_TEAM_POINTS
    return (scoring["points_scored"] + opponent_scoring["points_allowed"]) / 2


def expected_total(home_scoring: dict | None, away_scoring: dict | None) -> tuple[float, float]:
    """(expected total points, std to use). Falls back to the league average when
    either team is below scoring_ratings_wnba.MIN_GAMES -- that fallback is not a
    failure mode but the measured-correct behaviour early in a season, when team
    scoring averages carry less signal than the league mean."""
    if not home_scoring or not away_scoring:
        return LEAGUE_AVG_TOTAL, NAIVE_TOTAL_STD
    return (
        expected_team_points(home_scoring, away_scoring)
        + expected_team_points(away_scoring, home_scoring),
        TOTAL_STD,
    )


def prob_over(line: float, home_scoring: dict | None, away_scoring: dict | None) -> float:
    """P(combined score exceeds `line`)."""
    mu, std = expected_total(home_scoring, away_scoring)
    return 1.0 - _norm_cdf(line, mu, std)
