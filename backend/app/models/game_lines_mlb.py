"""MLB run-line/totals probability model -- parallel to game_lines.py (NFL)/
game_lines_nba.py, same "Normal distribution around a derived point
estimate" approach. Every constant below is derived from this app's own
cached 10-season dataset (data/mlb_schedule_cache.json + data/
mlb_pitcher_snapshot_cache.json, 22,764 REG games with a final score,
2016-2025), not guessed or borrowed.

MARGIN: linear-regressed actual run margin against elo_service_mlb's own
home-perspective elo_diff (walk-forward, INCLUDES home-field-adv AND the
starting-pitcher blend already -- see elo_service_mlb.py::get_elo_diff) --
margin ~= MARGIN_SLOPE * elo_diff, residual std MARGIN_STD. Fitted through
the origin (no intercept), same convention as NFL/NBA. Correlation
elo_diff-vs-margin: r=0.186 -- real, but the weakest of the three sports in
this app (NFL's is stronger, NBA's r=0.408 much stronger), consistent with
baseball's well-documented game-to-game unpredictability (bullpen variance,
balls-in-play luck) even with a real, validated pregame signal.

TOTAL: team-BEHAVIOR signals checked and REJECTED (not the same as "no
signal exists at all" -- see PARK_FACTOR below for the one that IS real).
Two candidates tested against real data before committing to either:
  1. A trailing team-scoring blend (mirroring scoring_ratings.py/
     scoring_ratings_nba.py exactly, rolling windows {5,8,10,15,20,25,30,40,
     50} games): at EVERY window size, the blend's residual std was WORSE
     than the naive league-mean-only baseline (e.g. window=15: blend 4.578
     vs naive 4.532; even window=50, the best of the range tested, barely
     ties naive at 4.534 vs 4.532, not a real improvement).
  2. Combined starting-pitcher ERA (home_era + away_era, same walk-forward
     point-in-time data validated for the moneyline blend): correlation with
     actual game total is only r=0.069, and a full linear regression against
     it barely moves the residual std (4.544 -> 4.533) -- essentially no
     signal.
Both real, checked negative findings, still true -- MLB total runs are close
to unpredictable from team-behavior signals at the precision needed to beat
a flat league-average baseline, unlike NFL's modest improvement or NBA's
large one.

RE-TESTED ON BETTER METRICS 2026-08-15 (#201), AFTER the pitcher term shipped.
Candidate 1's rejection above used CURRENT-season trailing RUNS. Re-asked with
PRIOR-season team OPS -- a different time base and a rate rather than an outcome,
which is exactly the distinction that overturned the pitcher rejection (see
PITCHER_KBB_SLOPE: ERA said no, K-BB% said yes). Fitted on the RESIDUAL after
park + pitchers so it could not claim variance the shipped model already
explains:

    team offence   +2.264 runs per 1.000 OPS   95% CI [-3.876, +8.392]
                   1-sd better matchup = +0.061 runs

CI spans zero. Two independent metrics on two different time bases now agree, so
the original rejection is STRENGTHENED rather than merely repeated.

BULLPEN, both halves, now also dead. Workload was rejected in #168
(corr -0.005 with the residual, check_mlb_bullpen_fatigue.py). Quality, tested
here on prior-season combined relief ERA against the same residual:

    bullpen ERA    -0.386 runs per 1.00 ERA    95% CI [-0.8019, +0.0321]

CI spans zero AND the sign is backwards -- negative means a WORSE bullpen would
predict FEWER runs, which is a noise signature, not a finding.

WHAT REMAINS UNMEASURED: all four of these used prior-season or trailing values.
A current-season AS-OF-DATE offence or bullpen measure is untested and would need
day-by-day accumulation to stay lookahead-free. Given four independent negatives,
the prior on it is low.

SCOPE OF THE PITCHER REJECTION, tightened 2026-08-14. Candidate 2 tested
combined current-season ERA and nothing else. ERA charges a pitcher for balls
in play his defence handled, and it is measurably the weakest of the three
common descriptors here: on the same 15,352-game walk-forward set,
correlation with the game WINNER is era 0.089, FIP 0.105, K-BB% 0.110, and
out-of-sample K-BB% beats an Elo-only baseline in 8 of 9 held-out seasons
against ERA's 6 of 9 (see check_mlb_pitcher_metric.py). So "starting-pitcher
quality does not inform the TOTAL" has only ever been measured through the
noisiest available proxy. The better metrics have not been tested against
totals at all -- do not read candidate 2 as closing that question.

F5 (first-5-innings, 3-way incl. TIE): margin regression mirrors the
full-game one exactly (same walk-forward elo_diff, fit through the origin),
using real per-inning linescores for 2021-2025 + partial 2026 (13,591 games,
data/mlb_linescore_cache.json -- see build_mlb_linescore_cache.py/
derive_mlb_f5_rfi_constants.py). F5_MARGIN_SLOPE=0.008456, F5_MARGIN_STD=
3.3600, correlation elo_diff-vs-f5_margin r=0.151 (real, a bit weaker than
the full-game margin's r=0.186 -- expected, fewer innings means more
sampling noise per game even with the same real signal).

REAL CALIBRATION ISSUE caught and fixed before shipping: a naive continuity-
corrected Normal approximation (P(tie) = P(-0.5 < margin < 0.5)) UNDERSHOT
the real F5 tie rate badly -- predicted 11.7% on average vs. the actual
15.4% (n=13,591). Checked whether tie rate varies with matchup closeness
before assuming it's a universal constant (it could plausibly be higher in
close games) -- it does NOT: tie rate is flat at 14.9%-16.5% across every
|elo_diff| bucket from pick-em to 90+-point mismatches (correlation
-0.012, noise). F5_TIE_RATE is therefore used as a real EMPIRICAL constant,
not derived from the Normal margin model at all -- the continuous model's
own raw P(margin>0)/P(margin<0) split (which has no discreteness problem,
unlike the near-zero tie band) is used only for the home/away DIRECTION,
then rescaled so all three outcomes sum to 1. This is a real, checked
finding: MLB's low, discrete 5-inning run totals produce more mass at
"exactly tied" than a continuous approximation predicts, unlike a full
9-inning game (which has no comparable tie category to model at all, since
real games can't end tied by rule).

RFI (run scores in the 1st inning, binary): checked structural candidates
against real data rather than assuming a flat rate -- park-adjusted
expected_total(home_team) correlates only r=0.036 with RFI (too weak to use
alone), but combined (average) starting-pitcher CURRENT-SEASON ERA
correlates r=0.038 and is logistic-regression sign-consistent across a
first-half/second-half split of the data (coef 0.089 vs 0.063, n=8,292,
61% of games -- the other 39% lack a qualifying current-season ERA for one
or both starters, same MIN_IP gate as the moneyline pitcher blend). This is
a REAL but genuinely WEAK signal -- weaker than the moneyline pitcher signal or
the accepted park-factor total signal, more in the "barely clears noise" territory
this app's rejected signals
(bullpen fatigue, MLB team-scoring totals) also lived in, except this one
DOES clear it on both the correlation-vs-noise-floor math (n=8,292, SE~0.011,
r=0.038 is ~3.5 SE from zero) and the sign-consistency check. Shipped as a
real, honestly-modest structural adjustment (RFI_INTERCEPT/RFI_ERA_SLOPE,
raw-unit logistic fit) rather than rejected outright, but the practical
swing is small: full real combined-ERA range (3.0-5.5) moves predicted RFI
probability from only 47.4% to 52.3%. Falls back to the flat empirical
league rate (RFI_LEAGUE_AVG_RATE=0.4936) when current-season ERA isn't
available for both starters -- unknown, not guessed, same convention as the
moneyline pitcher blend.

TEMPERATURE: a real, checked, structural signal for totals -- closes a gap
this app has had since the NFL weather module was built (weather_rules.py's
own docstring: no free HISTORICAL weather source existed there, so its
total-suppression constant was hand-picked, never fitted against real
outcomes). Open-Meteo's archive API (free, no key, confirmed live) fixes
that for MLB: real hourly temperature at first pitch for 8,503 games
(2021-2025, the 21 OUTDOOR-roof ballparks only -- retractable/dome parks
excluded since this app has no historical roof-open/closed record and
including them would dilute a real outdoor-only effect toward zero, see
app/data/mlb_ballparks.py). Checked against the RESIDUAL after PARK_FACTOR
(isolating whatever park factor doesn't already explain, same "control for
what's already validated" logic as the pitcher-signal's elo_diff-redundancy
check): r=0.083, sign-consistent across a chronological half-split (0.079 vs
0.088), and the bucketed view is cleanly MONOTONIC and physically sensible
(8.28 runs at <50°F rising smoothly to 10.17 runs at 90°F+, a real ~1.9-run
swing across the observed range -- warmer air carries fly balls further, a
well-documented effect). TEMP_SLOPE=0.0365 runs/°F from a real linear fit,
LEAGUE_AVG_TEMP_F=72.6 (the real mean game-time temp in this same dataset,
used as the pivot so the adjustment is ~0 at a "typical" game).

REAL BUG caught and fixed while wiring up LIVE serving of this signal
(mlb_markets.py::_game_kickoff_local, 2026-07-17): `gametime` is a raw UTC
clock reading with no date, and naively pairing it with `gameday` (the LOCAL
date) assumes the UTC calendar day always equals the local one -- FALSE for
evening games at negative UTC offsets (the real instant is on gameday+1),
this app's own already-documented gameday/gametime ambiguity. Caught because
a same-day Coors Field game showed "no forecast available" when it should
have had one -- the miscalculated instant had landed a day in the past.
Fixed properly (both at serving time AND retroactively in
build_mlb_weather_cache.py, then the cache was rebuilt and this signal
re-derived) by trying both candidate UTC days and keeping whichever one's
local conversion round-trips back to the real `gameday` -- both halves of
the stored data are individually correct, only their day-pairing was
ambiguous, so this isn't a guess. The BUGGY version of this cache had
already shown a real, sign-consistent, monotonic signal (r=0.078) -- the
corrected version's r=0.083 confirms the bug was adding pure noise (day-off
weather is still similar weather), not a directional bias, but the
corrected numbers above are what's actually used.

WIND SPEED (magnitude only, no direction): checked and REJECTED, a real
negative finding -- correlation with the same park-factor residual is
r=-0.0003, essentially zero. Raw wind SPEED with no direction component
averages across "blowing out" and "blowing in" games and washes out.

WIND DIRECTION, relative to each park's own orientation: a real, validated,
SEPARATE signal from raw speed -- built after sourcing real ballpark
orientation data (Clem's Baseball, andrewclem.com/Baseball/
Stadium_statistics.html, sourced there from Lowry's "Green Cathedrals" and
other baseball references -- see app/data/mlb_ballparks.py::ORIENTATION_DEG;
several other ballpark-orientation sites exist but only as image diagrams,
not usable numeric data, so this was the one real source found with an
actual table). `out_wind_mph = wind_mph * cos(wind_from_deg - (park_orientation
+ 180))` (scripts/check_mlb_wind_direction_signal.py) -- positive when wind
blows OUT toward center field, negative when IN, zero for a pure crosswind.
Checked against the residual after PARK_FACTOR AND TEMPERATURE (isolating
whatever neither already explains): r=0.069 league-wide, sign-consistent
across a chronological half-split (0.079 vs 0.053), and the bucketed data is
monotonic (7.81 runs at strong "in" wind rising to 9.22 runs at strong "out"
wind, a real ~1.4-run swing). **Sanity-checked against a well-known specific
case before trusting the league-wide number**: Wrigley Field alone shows
r=0.277, roughly 4x the league average -- matching its real, famous
reputation for wind-driven scoring swings almost exactly, strong independent
evidence the orientation data and methodology are both correct (a wrong
orientation table would not have reproduced this). OUT_WIND_SLOPE=0.0406
runs/mph, fit through the origin (0 out-wind = no adjustment, a real,
meaningful zero point unlike temperature which needed a league-average
pivot).

PARK_FACTOR: a STRUCTURAL/physical signal (not team behavior), checked after
a deliberate second look rather than stopping at the team-behavior rejection
above -- same category as elo.py's Denver altitude bonus, not a contradiction
of the findings above. Real, large, well-known-in-baseball effect: games at
Coors Field (Colorado) average 11.49 total runs vs. the 9.05 league average
(+2.44, n=759, keyed by HOME TEAM not raw venue name -- venue name strings
fragment across seasons due to sponsorship renaming, e.g. "SunTrust Park" ->
"Truist Park", team identity doesn't). Confirmed BOTH home and away teams
score more at Coors specifically (home 5.53 vs league-average team-scoring
4.52, away 5.96 vs 4.52) -- a real shared physical effect (thin air ->
longer fly balls), not a home-team-specific artifact. Unlike Denver's NFL
altitude bonus (which needed conservative shrinkage because DEN's own team
strength confounded the raw win-rate gap across seasons), the FULL raw park
factor was tested and found to perform BEST in a walk-forward Brier check
(frac=1.0: 0.1921 vs frac=0.0/naive: 0.1943, monotonically improving at
every shrinkage fraction tried {0, .25, .5, .75, 1.0}) -- team scoring
quality doesn't confound this the same way, since it contributes symmetrically
to both a team's home and away splits, and the sample (759 games/team, SE on
the mean ~0.16 runs) is large enough that the raw estimate isn't noisy the
way DEN's smaller single-team altitude sample was. Applied at full strength,
NOT shrunk. Split evenly between home/away team's own expected runs for
team_total (a simplification -- the real home/away split was 5.53 vs 5.96 at
Coors, not perfectly even, but close enough not to warrant two separate
constants, same "don't over-fit a single-park asymmetry" judgment call as
other structural-adjustment halvings elsewhere in this app).
"""
import math

