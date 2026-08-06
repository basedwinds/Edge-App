"""Spread/total probability model, layered on top of the existing Elo
baseline and scoring_ratings.py -- same "free reference estimate, not
validated to beat the market" status as everything else (Elo itself
doesn't beat the market either, see elo_service.py).

Both spread and total are modeled as a Normal distribution around a point
estimate, using a standard deviation derived from real historical data
rather than guessed (2026-07-15, n=6,967 REG games 2012-2025, same
methodology as this project's other derived-not-guessed constants, e.g.
epa_mismatch_rules.py's EPA_DIFF_SCALE_PP):

- MARGIN: linear-regressed actual home margin against Elo's own home-
  perspective rating diff (which already includes home-field-advantage):
  margin ~= MARGIN_SLOPE * elo_diff, residual std MARGIN_STD = 13.52. The
  fitted intercept was ~0 (as expected -- elo_diff is already home-
  field-adjusted) so it's dropped.
- TOTAL: a team-scoring blend (see scoring_ratings.py) reduces residual std
  from 14.14 (naive league-mean) to TOTAL_STD = 13.85 -- a real, if modest,
  improvement. Falls back to the league-mean total (LEAGUE_AVG_TOTAL) with
  the wider naive std when a team has insufficient rolling scoring data
  (early season).
- TEAM TOTAL (one team's own points, not the combined game total): same
  blend applied to a single side, residual std 9.81 vs. 10.22 naive
  (n=13,812 team-games, 2026-07-16), correlation 0.29 -- same "real but
  modest" pattern as the combined total.

Two structural (non-weather) shifts for the total model, both checked
against real historical data before adding (2026-07-16, n=6,967 REG games,
1999-2025): divisional games average 1.5pts LOWER total (43.25 vs 44.74,
"ugly divisional games" isn't just folklore), dome/closed-roof games
average 3.5pts HIGHER total (46.78 vs 43.30 outdoor) -- neither comparison
is team-quality- or era-adjusted (average season of each group differs by
only ~1-1.5 years, so era inflation isn't the main driver, but some
confounding is still possible). The divisional number is used close to raw
since the effect is modest; the larger, more surprising dome number gets
the same "conservative fraction of the raw gap" treatment as
elo.py::DENVER_ALTITUDE_BONUS_ELO. A third hypothesis -- Denver's altitude
boosting scoring via longer field-goal range -- was checked and REJECTED:
DEN home games average 44.21 total vs. 44.16 for everyone else, essentially
no effect, so no altitude-total adjustment was added despite it being a
plausible-sounding idea.

HALF-GAME MODEL (1st/2nd half spread+total, 2026-07-16): nflverse's own PBP
`game_half` label ("Half1"/"Half2") gives an exact halftime score split, no
guessing needed. Regressed half-margin against the SAME Elo diff quantity as
the full-game model (n=3,663 games with PBP+Elo coverage, 2012-2025):
H1_MARGIN_SLOPE=0.02132 (std 10.54), H2_MARGIN_SLOPE=0.01905 (std 9.39) --
both roughly HALF the full-game MARGIN_SLOPE, exactly as expected (half a
game gives the better team roughly half as much time to assert its edge).
For half-totals, rather than a separate team-scoring-blend regression, each
half is modeled as a fixed SHARE of the already-computed full-game expected
total (H1 gets 50.2%, H2 gets 49.8% -- essentially an even split, confirmed
by real per-half totals summing to the full game: n=3,829 games, H1 mean
22.97/std 9.01, H2 mean 22.76/std 9.97, full mean 45.73/std 13.92) with each
half's own empirical std, not a scaled-down version of the full-game std.

EPA MISMATCH INTENSITY, added 2026-07-16 as a totals-specific extension of
the already-validated moneyline EPA-mismatch signal (epa_mismatch_rules.py):
that module deliberately only uses HOME offense vs AWAY defense (the mirror
direction showed no reliable moneyline signal in the Phase 2 backtest). For
TOTALS specifically, checked fresh rather than assumed the same asymmetry
holds: a two-sided "intensity" (home_off_epa - away_def_epa) + (away_off_epa
- home_def_epa) -- i.e. both teams' favorable-matchup signal combined, since
a genuine shootout needs both offenses beating their assigned defense, not
just one -- correlates with actual total points at r=0.104 (n=3,389 REG
games 2012-2025, real if modest, same "real but priced in" tier as the
moneyline version). Raw fitted slope is 7.37 points per unit of intensity;
EPA_MISMATCH_TOTAL_SCALE = 3.5 uses roughly half of that (same "conservative
fraction of the fitted slope" discipline as this file's other derived
constants), capped at EPA_MISMATCH_TOTAL_MAX_PTS = 3.0.

TURF (grass vs. artificial), checked 2026-07-16 as part of a round of
candidate-signal checks (team-specific HFA, tanking, short-week/TNF effect,
Mexico City altitude were also checked and REJECTED -- see this project's
memory for the numbers): a raw grass-vs-artificial total-points gap (n=6,923
REG games, 2012-2025) is 2.0pts (46.74 turf vs. 44.71 grass), but most of
that is DOME confounding (domes are almost always turf, and dome's own
+3.5pt effect is already captured by DOME_TOTAL_BOOST_PTS above). Controlling
for roof type (outdoor-only games, n=2,550) leaves a real, independent
residual: 45.38 (artificial) vs. 44.46 (grass), a 0.92pt gap -- same
"conservative fraction of the raw residual" treatment as this file's other
structural constants, TURF_TOTAL_BOOST_PTS = 0.5.

No scipy dependency -- the Normal CDF is a two-line closed form via
math.erf.
"""
import math

