"""College-football margin model -- the spread side of KXNCAAFSPREAD.

SEPARATE FROM game_lines.py ON PURPOSE. Reusing NFL's constants here would have
been badly wrong, and the size of the error is the reason this module exists:

                     NFL          CFB (fitted)
    MARGIN_SLOPE     0.04146      0.13636      3.3x steeper
    MARGIN_STD       13.52        19.82        47% wider

College football has a far wider talent spread than the NFL -- mismatches are
routine and blowouts are ordinary -- so a given Elo gap converts to many more
points, and the outcome scatters much further around it. Pricing CFB with NFL's
numbers would have called every game far more certain than it is.

HOW THE NUMBERS WERE FITTED (scripts/calibrate_cfb_lines.py, kept):
data/cfb_game_cache.json, 4,836 games over 2021-2025, all with final scores,
replayed chronologically with the same Elo update elo_service_cfb uses so every
game is predicted from PRE-GAME ratings (no leakage from its own result), then
actual margin regressed on the pre-game Elo difference.

OUT-OF-SAMPLE, holding each season out and fitting on the other four:

    2021  n=976  slope(train)=0.13311  held-out sd=20.75  mean resid=-0.61
    2022  n=971  slope(train)=0.13773  held-out sd=19.90  mean resid=-1.04
    2023  n=966  slope(train)=0.13715  held-out sd=19.34  mean resid=-0.58
    2024  n=965  slope(train)=0.13952  held-out sd=19.77  mean resid=-0.51
    2025  n=958  slope(train)=0.13468  held-out sd=19.32  mean resid=+0.63

The slope moves only between 0.133 and 0.140 across folds and the held-out
residual is centred within about a point, so this is a stable fit rather than
one season's quirk.

HOME-FIELD ADVANTAGE was swept rather than assumed: 55 Elo points leaves the
least-biased residual (-0.42 points, against +6.54 at zero and -1.88 at 100).

NO TOTALS MODEL HERE, and that is a measured decision, not an omission. A
per-team offence/defence scoring model was built and walk-forward tested on the
same 4,836 games (each game predicted only from that season's earlier games):
mean absolute error 13.01 points against 13.66 for simply using the running
league average -- a 4.8% improvement. That is far too thin to stake against a
market that prices totals sharply, so KXNCAAFTOTAL and KXNCAAFTEAMTOTAL stay
unpriced rather than carrying a number this weak. League mean total is 54.16
(sd 16.89) if a future model wants the baseline.

model_validated: false, like every other model here -- fitting a slope out of
sample says the margin relationship is real and stable, NOT that it beats the
closing line.
"""
import math

# Points of margin per Elo point of pre-game difference. See module docstring.
MARGIN_SLOPE = 0.13636
# Standard deviation of actual margin around that prediction.
MARGIN_STD = 19.82
# Elo points of home-field advantage, swept for the least-biased residual.
HOME_FIELD_ADV = 55.0


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def expected_margin(elo_diff: float) -> float:
    """Expected home margin. `elo_diff` is home-perspective and must ALREADY
    include home-field advantage -- (home_rating + HOME_FIELD_ADV) - away_rating
    for a normal game, or the bare rating difference at a neutral site. Same
    convention as game_lines.expected_margin, so the two cannot be confused by
    a caller moving between sports."""
    return MARGIN_SLOPE * elo_diff


def prob_team_covers(team_is_home: bool, line: float, elo_diff: float) -> float:
    """P(that team wins by MORE than `line` points).

    Deliberately identical in shape and sign convention to
    game_lines.prob_team_covers: a positive line always means "wins by more
    than this". Tennis's two spread graders disagreeing about what a negative
    line meant is exactly the kind of divergence worth not repeating.
    """
    home_margin_mu = expected_margin(elo_diff)
    team_margin_mu = home_margin_mu if team_is_home else -home_margin_mu
    return 1.0 - _norm_cdf(line, team_margin_mu, MARGIN_STD)


def elo_diff_for(home_rating: float, away_rating: float, neutral: bool = False) -> float:
    """Home-perspective Elo difference with home-field advantage applied, so a
    caller cannot forget it and silently price every game as neutral."""
    return (home_rating + (0.0 if neutral else HOME_FIELD_ADV)) - away_rating