MARGIN_SLOPE = 0.012569
MARGIN_STD = 4.4265

F5_MARGIN_SLOPE = 0.008456
F5_MARGIN_STD = 3.3600
F5_TIE_RATE = 0.1542  # real empirical rate, NOT derived from the Normal margin model -- see module docstring

RFI_LEAGUE_AVG_RATE = 0.4936
RFI_INTERCEPT = -0.3413
RFI_ERA_SLOPE = 0.0789

TEMP_SLOPE = 0.0365  # runs per degree F, real linear fit vs PARK_FACTOR residual, n=8,503
LEAGUE_AVG_TEMP_F = 72.6  # real mean game-time temp, same 2021-2025/21-outdoor-ballpark dataset

OUT_WIND_SLOPE = 0.0406  # runs per "out wind" mph, through-origin fit vs PARK_FACTOR+TEMP residual, n=8,503

# CHALLENGED AND UPHELD 2026-08-15 (#200). Real season means are 8.958 (2026)
# and 8.9715 (2023-25 pooled), so this looks ~0.09 runs high -- but refitting it
# on recent seasons measurably HURTS. Walk-forward over 2016-2026 (2020 dropped,
# 60-game covid season), predicting each season from prior ones only:
#     trailing 3yr  mean |err| 0.2627 runs
#     trailing 5yr             0.2880
#     all prior                0.2983
#     THIS CONSTANT            0.1992   (vs 2021+ actuals)
# Season means swing from 8.5706 (2022) to 9.6694 (2019) -- a 1.1-run range, so a
# 0.09 offset is far inside the noise and a recency-chasing estimator tracks that
# noise. Do not "freshen" this without re-running that comparison.
LEAGUE_AVG_TOTAL = 9.0486
NAIVE_TOTAL_STD = 4.5357  # kept for reference/backtest ablation; PARK_FACTOR-adjusted std (below) is what's actually used
TOTAL_STD = 4.4891  # residual std AFTER applying PARK_FACTOR -- real, if modest, tightening vs naive
LEAGUE_AVG_TEAM_RUNS = LEAGUE_AVG_TOTAL / 2
TEAM_TOTAL_STD = 3.1801  # residual std after applying (half) PARK_FACTOR to a single team's expected runs