MARGIN_SLOPE = 0.04146
MARGIN_STD = 13.52

LEAGUE_AVG_TOTAL = 44.16
NAIVE_TOTAL_STD = 14.14
TOTAL_STD = 13.85

LEAGUE_AVG_TEAM_POINTS = LEAGUE_AVG_TOTAL / 2
TEAM_NAIVE_STD = 10.22
TEAM_TOTAL_STD = 9.81

DIVISIONAL_TOTAL_SUPPRESSION_PTS = 1.5
DOME_TOTAL_BOOST_PTS = 1.75  # conservative ~half of the raw 3.49pt gap
TURF_TOTAL_BOOST_PTS = 0.5  # conservative ~half of the roof-controlled 0.92pt residual gap
EPA_MISMATCH_TOTAL_SCALE = 3.5  # ~half the fitted 7.37 pts/unit slope, see module docstring
EPA_MISMATCH_TOTAL_MAX_PTS = 3.0

HALF_MARGIN_SLOPE = {1: 0.02132, 2: 0.01905}
HALF_MARGIN_STD = {1: 10.54, 2: 9.39}
# P(a half ends LEVEL). Measured 2026-08-06 over 856 completed NFL games
# (2023-2025 regular season + playoffs, ESPN per-quarter linescores):
#
#            home    away     TIE
#   1st half 51.4%   42.3%    6.3%
#   2nd half 48.1%   42.1%    9.8%
#
# This is not a rounding detail, it is the whole reason prob_team_wins_half
# exists as its own function. The margin model is CONTINUOUS, so P(margin > 0)
# and P(margin < 0) sum to 1 and the tie mass gets silently split between the
# two teams -- overstating EACH side by ~3.2pp in the 1st half and ~4.9pp in
# the 2nd. Against a 10pp recommend gate that is a systematic manufactured
# edge on every half-winner row, on both sides of the same game at once.
#
# NFL ties much more than basketball does (scores move in 3s and 7s, and a 2nd
# half is often 0-0 or 7-7), which is why WNBA's equivalent could get away with
# treating a half winner as a spread at line 0 and this cannot.
#
# The same run reproduced the existing constants from independent data --
# measured 1H margin sd 10.56 vs the 10.54 coded above, 2H 9.55 vs 9.39 -- so
# these halves are being modelled on the right distribution.
HALF_TIE_RATE = {1: 0.063, 2: 0.098}
HALF_TOTAL_SHARE = {1: 0.502, 2: 0.498}
HALF_TOTAL_STD = {1: 9.01, 2: 9.97}


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def expected_margin(elo_diff: float) -> float:
    """elo_diff is the home-perspective rating difference INCLUDING
    home-field-advantage (i.e. (home_rating + home_field_adv) - away_rating,
    the same quantity elo.win_prob's `diff` computes internally)."""
    return MARGIN_SLOPE * elo_diff


