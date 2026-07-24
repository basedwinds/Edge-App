"""Tennis player-level Elo rating engine -- same walk-forward architecture
as elo_mma.py (no home/away side, no discrete season structure to regress
between), but with a SURFACE-BLENDED rating, unlike any other sport's Elo
in this app: a player's true skill genuinely varies by surface (hard/clay/
grass) in a way that a single skill number doesn't capture, and this is a
standard, published tennis-rating design (used by Sackmann/FiveThirtyEight),
not a novel invention -- re-implemented fresh here for this app's own
player_key/TennisMatch schema (not ported) from the design validated in the
user's earlier standalone project (Downloads/tennis-model/elo_model.py).

Dynamic K (K = 250/(n+5)^0.4, the standard tennis-Elo formula) instead of a
single constant like elo_mma.py's K=72 -- a fresh player debuting mid-career
(e.g. a Challenger-level player with zero rating history in this dataset)
needs LARGE early updates to converge quickly, then smaller ones as more
matches accumulate confidence in the rating; a fixed K can't do both well
for a player pool spanning brand-new ITF qualifiers through 20-year tour
veterans in the same rating system.

Blended rating for prediction = weighted average of a player's OVERALL Elo
and SURFACE-specific Elo, weight on surface increasing with surface match
count (more surface experience -> trust the surface-specific number more).

SURFACE_MATCH_CAP/MAX_SURFACE_WEIGHT grid-searched fresh in this app's own
harness (2026-07-19, scripts/grid_search_tennis_surface_weight.py) against
the same 415,411-match walk-forward backtest as the go/no-go check below --
the standalone project's own borrowed values (40 matches to saturate, 75%
max surface weight) were never actually validated here before. Real,
efficient grid search: SURFACE_MATCH_CAP/MAX_SURFACE_WEIGHT only affect the
PREDICTION blend, never update_ratings(), so one walk-forward pass captured
each match's raw (overall_r, surface_r, surface_n) once, then every grid
cell was re-scored cheaply by just recomputing the blend afterward.

Real result: a clean, smooth basin (not a noisy spike) at LOW weight
(0.20-0.30) across EVERY cap value tried (4 to 150) -- the previously-
shipped 0.75 weight was trusting the surface-specific rating far more than
the data actually supports, hurting Brier at every cap tested. Cap itself
barely mattered once weight was near-optimal (0.21477 at cap=4 vs 0.21508 at
cap=150, both at their own best weight) -- picked the round cap=10/
weight=0.25 (Brier 0.21484) over the marginal absolute-minimum cap=4/
weight=0.25 (0.21477) since the difference is noise-level and a bumpy
discrete grid's single best cell isn't worth trusting over a robust,
flat-basin round number -- same "smooth basin over noisy spike" bar this
app's other sports already hold their own grid-searched constants to.

IMPORTANT caveat found via the SAME real per-tier check: this improvement
(Brier 0.21585 -> 0.21484 pooled) is concentrated ENTIRELY in the TOUR tier
(0.21533 -> 0.21106) -- CHALLENGER and ITF showed ZERO change (identical to
5 decimal places), because those two tiers' matches almost always carry
surface=None (tennisexplorer.com's results-day scrape doesn't expose
per-tournament surface, see tennisexplorer_client.py's own docstring), so
blended_rating() falls to the surface=None branch (pure overall rating)
regardless of these two constants' values for nearly every Challenger/ITF
match. Still far short of beating the market at any tier (see GO/NO-GO
result below, re-run with these new constants) -- a real, validated
improvement to the baseline's own internal quality, not a result that
flips the standing conclusion.

GO/NO-GO RESULT (scripts/backtest_moneyline_tennis.py, re-run 2026-07-19
with the grid-searched SURFACE_MATCH_CAP=10/MAX_SURFACE_WEIGHT=0.25 above;
originally run 2026-07-18 with the old borrowed 40/0.75 values -- see the
grid-search comment above for what changed and why): market beats this Elo
baseline at EVERY tier tested, no exceptions, same verdict before and after
the surface-weight fix --
  TOUR (n=105,273):       Elo Brier 0.2114 vs market 0.1957, favorite-acc 69.58% (was 0.2153/69.90% pre-fix)
  CHALLENGER (n=87,746):  Elo Brier 0.2323 vs market 0.2063, favorite-acc 67.21% (unchanged -- see caveat above)
  ITF (n=222,392):        Elo Brier 0.2096 vs market 0.1815, favorite-acc 72.53% (unchanged -- see caveat above)
The TOUR-level result (pre-fix) reconfirmed (fresh, in this app's own
harness, not ported) the user's earlier standalone tennis-model project's
finding (Brier 0.2117/favorite-acc 69.87% there vs 0.2153/69.90% here,
pre-fix -- consistent within noise, small numeric drift because ratings
here are trained on ONE shared cross-tier player pool, so a tour player who
also plays Challenger events picks up rating updates from those matches
too). CHALLENGER and ITF are a genuinely NEW result this session's live-data
audit made possible (the standalone project had zero training/backtest data
at these tiers) -- same clear NO-GO verdict, no edge found there either.
Ships anyway, model_validated: false, as an honest reference estimate --
same standing policy as every other sport's baseline in this app.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BASE_RATING = 1500.0
SURFACE_MATCH_CAP = 10.0
MAX_SURFACE_WEIGHT = 0.25

# Real play happened but the match didn't resolve a normal skill question the
# same way it would if both players competed the full match -- included in
# is_retirement (see TennisMatch/ufc_data.py's identical no-contest handling),
# excluded from Elo updates entirely (neither played out a real result).
EXCLUDED_FROM_TRAINING = "excluded"


def dynamic_k(n_matches: int) -> float:
    return 250.0 / (n_matches + 5) ** 0.4


def win_prob(rating_a: float, rating_b: float) -> float:
    diff = rating_a - rating_b
    return 1.0 / (1.0 + 10 ** (-diff / 400.0))


@dataclass
class TennisEloState:
    overall_ratings: dict = field(default_factory=dict)
    overall_counts: dict = field(default_factory=dict)
    surface_ratings: dict = field(default_factory=dict)  # (player_key, surface) -> rating
    surface_counts: dict = field(default_factory=dict)  # (player_key, surface) -> n

    def get_overall(self, player_key: str) -> tuple[float, int]:
        return self.overall_ratings.get(player_key, BASE_RATING), self.overall_counts.get(player_key, 0)

    def get_surface(self, player_key: str, surface: str | None) -> tuple[float, int]:
        if surface is None:
            return self.get_overall(player_key)
        return (
            self.surface_ratings.get((player_key, surface), self.overall_ratings.get(player_key, BASE_RATING)),
            self.surface_counts.get((player_key, surface), 0),
        )

    def blended_rating(self, player_key: str, surface: str | None) -> float:
        overall_r, _ = self.get_overall(player_key)
        if surface is None:
            return overall_r
        surface_r, surface_n = self.get_surface(player_key, surface)
        weight = min(surface_n / SURFACE_MATCH_CAP, 1.0) * MAX_SURFACE_WEIGHT
        return weight * surface_r + (1 - weight) * overall_r


def update_ratings(state: TennisEloState, player_a_key: str, player_b_key: str, winner_key: str, surface: str | None) -> None:
    """winner_key must be player_a_key or player_b_key -- callers gate on a
    real, scoreable result before calling this (see predict_and_update)."""
    a_overall, a_n = state.get_overall(player_a_key)
    b_overall, b_n = state.get_overall(player_b_key)
    actual_a = 1.0 if winner_key == player_a_key else 0.0

    p_a_overall = win_prob(a_overall, b_overall)
    k_a, k_b = dynamic_k(a_n), dynamic_k(b_n)
    state.overall_ratings[player_a_key] = a_overall + k_a * (actual_a - p_a_overall)
    state.overall_ratings[player_b_key] = b_overall - k_b * (actual_a - p_a_overall)
    state.overall_counts[player_a_key] = a_n + 1
    state.overall_counts[player_b_key] = b_n + 1

    if surface is None:
        return
    a_surf, a_sn = state.get_surface(player_a_key, surface)
    b_surf, b_sn = state.get_surface(player_b_key, surface)
    p_a_surface = win_prob(a_surf, b_surf)
    k_as, k_bs = dynamic_k(a_sn), dynamic_k(b_sn)
    state.surface_ratings[(player_a_key, surface)] = a_surf + k_as * (actual_a - p_a_surface)
    state.surface_ratings[(player_b_key, surface)] = b_surf - k_bs * (actual_a - p_a_surface)
    state.surface_counts[(player_a_key, surface)] = a_sn + 1
    state.surface_counts[(player_b_key, surface)] = b_sn + 1


def predict_and_update(state: TennisEloState, match: dict) -> float | None:
    """Returns the PRE-match player_a win probability (walk-forward, no
    leakage), then updates ratings with the actual result if the match was
    genuinely played to a real result. `match` is TennisMatch-shaped (see
    app/ingestion/tennis_data.py). Returns None (no prediction, no update)
    for a match with no winner_key at all (not yet played) -- a real
    retirement (is_retirement=1) still gets a PREDICTION returned (there IS
    a genuine pre-match question to estimate) but does NOT update ratings,
    same "predict but don't train on it" treatment as elo_mma.py's
    no-contest handling."""
    player_a_key, player_b_key = match["player_a_key"], match["player_b_key"]
    surface = match.get("surface")
    a_r = state.blended_rating(player_a_key, surface)
    b_r = state.blended_rating(player_b_key, surface)
    p_a = win_prob(a_r, b_r)

    winner_key = match.get("winner_key")
    if winner_key is None:
        return None if not match.get("is_retirement") else p_a
    if match.get("is_retirement"):
        return p_a  # real result exists, but a retirement isn't a clean skill signal -- predict, don't train
    update_ratings(state, player_a_key, player_b_key, winner_key, surface)
    return p_a