# Derived from the full 2016-2025 sample (759 games/team, home team only --
# see module docstring for why keyed this way and why full-strength, not
# shrunk). Additive offset to LEAGUE_AVG_TOTAL for a game played at that
# team's home park.
PARK_FACTOR = {
    "COL": 2.4375, "BOS": 0.9935, "AZ": 0.6694, "TEX": 0.6444, "CIN": 0.5522,
    "MIN": 0.4086, "BAL": 0.3572, "WSH": 0.2808, "ATL": 0.2152, "TOR": 0.2136,
    "KC": 0.1556, "LAA": 0.1371, "PHI": 0.0858, "NYY": 0.0014, "DET": -0.0301,
    "CWS": -0.1700, "PIT": -0.1845, "CHC": -0.2421, "ATH": -0.3174, "MIL": -0.3398,
    "HOU": -0.3846, "LAD": -0.3894, "CLE": -0.4365, "STL": -0.4874, "MIA": -0.5209,
    "NYM": -0.6771, "SD": -0.7127, "SEA": -0.7219, "SF": -0.7403, "TB": -0.7983,
}


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def expected_margin(elo_diff: float) -> float:
    """elo_diff is the home-perspective rating difference INCLUDING
    home-field-adv and the starting-pitcher blend (i.e.
    elo_service_mlb.get_elo_diff's return value)."""
    return MARGIN_SLOPE * elo_diff


