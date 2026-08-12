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


# --- One team's score (KXWNBATEAMTOTAL) --------------------------------------
# MEASURED walk-forward over 390 team-games from 195 finished 2026 games (the
# same expanding-average, MIN_GAMES-gated setup the live path uses), 2026-08-11.
#
# TEAM_POINTS_STD IS NOT TOTAL_STD / sqrt(2). That shortcut assumes the two
# sides' scores are independent and gives 15.10; the measured value is 12.31,
# 23% narrower. The reason is in the data: residual correlation between the home
# and away scores is +0.46, because both are driven by the same game pace. Using
# the independence figure would have priced every team total far too wide.
TEAM_POINTS_STD = 12.31

# A HOME-COURT SCORING EFFECT THE GAME MODEL HAS NO TERM FOR. expected_team_points
# is symmetric -- it averages what a team scores with what its opponent concedes
# and knows nothing about venue -- but the measured residuals are NOT symmetric:
# home +3.58, away +0.56. The ~3pp asymmetry is the home effect; the shared part
# is the same upward drift the combined-total model also carries (its residual
# mean is +4.14 against a shipped std of 21.35, i.e. TOTAL is biased low too).
# That is a pre-existing issue in prob_over and is deliberately NOT changed here
# -- flagged rather than silently "fixed" under cover of a different feature.
#
# HELD OUT BEFORE SHIPPING, not just fitted. Offsets fitted on the first 60% of
# team-games (+2.69 / -1.14) and scored on the remaining 156, at each team's own
# expected line (the hardest, 50/50 case):
#     no offsets, TOTAL_STD/sqrt2   Brier 0.25371
#     no offsets, measured std      Brier 0.25437
#     WITH offsets, measured std    Brier 0.24597   <- shipped
# The width alone changes almost nothing at an at-the-money line, as expected;
# the offsets are what carry the gain. Constants below are refitted on the full
# sample, which is why they differ from the train-only pair above.
TEAM_POINTS_HOME_OFFSET = 3.58
TEAM_POINTS_AWAY_OFFSET = 0.56


def expected_team_points_venue(scoring: dict | None, opponent_scoring: dict | None,
                               team_is_home: bool) -> float:
    """expected_team_points plus the measured venue offset."""
    base = expected_team_points(scoring, opponent_scoring)
    return base + (TEAM_POINTS_HOME_OFFSET if team_is_home else TEAM_POINTS_AWAY_OFFSET)


def prob_team_over(line: float, scoring: dict | None, opponent_scoring: dict | None,
                   team_is_home: bool) -> float:
    """P(this one team's score exceeds `line`).

    Falls back to the league average through expected_team_points when either
    side is below scoring_ratings_wnba.MIN_GAMES -- same measured-correct
    early-season behaviour as expected_total, and the caller still gets a real
    number rather than None.
    """
    mu = expected_team_points_venue(scoring, opponent_scoring, team_is_home)
    return 1.0 - _norm_cdf(line, mu, TEAM_POINTS_STD)


# --- Halves ------------------------------------------------------------------
# Kalshi runs six live WNBA half series (1H/2H winner, spread and total) with
# real settled history -- 528/528/176/282/698/658 settled markets as of
# 2026-08-02, so this is proven inventory, not a speculative build.
#
# Constants MEASURED from ESPN quarter linescores over 227 finished 2026 games
# (1H = Q1+Q2, 2H = Q3+Q4):
#
#            margin mean   margin std   total mean   share of game
#     1H         +1.80        10.39        86.16        0.4931
#     2H         -0.11         9.48        87.43        0.5004
#     full       +1.76        14.21       174.71        1.0
#
# Two things that matter came out of that table:
#
# 1. HOME ADVANTAGE IS ALMOST ENTIRELY A FIRST-HALF EFFECT. 1H home margin is
#    +1.80 while 2H is -0.11 -- i.e. the whole +1.76 full-game edge accrues
#    before the break and the second half is essentially neutral. So the second
#    half must NOT carry the same home-court term; applying the full-game Elo
#    edge to it would bias every 2H line toward the home side.
# 2. The two shares sum to 0.9935, not 1.0. The gap is overtime, which belongs
#    to neither half -- so a 2H line must be modelled on regulation scoring, and
#    the shares are deliberately left un-normalised rather than forced to 1.
#
# The measurement also independently reproduced the shipped full-game constants
# (total 174.71 vs 174.1, margin std 14.21 vs 14.27), which is a useful check
# that the linescore source agrees with the scoreboard source already in use.
HALF_MARGIN_STD = {1: 10.39, 2: 9.48}
HALF_TOTAL_SHARE = {1: 0.4931, 2: 0.5004}
HALF_TOTAL_STD = {1: 13.43, 2: 13.41}

# Derived the same way MARGIN_SLOPE is (see the module docstring), from each
# half's own margin spread. NOT fitted -- the same caution applies, and forward
# CLV is the judge.
HALF_MARGIN_SLOPE = {
    1: math.log(10) / 1600.0 * HALF_MARGIN_STD[1] * math.sqrt(2 * math.pi),  # 0.0375
    2: math.log(10) / 1600.0 * HALF_MARGIN_STD[2] * math.sqrt(2 * math.pi),  # 0.0342
}

# Fraction of the Elo-implied edge that shows up in each half. The measurement
# above says the first half carries it all, so the second half gets none rather
# than a scaled-down share -- an invented middle value would be worse than the
# number actually measured.
HALF_EDGE_SHARE = {1: 1.0, 2: 0.0}


def prob_team_covers_half(team_is_home: bool, line: float, elo_diff: float, half: int) -> float:
    """P(team wins `half` by more than `line`). `elo_diff` is home-minus-away and
    already includes home-court (see the router), so HALF_EDGE_SHARE is what
    stops that edge being applied to a second half that does not exhibit it."""
    mu = HALF_MARGIN_SLOPE[half] * elo_diff * HALF_EDGE_SHARE[half]
    if not team_is_home:
        mu = -mu
    return 1.0 - _norm_cdf(line, mu, HALF_MARGIN_STD[half])


def prob_over_half(line: float, home_scoring: dict | None, away_scoring: dict | None, half: int) -> float:
    """P(combined score in `half` exceeds `line`)."""
    full_mu, _ = expected_total(home_scoring, away_scoring)
    mu = full_mu * HALF_TOTAL_SHARE[half]
    return 1.0 - _norm_cdf(line, mu, HALF_TOTAL_STD[half])


def prob_team_wins_half(team_is_home: bool, elo_diff: float, half: int) -> float:
    """P(team outscores the opponent in `half`) -- the half-winner markets. Just
    the spread model at a line of 0."""
    return prob_team_covers_half(team_is_home, 0.0, elo_diff, half)