def prob_team_covers(team_is_home: bool, line: float, elo_diff: float) -> float:
    """P(that team's margin of victory > line). `line` is the threshold
    from the market (e.g. Kalshi's floor_strike, or a traditional spread's
    magnitude for the favorite) -- always evaluated as "wins by more than
    line points", matching how both platforms actually phrase these
    markets (see kalshi_client.py/polymarket_client.py)."""
    home_margin_mu = expected_margin(elo_diff)
    team_margin_mu = home_margin_mu if team_is_home else -home_margin_mu
    return 1.0 - _norm_cdf(line, team_margin_mu, MARGIN_STD)


def expected_team_points(scoring: dict | None, opponent_scoring: dict | None) -> float:
    """One team's own expected points in this specific matchup -- blends
    this team's trailing scoring tendency with the opponent's trailing
    tendency to allow points. Falls back to half the league-average total
    when either side lacks enough rolling data yet (see
    scoring_ratings.py's MIN_GAMES_FOR_RATING)."""
    if not scoring or not opponent_scoring:
        return LEAGUE_AVG_TEAM_POINTS
    return (scoring["points_scored"] + opponent_scoring["points_allowed"]) / 2


def expected_total(home_scoring: dict | None, away_scoring: dict | None) -> tuple[float, float]:
    """Returns (expected_total, std_to_use)."""
    if not home_scoring or not away_scoring:
        return LEAGUE_AVG_TOTAL, NAIVE_TOTAL_STD
    return expected_team_points(home_scoring, away_scoring) + expected_team_points(away_scoring, home_scoring), TOTAL_STD


def structural_total_shift(is_divisional: bool, is_dome: bool, is_turf: bool = False) -> float:
    """Points to ADD to points_shift (i.e. positive = lower expected total)
    for prob_over/prob_team_over below, from static per-game facts that
    aren't weather (see module docstring for the real-data check behind
    all three numbers). is_turf is independent of is_dome (checked
    roof-controlled, not just re-detecting the dome effect) -- both can
    apply to the same game (nearly all domes ARE turf) without double
    counting, since the dome number was itself derived net of turf's own
    contribution."""
    shift = 0.0
    if is_divisional:
        shift += DIVISIONAL_TOTAL_SUPPRESSION_PTS
    if is_dome:
        shift -= DOME_TOTAL_BOOST_PTS
    if is_turf:
        shift -= TURF_TOTAL_BOOST_PTS
    return shift


def epa_mismatch_total_shift(
    home_off_epa: float | None, home_def_epa_allowed: float | None,
    away_off_epa: float | None, away_def_epa_allowed: float | None,
) -> float:
    """Points to ADD to points_shift (positive = lower expected total),
    from the two-sided EPA-mismatch intensity described in the module
    docstring. Unlike epa_mismatch_rules.py's moneyline signal (deliberately
    ONE-sided, home-off-vs-away-def only), this uses BOTH directions summed
    -- a real shootout needs both offenses beating their assigned defense,
    not just the home side. Returns 0.0 if either team's EPA rating is
    unavailable (insufficient rolling data), same graceful-degrade
    convention as every other structural shift in this file."""
    if None in (home_off_epa, home_def_epa_allowed, away_off_epa, away_def_epa_allowed):
        return 0.0
    intensity = (home_off_epa - away_def_epa_allowed) + (away_off_epa - home_def_epa_allowed)
    shift = -intensity * EPA_MISMATCH_TOTAL_SCALE
    return max(-EPA_MISMATCH_TOTAL_MAX_PTS, min(EPA_MISMATCH_TOTAL_MAX_PTS, shift))