def prob_team_covers(team_is_home: bool, line: float, elo_diff: float) -> float:
    """P(that team's margin of victory > line) -- "wins by more than line
    runs", matching how both Kalshi and Polymarket phrase these markets."""
    home_margin_mu = expected_margin(elo_diff)
    team_margin_mu = home_margin_mu if team_is_home else -home_margin_mu
    return 1.0 - _norm_cdf(line, team_margin_mu, MARGIN_STD)


def expected_total(home_team: str, temp_f: float | None = None, out_wind_mph: float | None = None,
                   combined_kbb: float | None = None) -> float:
    """home_team determines the park -- PARK_FACTOR.get(...) defaults to 0.0
    for any team not in the dict (shouldn't happen with the real 30-team
    set, but unknown = no adjustment, not a guess, same convention as
    elsewhere in this app). `temp_f` is the real game-time temperature at
    an OUTDOOR park (None -- the default -- for domed/retractable parks or
    when no live forecast is available yet, same "unknown = no adjustment"
    convention; see module docstring for TEMP_SLOPE's derivation). `out_wind_mph`
    is the signed wind-blowing-OUT component relative to the park's own
    orientation (positive = out, negative = in, see
    scripts/check_mlb_wind_direction_signal.py and OUT_WIND_SLOPE's
    derivation) -- same "unknown = no adjustment" convention."""
    total = LEAGUE_AVG_TOTAL + PARK_FACTOR.get(home_team, 0.0)
    if temp_f is not None:
        total += TEMP_SLOPE * (temp_f - LEAGUE_AVG_TEMP_F)
    if out_wind_mph is not None:
        total += OUT_WIND_SLOPE * out_wind_mph
    if combined_kbb is not None:
        total += PITCHER_KBB_SLOPE * (combined_kbb - LEAGUE_AVG_COMBINED_KBB)
    return total


