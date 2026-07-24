"""NBA spread/total probability model -- parallel to game_lines.py (NFL),
same "Normal distribution around a derived point estimate" approach. Every
constant below is derived from this app's own cached dataset (data/
nba_schedule_cache.json), not guessed or borrowed.

Refreshed 2026-07-16 (same day, later round) after catching a real gap: the
cache/DB were missing the entire 2025-26 season (build_nba_schedule_cache.py
had a hardcoded END date) -- Elo/scoring ratings were a full season stale.
After the fix (15,672 REG games, 2014-2026), MARGIN_SLOPE/STD and
LEAGUE_AVG_TOTAL/NAIVE_TOTAL_STD moved slightly (0.04171->0.04224,
13.08->13.20, 217.20->218.30, 22.29->22.43) -- normal noise from one more
season, not a correction of anything wrong, but updated here for accuracy.
Home-court/altitude/rest constants (elo_nba.py) and the scoring-blend TOTAL_STD
were re-checked too and shifted even less (e.g. home win rate 56.76%->56.66%),
not worth republishing.

MARGIN: linear-regressed actual home margin against elo_service_nba's own
home-perspective elo_diff (walk-forward, includes home-court-adv/altitude/
rest already) -- margin ~= MARGIN_SLOPE * elo_diff, residual std MARGIN_STD.
Fitted through the origin (no intercept) since elo_diff is already
home-court-adjusted, same convention as NFL's version. Correlation
elo_diff-vs-margin: r=0.408 (notably stronger than what a first look at
NFL's own game_lines.py implies -- NBA's Elo has real explanatory power
here).

TOTAL: a team-scoring blend (scoring_ratings_nba.py) cuts residual std from
22.29 (naive league-mean) to TOTAL_STD=18.64 -- a much bigger improvement
than NFL's modest 14.14->13.85, see scoring_ratings_nba.py's docstring for
why. Falls back to the naive league mean/std when either team lacks
sufficient rolling data (early season).

NOT built, checked and rejected: a divisional-game total/margin suppression
(NFL's real finding) does NOT hold for NBA -- divisional games average
216.32 total points vs. 217.40 non-divisional (n=2,789 vs 11,652), a ~1pt
gap that's noise on a ~217pt base. Consistent with the earlier baseline
audit's divisional-squeeze rejection (see elo_nba.py's own docstring) --
NBA teams already play everyone with real frequency, so there's no
familiarity asymmetry concentrated in division games the way there is in
the NFL's twice-a-year-only divisional slate.

NOT applicable at all (not "not built" -- structurally doesn't exist for
this sport): NFL's dome/turf boost has no NBA equivalent -- every NBA arena
is an indoor, climate-controlled court, so there's no roof-type/surface
variable to model in the first place.
"""
import math

MARGIN_SLOPE = 0.04224
MARGIN_STD = 13.20

LEAGUE_AVG_TOTAL = 218.30
NAIVE_TOTAL_STD = 22.43
TOTAL_STD = 18.64

LEAGUE_AVG_TEAM_POINTS = LEAGUE_AVG_TOTAL / 2
TEAM_NAIVE_STD = 13.30
TEAM_TOTAL_STD = 11.51