def prob_over(
    line: float, home_scoring: dict | None, away_scoring: dict | None, points_shift: float = 0.0
) -> float:
    """P(total points > line). `points_shift` is SUBTRACTED from the
    expected total before computing probability -- a positive shift means a
    lower expected total (e.g. weather suppression, see
    weather_rules.py::compute_total_points_adjustment, or
    structural_total_shift above)."""
    mu, sigma = expected_total(home_scoring, away_scoring)
    return 1.0 - _norm_cdf(line, mu - points_shift, sigma)


def prob_team_over(
    line: float, scoring: dict | None, opponent_scoring: dict | None, points_shift: float = 0.0
) -> float:
    """P(this team's own points > line) -- for a team-total-points market
    (see kalshi_client.py's KXNFLTEAMTOTAL), not the combined game total."""
    mu = expected_team_points(scoring, opponent_scoring)
    sigma = TEAM_TOTAL_STD if (scoring and opponent_scoring) else TEAM_NAIVE_STD
    return 1.0 - _norm_cdf(line, mu - points_shift, sigma)


def prob_team_covers_half(team_is_home: bool, line: float, elo_diff: float, half: int) -> float:
    """Half-specific version of prob_team_covers -- half in {1, 2}."""
    home_margin_mu = HALF_MARGIN_SLOPE[half] * elo_diff
    team_margin_mu = home_margin_mu if team_is_home else -home_margin_mu
    return 1.0 - _norm_cdf(line, team_margin_mu, HALF_MARGIN_STD[half])


def prob_team_wins_half(team_is_home: bool, elo_diff: float, half: int) -> float:
    """P(team OUTSCORES the opponent in `half`) -- the 3-way half-winner
    markets (KXNFL1H / KXNFL2H), which carry a TIE leg of their own.

    NOT the spread model at a line of 0. That is how WNBA can do it, but its
    halves almost never end level; NFL's do, at a measured 6.3% / 9.8% (see
    HALF_TIE_RATE). The continuous margin model has no point mass at exactly
    zero, so P(margin > 0) quietly absorbs half the tie mass into each team and
    would overstate BOTH sides of the same game.

    The two team legs keep their relative odds and are scaled to leave room for
    the tie, so home + away + tie sums to 1. That is a deliberate first cut: the
    real tie rate surely varies with the spread (level matchups draw more
    often), but nothing here measures that, so it is applied flat and the TIE
    leg itself is left unpriced rather than sold at a league-average number.
    """
    p_no_tie = 1.0 - HALF_TIE_RATE[half]
    return prob_team_covers_half(team_is_home, 0.0, elo_diff, half) * p_no_tie


def prob_over_half(
    line: float,
    home_scoring: dict | None,
    away_scoring: dict | None,
    half: int,
    points_shift: float = 0.0,
) -> float:
    """Half-specific version of prob_over -- half in {1, 2}. `points_shift`
    is a FULL-GAME-scale shift (e.g. weather suppression); it's scaled down
    by the same HALF_TOTAL_SHARE used for the mean, so a 4pt full-game
    weather suppression contributes ~2pt to each half rather than 4pt to
    both (which would double-count when the two halves are considered
    together)."""
    full_mu, _ = expected_total(home_scoring, away_scoring)
    share = HALF_TOTAL_SHARE[half]
    mu = full_mu * share
    sigma = HALF_TOTAL_STD[half]
    return 1.0 - _norm_cdf(line, mu - points_shift * share, sigma)