# RUN TOTALS ARE SKEWED COUNTS, NOT A NORMAL (#194, 2026-08-15).
#
# Found via calibration_report: `mlb/total` was one of five flagged cells, and
# BOTH SIDES of the market missed in opposite directions -- which is what proves
# it real rather than a one-sided logging artifact:
#
#     side=over   n=982  claimed 0.605  actual 0.519  gap +0.086
#     side=under  n=201  claimed 0.438  actual 0.562  gap -0.124
#
# One directional fact: the model over-predicted scoring. NOT the stale mean --
# LEAGUE_AVG_TOTAL 9.0486 against a real 2026 mean of 8.958 is 0.09 runs, worth
# under 1pp at this sigma, while the live miss was 13-15pp.
#
# It is the SHAPE. Real regular-season totals, from statsapi:
#
#     2023 n=2433 mean 9.235 median 9.0 std 4.577 skew +0.636
#     2024 n=2428 mean 8.782 median 8.0 std 4.312 skew +0.731
#     2025 n=2429 mean 8.897 median 8.0 std 4.593 skew +0.823
#     2026 n=1839 mean 8.958 median 8.0 std 4.533 skew +0.739
#
# The median sits a full run below the mean EVERY season. A right-skewed variable
# has P(over ~mean) below 0.5; a symmetric Normal says exactly 0.5. Handed the
# CORRECT mean, the Normal still overstated P(over) by +0.060 at 7.5 and +0.054
# at 8.5 and 9.5 -- the lines that carry the volume.
#
# NEGATIVE BINOMIAL, NOT POISSON: variance ~20.2 against a mean ~9.0 is heavily
# overdispersed, and Poisson forces variance == mean (std ~3.0 vs a real ~4.5).
# NB has exactly the extra dispersion parameter that gap calls for and yields the
# right skew as a consequence rather than an assumption.
#
# FITTED 2023-2025 (n=7,290), VALIDATED ON 2026 (n=1,838) which was never used to
# fit. Both models given the test season's own mean, so this isolates shape:
#
#     line   actual   Normal (err)    NegBin (err)
#      6.5    0.680   0.708 (+0.028)  0.676 (-0.004)
#      7.5    0.567   0.627 (+0.060)  0.581 (+0.014)
#      8.5    0.487   0.540 (+0.053)  0.488 (+0.001)
#      9.5    0.397   0.452 (+0.054)  0.401 (+0.004)
#     10.5    0.329   0.365 (+0.037)  0.323 (-0.006)
#     11.5    0.259   0.285 (+0.026)  0.255 (-0.004)
#   mean |err|         0.0430          0.0054      NegBin wins 6/6
#
# The Normal's errors are all POSITIVE (systematic); NegBin's are +-0.01 with
# mixed sign (noise). See scripts/fit_mlb_total_distribution.py.
#
# TOTAL_STD is deliberately KEPT: it is still the honest residual spread and is
# referenced by the module docstring and backtest ablations. It is simply no
# longer what prices a total.
TOTAL_NB_DISPERSION = 7.1376       # r, fitted 2023-2025 (n=7,290)
TEAM_TOTAL_NB_DISPERSION = 3.5593  # r, team-level runs, same seasons (n=14,580)

