"""UFC fighter-level Elo rating engine -- same walk-forward architecture as
elo.py (NFL)/elo_nba.py/elo_mlb.py, but structurally different from all
three: there is no home/away side (no home-field-advantage term) and no
discrete season structure to regress ratings between (UFC runs continuously,
year-round, no offseason).

K: grid-searched (scripts/derive_mma_elo_constants.py) against this app's
own walk-forward Brier score on the full ufcstats.com historical scrape
(17,560 fight-rows / 8,780 fights, 1993-2026, scripts/build_ufc_fight_cache.py).
**K=72** -- a clean, smooth basin around 65-82 (Brier 0.24254 at the true
optimum vs. 0.24812 for a naive always-0.5 baseline, post-1,500-fight
warmup), not a noisy single-point spike, same credibility bar elo_mlb.py's
own K derivation used. Real, expected, and MUCH higher than NFL/NBA's K=20
or MLB's K=5: a UFC fighter has far fewer career fights than a team plays
games in even one NFL/NBA/MLB season, so each individual result needs to
move the rating more for it to converge to true skill within that much
smaller sample -- the mirror image of MLB's reasoning for why ITS K is so
low (162 games/season needs each one to matter less).

Draws (65 real fights in the historical data, confirmed via ufc_data.py's
is_draw field -- previously conflated with no-contests/unfought fights
before that was fixed) update ratings as a genuine 0.5/0.5 outcome, same
convention as elo_mlb.py's rare-tie handling. No-contests (91 fights) are
excluded entirely from both training and any future live scoring -- they
don't resolve a real skill question.

Real accuracy at K=72 (post-warmup, draws counted as a miss either way):
**56.2%** -- a real, modest signal above a 50% coin flip, but notably lower
than the ~65-66% "always bet the sportsbook favorite" accuracy this app's
separate, standalone UFC research project found. That's not a contradiction:
that number measured how good REAL SPORTSBOOK ODDS are (which price in
styles, injury reports, insider information, money flow), not a model's own
prediction accuracy from win/loss history alone -- this Elo module has zero
market information and zero fighter-feature data (styles, physical
attributes), so a smaller edge over a coin flip is the expected, honest
result for what it actually is: a pure win/loss rating, nothing more.

Whether UFC moneyline shows any real edge over the MARKET here is a
SEPARATE, re-tested question from this app's own earlier finding (a
different, standalone research project) that moneyline had none against
sportsbook odds -- this module ships regardless, labeled
model_validated: false, as an honest reference estimate, same as every
other sport's moneyline baseline in this app.

AGE ADJUSTMENT (added 2026-07-18, scripts/check_mma_situational_signals.py
+ scripts/check_mma_age_adjustment.py): checked whether Elo's own
walk-forward residual (actual outcome minus predicted probability)
correlates with fighter age BEFORE building anything, same "check first"
discipline as MLB's Phase 4. Real, robust finding: r=-0.1114 across all
fighters, and the effect gets STRONGER (r=-0.1363) when restricted to
fighters with >=4 prior UFC fights -- ruling out "Elo hasn't converged yet
for a young fighter" as the real mechanism (that confound would have
WEAKENED the effect on the experienced-only subset, not strengthened it).
Slope (experienced-only): -0.01672 win-probability per year of age around
a real mean age of 32.0. Converted to Elo points via the standard near-
p=0.5 logistic derivative (dp/d(diff) ~= 0.001439/point, same conversion
this app's NFL EPA-mismatch already uses) -> ~11.62 Elo points per year,
capped at +/-10 years from the 32.0 reference (real P1/P99 fighter-age
range is ~22-41). **Validated to actually improve walk-forward Brier
before shipping** (0.24255 -> 0.23453, a real, meaningfully larger
improvement than several already-shipped structural adjustments in this
app, e.g. MLB's ballpark factor was 0.1942->0.1921) -- confirms this
survives contact with the model, unlike NFL's turnover-margin-regression
experiment (real in isolation, made Brier worse once wired in, reverted).
Applied as a BASELINE/structural correction (same category as Denver
altitude/park factor), not the situational/news layer, since age is a
known, certain fact well before fight time, not a game-day uncertainty.
K=72 re-confirmed as still optimal with the age adjustment included (flat
0.23452-0.23453 across K=72-76).
"""
import math
from dataclasses import dataclass, field

