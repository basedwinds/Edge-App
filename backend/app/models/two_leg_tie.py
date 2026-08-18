"""P(advance) for a TWO-LEGGED knockout tie.

WHY THIS IS ITS OWN MODULE. uefa_match.py and conmebol_match.py both refuse to
answer "who advances", and their docstrings say so: a single-match goal
distribution cannot express an aggregate across two matches, and cup_match's
SINGLE-leg advance formula is a different question that would be silently wrong
here. This module is the missing piece, and it takes single-leg distributions as
INPUT rather than re-deriving them, so it works for UEFA and CONMEBOL alike
without either model learning about the other.

THE ARITHMETIC. Goals in each leg are independent Poissons, so a side's
AGGREGATE over the remaining legs is Poisson with the summed lambda -- no
convolution loop is needed, which is why this stays cheap enough to run inside a
market pass:

    P(A advances) = SUM over (i, j) of P(A_rem = i) * P(B_rem = j) * f(lead + i - j)

        f(d) = 1        when d > 0   (A through on aggregate)
               0        when d < 0
               p_et     when d = 0   (level -> extra time, then penalties)

`lead` is A's aggregate margin from any leg already played, so the same function
serves a tie that has not started (lead = 0, both legs' lambdas summed) and one
sitting on a first-leg result (lead = a1 - b1, only leg 2's lambdas remaining).

EXTRA TIME AND PENALTIES.
  * Extra time is 30 minutes, modelled as EXTRA_TIME_FRACTION (=30/90) of the
    second leg's scoring rates, at the second leg's venue -- so the home side in
    the deciding leg keeps its home term through ET, which is where ET is
    actually played.
  * A shootout is treated as a coin flip. That is not laziness: shootout outcome
    is famously close to unpredictable from team strength, and inventing a
    strength-tilted number here would add confident noise. It is a named
    constant so it can be tested rather than argued about.

NO AWAY-GOALS RULE, deliberately. UEFA abolished it in 2021 and CONMEBOL in
2023. Every tie this module will ever price is post-abolition. Implementing it
"just in case" would be a rule that fires on no real fixture and quietly changes
answers if it did.

model_validated stays False.
"""
from __future__ import annotations

import math

# 30 minutes of extra time against a 90-minute leg.
EXTRA_TIME_FRACTION = 30.0 / 90.0

# A penalty shootout, priced as a coin flip. See the docstring.
SHOOTOUT_HOME_PROB = 0.5

# Poisson tail cut. 12 goals in the REMAINING legs is already absurd; the
# truncated mass is < 1e-9 for any realistic lambda and the grid stays 13x13.
_MAX_GOALS = 12


def _poisson_pmf(lam: float, k_max: int = _MAX_GOALS) -> list[float]:
    """P(X = 0..k_max) for X ~ Poisson(lam), normalised over the truncation so
    the returned vector sums to 1 and the caller never loses probability into
    the tail it cannot see."""
    lam = max(float(lam), 1e-9)
    out, term = [], math.exp(-lam)
    for k in range(k_max + 1):
        out.append(term)
        term = term * lam / (k + 1)
    total = sum(out)
    return [p / total for p in out]


def prob_level_after_extra_time(lam_a_et: float, lam_b_et: float) -> float:
    """P(A wins the tie | aggregate level at 90' of the second leg).

    Extra time first, then a shootout for whatever is still level."""
    pa, pb = _poisson_pmf(lam_a_et), _poisson_pmf(lam_b_et)
    win = draw = 0.0
    for i, p_i in enumerate(pa):
        for j, p_j in enumerate(pb):
            p = p_i * p_j
            if i > j:
                win += p
            elif i == j:
                draw += p
    return win + draw * SHOOTOUT_HOME_PROB


def prob_advance(
    lam_a_remaining: float,
    lam_b_remaining: float,
    lead: int = 0,
    lam_a_extra_time: float | None = None,
    lam_b_extra_time: float | None = None,
) -> float:
    """P(team A advances).

    lam_*_remaining -- summed scoring rate over the legs STILL TO BE PLAYED.
    lead            -- A's aggregate margin so far (0 before the first leg).
    lam_*_extra_time -- ET rates; default to EXTRA_TIME_FRACTION of the
                        remaining rates, which is right when one leg remains and
                        is the only sensible default when two do.

    A is whichever side the caller wants the answer for; the caller is
    responsible for passing that side's lambdas as the `a` arguments. Getting
    that backwards is the classic ordered-tuple error, so callers should assert
    against the fixture's own home/away rather than trusting position.
    """
    if lam_a_extra_time is None:
        lam_a_extra_time = lam_a_remaining * EXTRA_TIME_FRACTION
    if lam_b_extra_time is None:
        lam_b_extra_time = lam_b_remaining * EXTRA_TIME_FRACTION

    p_et = prob_level_after_extra_time(lam_a_extra_time, lam_b_extra_time)
    pa, pb = _poisson_pmf(lam_a_remaining), _poisson_pmf(lam_b_remaining)

    total = 0.0
    for i, p_i in enumerate(pa):
        if p_i < 1e-12:
            continue
        for j, p_j in enumerate(pb):
            d = lead + i - j
            if d > 0:
                total += p_i * p_j
            elif d == 0:
                total += p_i * p_j * p_et
    return total