# STARTING PITCHERS DO MOVE TOTALS (#199, 2026-08-15) -- answering the question
# this module's own docstring left open. "Candidate 2" above rejected pitchers
# for totals on combined current-season ERA (r=0.069), and that docstring already
# flagged the limitation: ERA is the noisiest of the three descriptors, and
# "the better metrics have not been tested against totals at all". They have now.
#
# K-BB% = (SO - BB) / BF, the metric check_mlb_pitcher_metric found most
# predictive. NO LOOKAHEAD BY CONSTRUCTION: fitted on each pitcher's PRIOR-SEASON
# line, fully known before the predicted season begins, so no as-of bookkeeping
# can be got subtly wrong. Fitted 2023-2025 (n=2,840 games where BOTH starters
# had a usable prior season), validated on 2026 (n=755) never used to fit:
#
#     raw slope -8.566 runs per unit K-BB%, 95% CI [-13.251, -3.936]
#     => a 1-sd better pitching matchup is worth -0.31 runs. Sign negative.
#
# THE RAW SLOPE OVERSHOOTS AND IS NOT WHAT SHIPS. On the held-out season the
# actual spread across pitching terciles was 0.325 runs while the raw slope
# predicted 0.642 -- 1.98x, mispricing ace matchups in the opposite direction
# instead of the old one. Shrink chosen by cross-validation INSIDE train (fit
# 2023-24, validate 2025; the curve bottoms cleanly at 0.4 -- 0.3619 vs 0.4299
# with no term and 0.3790 at full strength), never by looking at 2026.
#
# HELD-OUT RESULT, per-matchup error against actual runs:
#
#     tercile        flat      full slope   shrunk x0.4
#     worst pitching -0.234      +0.142       -0.084
#     middle         +0.021      +0.103       +0.054
#     best pitching  +0.091      -0.175       -0.016
#     mean |err|      0.115       0.140        0.051
#     spread vs actual   --       1.98x        0.79x
#
# THE AGGREGATE PREFERS THE FULL SLOPE AND THE AGGREGATE IS THE WRONG METRIC.
# Averaged P(over) across lines reads 0.0069 flat / 0.0036 full / 0.0055 shrunk,
# because errors CANCEL: flat under-prices ace matchups and over-prices bad ones,
# and the full slope overshoots in both directions. This app has been bitten by
# exactly that before -- restrictor-plate racing showed a mean gap of +-0.000
# while carrying ten times the decile error. Individual games get bet, not the
# average, so the per-matchup column decides and it picks the shrunk slope.
#
# COVERAGE IS PARTIAL AND THAT IS DELIBERATE: only ~37-40% of games had both
# starters clearing the prior-season threshold in the fit. Live, the gate is
# MIN_BF_FOR_KBB on current-season data; when either starter is unknown or too
# thin, get_combined_kbb returns None and expected_total prices exactly as it did
# before this term existed -- the same "unknown = no adjustment" convention as
# park and weather.
PITCHER_KBB_SLOPE = -3.4264        # -8.566 raw x 0.4 CV shrink
LEAGUE_AVG_COMBINED_KBB = 0.1568   # train mean, the pivot so the term is ~0 at a typical matchup