# HALF-LINE constants (2026-07-17), derived from a SAMPLE of real quarter-by-
# quarter scores, not the full dataset -- unlike NFL (nflverse's cached PBP
# has an exact game_half column, zero extra cost), ESPN's per-game summary
# endpoint is the only free source of NBA quarter scores and costs one
# network call PER GAME (~0.5s, confirmed live) -- pulling all 15,000+
# cached games would take 2+ hours. Sampled 600 games instead (200 each from
# 3 recent seasons, evenly spaced to avoid early/late-season bias) via
# scripts/build_nba_halfline_sample.py -- smaller than NFL's 3,663-game
# derivation, documented as such rather than presented as equally robust.
# H1/H2 slopes both come out to almost exactly HALF of the full-game
# MARGIN_SLOPE (0.02485, 0.02446 vs. 0.04224) -- expected, a half-game gives
# the better team about half as much time to assert its edge, same pattern
# NFL found. Split is almost perfectly even (49.92%/50.08%) -- two equal
# halves, as expected structurally (no reason to expect otherwise, unlike
# NFL's stoppage-heavy 2-minute-drill dynamics). H2's total std (14.44) is
# meaningfully higher than H1's (12.55), plausibly from occasional overtime
# periods folded into "2nd half" plus endgame pace changes (garbage time,
# intentional fouling) -- not investigated further, just reflected honestly
# in the wider H2 sigma.
HALF_MARGIN_SLOPE = {1: 0.02485, 2: 0.02446}
HALF_MARGIN_STD = {1: 10.93, 2: 11.17}
HALF_TOTAL_SHARE = {1: 0.4992, 2: 0.5008}
HALF_TOTAL_STD = {1: 12.55, 2: 14.44}


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def expected_margin(elo_diff: float) -> float:
    """elo_diff is the home-perspective rating difference INCLUDING
    home-court-adv/altitude/rest (i.e. elo_service_nba.get_elo_diff's
    return value)."""
    return MARGIN_SLOPE * elo_diff


def prob_team_covers(team_is_home: bool, line: float, elo_diff: float) -> float:
    """P(that team's margin of victory > line) -- "wins by more than line
    points", matching how both Kalshi and Polymarket phrase these markets."""
    home_margin_mu = expected_margin(elo_diff)
    team_margin_mu = home_margin_mu if team_is_home else -home_margin_mu
    return 1.0 - _norm_cdf(line, team_margin_mu, MARGIN_STD)


def expected_team_points(scoring: dict | None, opponent_scoring: dict | None) -> float:
    if not scoring or not opponent_scoring:
        return LEAGUE_AVG_TEAM_POINTS
    return (scoring["points_scored"] + opponent_scoring["points_allowed"]) / 2


def expected_total(home_scoring: dict | None, away_scoring: dict | None) -> tuple[float, float]:
    """Returns (expected_total, std_to_use)."""
    if not home_scoring or not away_scoring:
        return LEAGUE_AVG_TOTAL, NAIVE_TOTAL_STD
    return expected_team_points(home_scoring, away_scoring) + expected_team_points(away_scoring, home_scoring), TOTAL_STD


def prob_over(line: float, home_scoring: dict | None, away_scoring: dict | None) -> float:
    total_mu, std = expected_total(home_scoring, away_scoring)
    return 1.0 - _norm_cdf(line, total_mu, std)


def prob_team_over(line: float, scoring: dict | None, opponent_scoring: dict | None) -> float:
    if not scoring or not opponent_scoring:
        return 1.0 - _norm_cdf(line, LEAGUE_AVG_TEAM_POINTS, TEAM_NAIVE_STD)
    mu = expected_team_points(scoring, opponent_scoring)
    return 1.0 - _norm_cdf(line, mu, TEAM_TOTAL_STD)


def prob_team_covers_half(team_is_home: bool, line: float, elo_diff: float, half: int) -> float:
    """Half-specific version of prob_team_covers -- half in {1, 2}."""
    home_margin_mu = HALF_MARGIN_SLOPE[half] * elo_diff
    team_margin_mu = home_margin_mu if team_is_home else -home_margin_mu
    return 1.0 - _norm_cdf(line, team_margin_mu, HALF_MARGIN_STD[half])


def prob_over_half(line: float, home_scoring: dict | None, away_scoring: dict | None, half: int) -> float:
    """Half-specific version of prob_over -- half in {1, 2}."""
    full_mu, _ = expected_total(home_scoring, away_scoring)
    mu = full_mu * HALF_TOTAL_SHARE[half]
    return 1.0 - _norm_cdf(line, mu, HALF_TOTAL_STD[half])
