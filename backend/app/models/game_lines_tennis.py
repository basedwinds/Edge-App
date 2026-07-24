"""Tennis set/game-line probability model -- set winner, game spread, game
total, exact match score. Parallel to game_lines.py (NFL)/game_lines_mlb.py/
game_lines_nba.py: every constant below is regressed against real historical
data (scripts/derive_tennis_game_line_constants.py, 491,775 matches with
real per-set game scores from tennis-data.co.uk + tennisexplorer.com), not
guessed. Uses the SAME walk-forward Elo diff this app's moneyline model
already uses -- these are independent derived relationships, not forced to
be mathematically consistent with elo_tennis.py's own match-level win_prob
curve (same "separately-fit, not reconciled" precedent as NFL's spread/total
regression vs. its own moneyline Elo).

No scipy dependency -- Normal CDF via math.erf, same convention as
game_lines.py.

REAL BUG fixed here (2026-07-18, caught while wiring Polymarket's set/game
markets, not during the original derivation): tennisexplorer.com's score
strings glue a tiebreak's LOSER-side point count onto whichever number
represents that set's LOSER -- and that can be either the first or second
number in the string, not always the second as the original parser
assumed (e.g. "67-7" is really "6-7", a set the FIRST-listed player lost
7-6 in a breaker where they scored 7 breaker points). The original
one-sided fix produced impossible per-set game totals (~65-76 combined
games in a single set) for ~7,674 real matches, which meaningfully
inflated TOTAL_GAMES_PARAMS/GAME_DIFF_STD (their residual std alone moved
from 13.85/12.32 down to 7.11/5.76 once fixed -- a huge, obviously-wrong
overestimate before). A second, opposite mistake was caught fixing the
first one: genuine long advantage-scoring sets (e.g. real "13-11", "17-15"
scores, common at Challenger/ITF level) also have 2-digit numbers on both
sides and must NOT be truncated -- see
app/ingestion/tennis_data.py::_parse_set_game_count for the actual fix
(distinguishes the two shapes by checking whether the raw digits START
with "6", which is only possible for a genuine tiebreak-set loser, never
for a real advantage-set score since play continues past 6-6 without a
breaker). Every constant below reflects the corrected data.
"""
from __future__ import annotations

import math

# --- Total games (game_diff regression split by best_of; a Bo5 match has
# ~36 games on average vs Bo3's ~21.5 -- pooling these would bias every Slam
# prediction toward the wrong mean, confirmed by refitting split). Bo5's own
# numbers are untouched by the tiebreak-parsing fix below (Bo5 matches are
# ATP Grand Slams only, sourced entirely from tennis-data.co.uk's clean
# numeric columns, never tennisexplorer's string-parsed scores) -- only
# Bo3's slope/intercept/std actually moved when the bug was fixed. ---
TOTAL_GAMES_PARAMS = {
    3: {"slope": -0.009336, "intercept": 22.7119, "std": 7.1122},
    5: {"slope": -0.014288, "intercept": 38.9577, "std": 9.2882},
}

# --- Game differential (player_a games won - player_b games won), vs signed
# Elo diff (a-b). Not split by best_of (best_of=5 is a small minority even
# within tour-level ATP, and Bo5's naturally larger total-games scale is
# already handled by the total-games model above; the differential's own
# slope/std didn't show a comparably large best_of split in the same
# derivation run). ---
GAME_DIFF_SLOPE = 0.016533
GAME_DIFF_STD = 5.7600

# --- Per-set win probability (logistic, fit via gradient descent on every
# real set outcome in the merged dataset, walk-forward Elo diff at match
# time) ---
SET_WIN_INTERCEPT = 0.0
SET_WIN_SLOPE = 0.002688

# --- Exact match score (favorite_sets-underdog_sets), empirical frequency
# table bucketed by the FAVORITE's own win-prob decile and best_of -- a
# nonparametric fit directly from real scorelines, not derived from the
# per-set logistic's iid-sets assumption (that assumption was checked
# separately for the best-of-5 correction and found to make things WORSE
# when forced into the match-probability calculation, see
# check_tennis_best_of_signal.py/validate_tennis_best_of_correction.py's
# docstring -- using real empirical frequencies here sidesteps needing that
# assumption to hold for this different purpose). Keys are (decile 0-9,
# best_of) -> {(favorite_sets, underdog_sets): probability}. Only rows with
# a real n>=50 sample were kept from the derivation run's output; a bucket
# not present here has no real estimate and callers should return None
# rather than guess.
EXACT_SCORE_TABLE: dict[tuple[int, int], dict[tuple[int, int], float]] = {
    (5, 3): {(2, 0): 0.670, (2, 1): 0.319, (1, 0): 0.008, (1, 1): 0.002, (0, 1): 0.001, (0, 0): 0.001},
    (5, 5): {(3, 0): 0.407, (3, 1): 0.347, (3, 2): 0.244},
    (6, 3): {(2, 0): 0.679, (2, 1): 0.309, (1, 0): 0.008, (1, 1): 0.002, (0, 1): 0.001},
    (6, 5): {(3, 0): 0.427, (3, 1): 0.345, (3, 2): 0.227},
    (7, 3): {(2, 0): 0.710, (2, 1): 0.278, (1, 0): 0.008, (1, 1): 0.002, (0, 1): 0.001},
    (7, 5): {(3, 0): 0.484, (3, 1): 0.323, (3, 2): 0.191},
    (8, 3): {(2, 0): 0.774, (2, 1): 0.217, (1, 0): 0.007, (1, 1): 0.001, (0, 1): 0.001},
    (8, 5): {(3, 0): 0.543, (3, 1): 0.293, (3, 2): 0.162},
    (9, 3): {(2, 0): 0.865, (2, 1): 0.127, (1, 0): 0.006, (1, 1): 0.001},
    (9, 5): {(3, 0): 0.727, (3, 1): 0.205, (3, 2): 0.068},
}