def _nb_sf(line: float, mean: float, r: float) -> float:
    """P(X > line) for a negative-binomial count with this mean and dispersion.

    Lines are half-integers, so there is no push and no continuity correction to
    argue about: P(X > 8.5) is exactly P(X >= 9).

    Falls back to the Normal if the mean is non-positive -- park/weather offsets
    are small enough (well under a run) that this cannot trigger for real inputs,
    but a NB is undefined at mean <= 0 and silently returning nonsense is worse
    than returning the old answer."""
    if mean <= 0.0 or r <= 0.0:
        return 1.0 - _norm_cdf(line, mean, TOTAL_STD)
    p = mean / (mean + r)
    k_max = int(math.floor(line))
    if k_max < 0:
        return 1.0
    base = -math.lgamma(r) + r * math.log1p(-p)
    log_p = math.log(p)
    cdf = 0.0
    for k in range(k_max + 1):
        cdf += math.exp(base + math.lgamma(k + r) - math.lgamma(k + 1) + k * log_p)
    return min(1.0, max(0.0, 1.0 - cdf))


def prob_over(line: float, home_team: str, temp_f: float | None = None, out_wind_mph: float | None = None,
              combined_kbb: float | None = None) -> float:
    return _nb_sf(line, expected_total(home_team, temp_f, out_wind_mph, combined_kbb), TOTAL_NB_DISPERSION)


def expected_f5_margin(elo_diff: float) -> float:
    return F5_MARGIN_SLOPE * elo_diff


def prob_f5_outcome(elo_diff: float) -> tuple[float, float, float]:
    """Returns (p_home_win, p_away_win, p_tie) for the first 5 innings. The
    continuous Normal model supplies only the home/away DIRECTIONAL split
    (P(margin>0) vs P(margin<0), which sum to 1 with no discreteness issue);
    F5_TIE_RATE is a real empirical constant, not derived from this
    distribution -- see module docstring for why a continuity-corrected tie
    band badly undershot the real tie rate."""
    mu = expected_f5_margin(elo_diff)
    p_home_raw = 1.0 - _norm_cdf(0.0, mu, F5_MARGIN_STD)
    p_away_raw = _norm_cdf(0.0, mu, F5_MARGIN_STD)
    non_tie = 1.0 - F5_TIE_RATE
    return p_home_raw * non_tie, p_away_raw * non_tie, F5_TIE_RATE


def prob_rfi(combined_era: float | None) -> float:
    """P(a run scores in the 1st inning). `combined_era` is the average of
    both starters' current-season ERA (elo_service_mlb.get_combined_era) --
    None (unavailable/too little IP for one or both starters) falls back to
    the flat empirical league rate, same "unknown = no adjustment"
    convention as the moneyline pitcher blend. See module docstring for why
    this is a real but genuinely weak signal."""
    if combined_era is None:
        return RFI_LEAGUE_AVG_RATE
    z = RFI_INTERCEPT + RFI_ERA_SLOPE * combined_era
    return 1.0 / (1.0 + math.exp(-z))


def prob_team_over(line: float, home_team: str, temp_f: float | None = None, out_wind_mph: float | None = None) -> float:
    """Same PARK_FACTOR, halved and applied to a single team's expected runs
    regardless of whether THIS team is the home or away side -- both sides
    play at `home_team`'s park, see module docstring for the confirmed
    symmetric home/away effect. `temp_f`/`out_wind_mph`, same halving logic,
    when available."""
    mu = LEAGUE_AVG_TEAM_RUNS + PARK_FACTOR.get(home_team, 0.0) / 2
    if temp_f is not None:
        mu += TEMP_SLOPE * (temp_f - LEAGUE_AVG_TEMP_F) / 2
    if out_wind_mph is not None:
        mu += OUT_WIND_SLOPE * out_wind_mph / 2
    # Same skew fix as prob_over, and team runs are MORE skewed than game
    # totals, not less -- train skew +1.013 vs +0.739, because a single team's
    # runs are bounded below by 0 with the same long upper tail. Validated on
    # the same held-out 2026 season (n=3,676 team-games), NegBin winning all 5
    # volume lines: mean |err| 0.0534 -> 0.0089. Normal errors again all
    # POSITIVE (+0.031 to +0.072), NegBin's mixed-sign and inside +-0.013.
    return _nb_sf(line, mu, TEAM_TOTAL_NB_DISPERSION)