BASE_RATING = 1500.0
K = 72.0

AGE_REFERENCE_YEARS = 32.0
AGE_ADJUSTMENT_ELO_PER_YEAR = 0.01672 / 0.001439  # ~11.62
MAX_AGE_ADJUSTMENT_DELTA_YEARS = 10.0


def age_adjustment_elo(age_years: float | None) -> float:
    """Elo-point adjustment for a fighter's age relative to AGE_REFERENCE_YEARS
    -- positive for younger-than-reference (real, validated outperformance
    vs. pure win/loss Elo), negative for older. 0.0 for unknown age (no
    DOB on file) -- unknown stays neutral, never guessed. See module
    docstring for the derivation and validation."""
    if age_years is None:
        return 0.0
    delta_years = max(-MAX_AGE_ADJUSTMENT_DELTA_YEARS, min(MAX_AGE_ADJUSTMENT_DELTA_YEARS, AGE_REFERENCE_YEARS - age_years))
    return AGE_ADJUSTMENT_ELO_PER_YEAR * delta_years


@dataclass
class MmaEloState:
    ratings: dict = field(default_factory=dict)

    def get(self, fighter_id: str) -> float:
        return self.ratings.get(fighter_id, BASE_RATING)


def win_prob(fighter_a_rating: float, fighter_b_rating: float) -> float:
    diff = fighter_a_rating - fighter_b_rating
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


def implied_elo_diff(prob: float) -> float:
    """Inverse of win_prob -- same role as elo.py's/elo_mlb.py's identical
    helper (would feed a future news-adjusted variant without hand-building
    a second version of every situational factor)."""
    prob = min(max(prob, 1e-6), 1 - 1e-6)
    return 400.0 * math.log10(prob / (1.0 - prob))


def update_ratings(
    state: MmaEloState, fighter_a_id: str, fighter_b_id: str, winner_id: str | None, is_draw: bool = False
) -> None:
    """No-op if winner_id is None and is_draw is False (no-contest/not yet
    fought) -- an unresolved fight shouldn't move either fighter's rating.
    A real draw (is_draw=True, winner_id still None) DOES update, as a
    genuine 0.5/0.5 outcome -- see module docstring."""
    if winner_id is None and not is_draw:
        return
    a_r = state.get(fighter_a_id)
    b_r = state.get(fighter_b_id)
    p_a = win_prob(a_r, b_r)
    actual_a = 0.5 if is_draw else (1.0 if winner_id == fighter_a_id else 0.0)
    delta = K * (actual_a - p_a)
    state.ratings[fighter_a_id] = a_r + delta
    state.ratings[fighter_b_id] = b_r - delta


def predict_and_update(
    state: MmaEloState, fight: dict,
    fighter_a_age: float | None = None, fighter_b_age: float | None = None,
) -> float | None:
    """Returns the PRE-fight fighter_a win probability (walk-forward, no
    leakage), then updates ratings with the actual result if known.
    Debut fighters (either side unseen) start at BASE_RATING -- same
    "unknown = league average" convention as every other sport's Elo here.
    `fight` is MmaFight-shaped (see ufc_data.py) -- reads is_draw/
    is_no_contest when present (historical backtest rows), defaults to
    False for live upcoming-fight rows (which never carry those keys).

    fighter_a_age/fighter_b_age shift only the RETURNED prediction, never
    the rating UPDATE below -- same "adjustment affects the estimate, not
    the underlying rating" convention as MLB's pitcher_adj (a young
    fighter's real age-driven edge shouldn't permanently inflate their
    pure win/loss Elo rating)."""
    a_r = state.get(fight["fighter_a_id"])
    b_r = state.get(fight["fighter_b_id"])
    p_a = win_prob(a_r + age_adjustment_elo(fighter_a_age), b_r + age_adjustment_elo(fighter_b_age))
    if fight.get("is_no_contest"):
        return p_a  # no-contest -- don't update ratings, but still return the pre-fight estimate
    update_ratings(
        state, fight["fighter_a_id"], fight["fighter_b_id"],
        fight.get("winner_id"), fight.get("is_draw", False),
    )
    return p_a