# --- Games within a SINGLE set (Polymarket's "Set N O/U X.5" market --
# Kalshi has no per-set total, only match-level). Fit on real first-set
# game totals only (deliberately not later sets, which would need their own
# separate fit if a real difference emerged -- not checked, first-set data
# alone was enough for a real, tight fit: resid_std=2.03 games, versus
# ~7-9 for a full match, matching the obvious intuition that one set has
# far less accumulated variance than a whole match). Excludes the tiny
# residual tail of malformed/very-long-set rows (>25 combined games) that
# even the corrected parser can't fully resolve -- see
# app/ingestion/tennis_data.py::_parse_set_game_count's own docstring. ---
SET_GAMES_SLOPE = -0.001895
SET_GAMES_INTERCEPT = 9.4613
SET_GAMES_STD = 2.0338


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_win_set(elo_diff: float) -> float:
    """P(player A wins a given set), from the per-set logistic fit."""
    z = SET_WIN_INTERCEPT + SET_WIN_SLOPE * elo_diff
    return 1.0 / (1.0 + math.exp(-z))


def expected_total_games(elo_diff: float, best_of: int) -> tuple[float, float]:
    """Returns (expected_total_games, residual_std) for this best_of format.
    Falls back to best_of=3 params for any unexpected value (never guesses
    a brand-new number, just uses the more common format's real fit)."""
    p = TOTAL_GAMES_PARAMS.get(best_of, TOTAL_GAMES_PARAMS[3])
    expected = p["intercept"] + p["slope"] * abs(elo_diff)
    return expected, p["std"]


def prob_over_total_games(line: float, elo_diff: float, best_of: int) -> float:
    expected, std = expected_total_games(elo_diff, best_of)
    z = (line - expected) / std
    return 1.0 - _norm_cdf(z)


def expected_game_diff(elo_diff: float) -> float:
    return GAME_DIFF_SLOPE * elo_diff


def prob_game_spread_cover(line: float, elo_diff: float) -> float:
    """P(player A's games won minus player B's games won exceeds `line`) --
    same "wins by more than line" convention as game_lines.py's
    prob_team_covers (positive line = favorite must win by more than that
    many games; negative line = underdog must not lose by that many)."""
    expected = expected_game_diff(elo_diff)
    z = (line - expected) / GAME_DIFF_STD
    return 1.0 - _norm_cdf(z)


def prob_exact_score(elo_diff: float, best_of: int, win_prob_a: float) -> dict[tuple[int, int], float] | None:
    """Returns {(player_a_sets, player_b_sets): probability, ...} or None if
    no real empirical bucket exists for this best_of/decile combination.
    Looks up the table from the FAVORITE's perspective (the table is
    favorite-centric, since that's how it was derived) then flips back to
    player_a/player_b terms."""
    favorite_is_a = win_prob_a >= 0.5
    favorite_p = win_prob_a if favorite_is_a else 1.0 - win_prob_a
    decile = min(int(favorite_p * 10), 9)
    bucket = EXACT_SCORE_TABLE.get((decile, best_of))
    if bucket is None:
        return None
    result: dict[tuple[int, int], float] = {}
    for (fav_sets, dog_sets), prob in bucket.items():
        key = (fav_sets, dog_sets) if favorite_is_a else (dog_sets, fav_sets)
        result[key] = prob
    return result


def prob_over_total_sets(elo_diff: float, best_of: int, win_prob_a: float, line: float) -> float | None:
    """P(match goes MORE than `line` sets) -- Polymarket's "Total Sets O/U"
    market (no Kalshi equivalent). Derived for free from the same empirical
    EXACT_SCORE_TABLE (sums the probability of every real scoreline whose
    total set count exceeds the line) rather than needing its own
    constant."""
    table = prob_exact_score(elo_diff, best_of, win_prob_a)
    if table is None:
        return None
    return sum(p for (a_sets, b_sets), p in table.items() if (a_sets + b_sets) > line)


def expected_set_games(elo_diff: float) -> float:
    return SET_GAMES_INTERCEPT + SET_GAMES_SLOPE * abs(elo_diff)


def prob_over_set_games(line: float, elo_diff: float) -> float:
    """P(a given SET has more than `line` total games) -- Polymarket's
    "Set N O/U X.5" market (no Kalshi equivalent, Kalshi's game total is
    match-level only). Uses the SAME expected value/std for every set
    number (only fit against real first-set data -- see SET_GAMES_STD's own
    docstring on why later sets weren't separately checked)."""
    expected = expected_set_games(elo_diff)
    z = (line - expected) / SET_GAMES_STD
    return 1.0 - _norm_cdf(z)
