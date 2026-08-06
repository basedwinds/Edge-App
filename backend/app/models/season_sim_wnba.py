"""WNBA season Monte Carlo -- the win distribution behind Kalshi's KXWNBAWINS
ladders ("Will <team> win at least N games this season?", 45 open markets).

Same shape as season_sim_cfb: simulate every REMAINING game from the Elo win
probability, count wins, return a per-team histogram. Games already played
contribute their real result, so the answer sharpens through the season instead
of staying a preseason guess.

WHY IT FETCHES ITS OWN SCHEDULE. poller_wnba only ingests 14 days ahead
(espn_wnba_client.FORWARD_DAYS), which is right for pricing games but useless
here: measured 2026-08-02 the WnbaGame table held 282 rows ending 2026-08-16,
while a WNBA team plays 44 games. Simulating off that would count only part of
each team's remaining schedule and understate every win total -- badly, and
silently. So this pulls the season-wide schedule itself and caches it.

Unlike CFB there is no FCS problem: every WNBA opponent is a rated WNBA team, so
there is no unrated-opponent fallback to reason about.
"""
import datetime
import logging
import threading
import time

import numpy as np

from app.clients import espn_wnba_client
from app.ingestion import wnba_data
from app.ingestion.market_matcher_wnba import KALSHI_TEAM_ABBRS, to_espn_abbr
from app.models.baseline import elo_service_wnba

# The 15 real franchises, in ESPN abbreviations (the form WnbaGame stores).
REAL_TEAMS = {to_espn_abbr(a) for a in KALSHI_TEAM_ABBRS}

log = logging.getLogger("season_sim_wnba")

# The WNBA regular season runs roughly May to mid-September; the window is kept
# wide so a schedule shift can't truncate it.
_SEASON_START = (4, 15)
_SEASON_END = (9, 30)

# Per-season, per-team Elo uncertainty, in Elo points -- the same fix already
# shipped for CFB (225), NFL (100) and NBA (75). Without it each rating is
# treated as EXACTLY known and every game as an independent coin, making a
# 44-game season ~binomial and far too narrow. Real pre-season uncertainty is
# about the TEAM and persists all season, which fattens the tails in a way
# independent flips cannot.
#
# 100 is measured. Backtest on data/wnba_game_cache.json (2021-2026), seasons
# 2023-2025 each projected from PRIOR-SEASONS-ONLY Elo with zero games played,
# 280 team-threshold predictions:
#
#   sigma=0     mean abs gap 7.77pp, Brier 0.1040
#   sigma=100   mean abs gap 1.50pp, Brier 0.0936  (every bucket within 3.7pp)
#
# Leave-one-season-out improved all 3 held-out seasons (7.23->5.37, 10.80->4.54,
# 8.80->4.53) and fitted 100/100/125, so 100 is the modal AND the low end.
#
# WEAKER EVIDENCE THAN NBA'S, and deliberately noted: only 3 testable seasons
# and n=280, versus NBA's 10 seasons and n=2,700. The DIRECTION is unanimous
# across folds; the exact magnitude is not pinned down as tightly.
TEAM_STRENGTH_SIGMA = 100.0

_TTL = 3600
# Retry window for a FAILED run (empty distribution) -- see warm().
_FAILURE_TTL = 120
_lock = threading.Lock()
_cache: dict = {}


def _season_bounds(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    year = today.year
    return datetime.date(year, *_SEASON_START), datetime.date(year, *_SEASON_END)


def _fetch_season_games() -> list[dict]:
    """Season-wide schedule, bypassing the poller's 14-day horizon. Calls the
    client's own date-range fetch directly rather than fetch_scoreboard_events'
    default window."""
    start, end = _season_bounds(datetime.date.today())
    return wnba_data.fetch_games(start, end, respect_horizon=False)


def simulate(trials: int = 4000, games: list[dict] | None = None, _with_wins: bool = False):
    """{team: {win count: how many simulated seasons ended there}}.

    With `_with_wins`, also returns the raw (trials x teams) win matrix and the
    team order, so seed/playoff probabilities can be read off the SAME
    simulation rather than a second one. Two sims would silently disagree --
    a team could be shown 30% for the #1 seed while its own win distribution
    said something else -- which is the reasoning-vs-pricing split this repo
    has now hit three separate times.

    Vectorised over trials -- each remaining game is one Bernoulli draw per
    simulated season, so a whole season is a (trials x games) boolean matrix."""
    games = _fetch_season_games() if games is None else games
    # Regular season only: preseason games don't count toward a win total, and
    # including them would inflate every team.
    games = [g for g in games if g.get("game_type") == "REG"]
    # ESPN tags the All-Star game "REG" too, and its participants are the exhibition
    # squads (2026: "COOP" / "SPO"), not franchises. They only ever play each other,
    # so no real team's win count is affected -- but they would otherwise appear as
    # two extra "teams" in the output, and an abbreviation collision with a real
    # franchise would silently corrupt a win total. Restrict to real franchises.
    games = [
        g for g in games
        if g["home_team"] in REAL_TEAMS and g["away_team"] in REAL_TEAMS
    ]
    if not games:
        return ({}, None, []) if _with_wins else {}

    teams = sorted({g["home_team"] for g in games} | {g["away_team"] for g in games})
    idx = {t: i for i, t in enumerate(teams)}
    banked = np.zeros(len(teams), dtype=np.int32)
    probs: list[float] = []
    pair: list[tuple[int, int]] = []

    for g in games:
        h, a = g["home_team"], g["away_team"]
        if g.get("home_score") is not None and g.get("away_score") is not None:
            banked[idx[h if g["home_score"] > g["away_score"] else a]] += 1
            continue
        p = elo_service_wnba.get_home_win_prob(h, a, g.get("location"))
        if p is None:
            continue
        probs.append(float(p))
        pair.append((idx[h], idx[a]))

    # REFUSE to price off a schedule whose remaining games mostly failed to rate.
    # Skipping them doesn't produce a wide distribution, it produces a POINT MASS
    # at the banked wins -- every threshold below it reads 1.0 and every one above
    # reads 0.0, which is a certainty the data does not support. Caught live: a
    # cold Elo cache (get_home_win_prob returns None until refresh_ratings runs)
    # made every remaining game unrateable and priced MIN 25+ wins at exactly 1.0,
    # with a real stake attached. An empty dict shows "no season projection"
    # instead, which is the honest answer.
    unplayed = sum(
        1 for g in games
        if g.get("home_score") is None or g.get("away_score") is None
    )
    if unplayed and len(probs) < 0.5 * unplayed:
        log.error(
            "wnba season sim: only %d of %d remaining games could be rated -- "
            "refusing to return a distribution", len(probs), unplayed,
        )
        return ({}, None, []) if _with_wins else {}

    counts: dict[str, dict[int, int]] = {t: {} for t in teams}
    if not probs:
        # Season genuinely complete: every game has a real result, so a point mass
        # at the final win count is correct rather than a fabricated certainty.
        for t in teams:
            counts[t][int(banked[idx[t]])] = trials
        if _with_wins:
            # Season over: every trial is the same real final table.
            return counts, np.tile(banked, (trials, 1)).astype(np.int32), teams
        return counts

    p_arr = np.array(probs)
    hi = np.array([x[0] for x in pair])
    ai = np.array([x[1] for x in pair])
    rng = np.random.default_rng()

    # Per-trial team-strength offsets (TEAM_STRENGTH_SIGMA). This module keeps
    # PROBABILITIES rather than ratings, so the offset is applied in Elo space
    # by inverting each probability back to the effective Elo diff it implies,
    # shifting it, and converting back. Inverting rather than re-deriving from
    # raw ratings deliberately preserves everything get_home_win_prob already
    # folded in (home-court advantage, neutral sites), which a re-derivation
    # would silently drop.
    p_safe = np.clip(p_arr, 1e-6, 1 - 1e-6)
    diff_arr = 400.0 * np.log10(p_safe / (1.0 - p_safe))

    wins = np.tile(banked, (trials, 1)).astype(np.int32)
    done = 0
    while done < trials:
        n = min(500, trials - done)
        if TEAM_STRENGTH_SIGMA > 0:
            # One draw per simulated season per team, held across all its games.
            off = rng.normal(0.0, TEAM_STRENGTH_SIGMA, size=(n, len(teams)))
            d = diff_arr[None, :] + off[:, hi] - off[:, ai]
            p_mat = 1.0 / (1.0 + 10.0 ** (-d / 400.0))
            hw = rng.random((n, len(p_arr))) < p_mat
        else:
            hw = rng.random((n, len(p_arr))) < p_arr
        np.add.at(wins[done:done + n], (slice(None), hi), hw)
        np.add.at(wins[done:done + n], (slice(None), ai), ~hw)
        done += n

    for t in teams:
        vals, cnt = np.unique(wins[:, idx[t]], return_counts=True)
        counts[t] = {int(v): int(c) for v, c in zip(vals, cnt)}
    return (counts, wins, teams) if _with_wins else counts


# The WNBA takes the top 8 records into the playoffs, league-wide -- there are
# no conferences to qualify out of, so "made the playoffs" is purely a
# regular-season finishing position and needs no bracket.
PLAYOFF_FIELD_SIZE = 8


def _standings_from_wins(wins, teams) -> dict[str, dict[str, float]]:
    """Seed/playoff probabilities from an already-computed win matrix."""
    if wins is None or not teams:
        return {}
    return _standings_impl(wins, teams)


def standings_probs(trials: int = 4000, games: list[dict] | None = None) -> dict[str, dict[str, float]]:
    """{team: {"one_seed": p, "playoff": p}} for KXWNBA1SEED / KXWNBAPLAYOFF.

    Read off the SAME simulation as the win-total ladders (simulate's own win
    matrix), so a team's seed probability and its win distribution can never
    disagree.

    NO BRACKET IS INVOLVED, and that is the point. Both markets resolve on
    regular-season finishing position: the #1 seed is the best record, and the
    playoff field is simply the top 8. The championship market WOULD need a
    bracket -- KXWNBACHAMP has 0 open markets (checked 2026-08-06), so nothing
    is built for it rather than guessing at a series model nobody can bet.

    TIES ARE SPLIT, NOT BROKEN. The real league breaks a tie on head-to-head
    and division record, which this sim does not track. A trial where k teams
    share the best record contributes 1/k to each of them, which keeps the
    column summing to 1.0 -- rather than awarding it to whichever team happens
    to sort first, which would bias systematically toward one franchise.
    """
    _counts, wins, teams = simulate(trials=trials, games=games, _with_wins=True)
    if wins is None or not teams:
        return {}
    return _standings_impl(wins, teams)


def _standings_impl(wins, teams) -> dict[str, dict[str, float]]:
    n_trials = wins.shape[0]

    best = wins.max(axis=1, keepdims=True)
    is_best = wins == best                       # (trials x teams) bool
    n_tied = is_best.sum(axis=1, keepdims=True)  # how many share the top record
    one_seed = (is_best / n_tied).sum(axis=0) / n_trials

    # Top 8 by wins. A cut-line tie is split the same way: everyone strictly
    # above the 8th-best record is in, and the teams level with it share the
    # remaining slots.
    order = np.sort(wins, axis=1)[:, ::-1]
    cutoff = order[:, PLAYOFF_FIELD_SIZE - 1][:, None]
    strictly_in = wins > cutoff
    on_line = wins == cutoff
    slots_left = PLAYOFF_FIELD_SIZE - strictly_in.sum(axis=1, keepdims=True)
    share = np.where(on_line.sum(axis=1, keepdims=True) > 0,
                     slots_left / np.maximum(on_line.sum(axis=1, keepdims=True), 1), 0.0)
    playoff = (strictly_in + on_line * share).sum(axis=0) / n_trials

    return {
        t: {"one_seed": round(float(one_seed[i]), 4), "playoff": round(float(playoff[i]), 4)}
        for i, t in enumerate(teams)
    }


# --- Playoff bracket -------------------------------------------------------
#
# THE FORMAT IS RECOVERED FROM PLAY, NOT FROM MEMORY. Kalshi's rules state the
# question ("does X qualify for the Finals") but never the bracket, so the
# pairing rule was reconstructed from the 2024 and 2025 postseasons in
# data/wnba_game_cache.json -- a playoff series is the same pair meeting
# repeatedly at the end of a season, which the regular schedule never does.
# Both seasons agree, and they agree on the thing that actually matters:
#
#   2025  after round 1: MIN(1), LV(2), IND(4), PHX(6)
#         semifinals played: MIN-PHX and LV-IND
#   2024  after round 1: NY(1), MIN(2), CON(3), LV(5)
#         semifinals played: NY-LV and MIN-CON
#
# In both, the highest remaining seed drew the LOWEST remaining seed. The
# league RESEEDS after round 1; it is not a fixed bracket. A fixed bracket
# would have paired 2025's MIN(1) with IND(4) and 2024's NY(1) with CON(3),
# and neither happened.
_ROUND1_PAIRS = ((0, 7), (1, 6), (2, 5), (3, 4))  # seed indices: 1v8, 2v7, 3v6, 4v5

# Bracket trials, deliberately below the ladders' 4,000: each trial plays up to
# 19 extra series games in a Python loop, and a title price is not read to the
# fourth decimal the way a win-total threshold is. At 1,500 the sampling error
# on a 40% estimate is about 1.3pp.
_BRACKET_TRIALS = 1500

# Venue pattern per series, from the higher seed's perspective. The higher seed
# hosts the majority in every WNBA round. ASSUMED, unlike the pairing rule
# above: the exact game-by-game venue order is not recoverable from a cache
# with no venue-vs-seed labelling, and it only weights home advantage -- it
# cannot change who plays whom. Finals length is likewise assumed best-of-5;
# 2025's Finals ended 4-0, which a best-of-5 and a best-of-7 both allow, so the
# data cannot separate them.
_SERIES_VENUES = {
    "round1": (True, False, True),                       # best-of-3
    "semifinal": (True, True, False, False, True),       # best-of-5, 2-2-1
    "finals": (True, True, False, False, True),          # best-of-5, 2-2-1
}


def _series_winner(hi: str, lo: str, venues, rng) -> str:
    """Play one series. `hi` is the higher seed; venues[i] True = hi at home.

    Game-by-game rather than a closed-form best-of-N so home advantage can
    differ per game, which a single series-level probability cannot express.
    """
    need = len(venues) // 2 + 1
    w_hi = w_lo = 0
    for at_home in venues:
        p = (elo_service_wnba.get_home_win_prob(hi, lo, None) if at_home
             else 1.0 - (elo_service_wnba.get_home_win_prob(lo, hi, None) or 0.5))
        if p is None:
            p = 0.5
        if rng.random() < p:
            w_hi += 1
        else:
            w_lo += 1
        if w_hi == need or w_lo == need:
            break
    return hi if w_hi > w_lo else lo


def bracket_probs(trials: int = 2000, games: list[dict] | None = None) -> dict[str, dict[str, float]]:
    """{team: {"semifinal": p, "finals": p, "champion": p}}.

    For KXWNBASEMIFINAL / KXWNBAFINAL / KXWNBA (and their Polymarket twins).
    Read off the SAME win matrix as the win-total ladders and standings, so a
    team's title odds can never contradict its own seed or win distribution --
    the reasoning-vs-pricing split this repo has hit repeatedly.

    Seeding ties are broken at random per trial rather than shared, unlike
    standings_probs. A bracket needs ONE concrete ordering to play out, so a
    fractional seed is not representable; randomising is unbiased across trials
    where sharing is impossible.
    """
    _counts, wins, teams = simulate(trials=trials, games=games, _with_wins=True)
    return _bracket_from_wins(wins, teams, trials=trials)


def _bracket_from_wins(wins, teams, trials: int) -> dict[str, dict[str, float]]:
    """Bracket probabilities from an already-computed win matrix -- the shared
    implementation so warm() and bracket_probs() can never diverge."""
    import random

    if wins is None or not teams or len(teams) < PLAYOFF_FIELD_SIZE:
        return {}

    rng = random.Random(20260806)
    tally = {t: {"semifinal": 0, "finals": 0, "champion": 0} for t in teams}
    # Play at most `trials` of the available seasons -- warm() runs the ladders
    # at 4,000 but the bracket at fewer, and the first N rows of a Monte Carlo
    # are as unbiased as any other N.
    n_trials = min(int(trials), wins.shape[0])

    for i in range(n_trials):
        row = wins[i]
        # Random jitter breaks ties without favouring whichever team sorts first.
        order = sorted(range(len(teams)), key=lambda j: (-int(row[j]), rng.random()))
        seeds = [teams[j] for j in order[:PLAYOFF_FIELD_SIZE]]

        survivors = []
        for hi_i, lo_i in _ROUND1_PAIRS:
            w = _series_winner(seeds[hi_i], seeds[lo_i], _SERIES_VENUES["round1"], rng)
            survivors.append(w)
        for t in survivors:
            tally[t]["semifinal"] += 1

        # RESEED: best remaining plays worst remaining (see the block comment).
        survivors.sort(key=lambda t: seeds.index(t))
        finalists = [
            _series_winner(survivors[0], survivors[3], _SERIES_VENUES["semifinal"], rng),
            _series_winner(survivors[1], survivors[2], _SERIES_VENUES["semifinal"], rng),
        ]
        for t in finalists:
            tally[t]["finals"] += 1

        finalists.sort(key=lambda t: seeds.index(t))
        champ = _series_winner(finalists[0], finalists[1], _SERIES_VENUES["finals"], rng)
        tally[champ]["champion"] += 1

    return {
        t: {k: round(v / n_trials, 4) for k, v in d.items()}
        for t, d in tally.items()
    }


def prob_wins_at_least(dist: dict[int, int] | None, threshold: float, trials: int) -> float | None:
    """P(team finishes with at least `threshold` wins). Kalshi states these with
    an INTEGER floor_strike (20 means 20+), the same convention as CFB's win
    ladders and unlike the soccer points ladders' N-0.5."""
    if not dist or not trials:
        return None
    import math
    cutoff = math.ceil(threshold)
    return sum(c for w, c in dist.items() if w >= cutoff) / trials


def warm(trials: int = 4000) -> None:
    """Recompute + cache off the request path -- the season fetch is one ESPN
    call per day across the season.

    A FAILED run must not latch. Caching an empty result under the normal _TTL
    pins every win-total row to "Season simulation not warm yet" for a full hour
    even though the next attempt would succeed -- observed live 2026-08-03: the
    sim stayed cold for 15+ minutes while running warm() by hand in the same
    checkout produced 15 teams and exact win conservation. The failure modes
    here are transient by nature (the 168-call season fetch races 11 other
    pollers at startup, and simulate() deliberately returns {} rather than a
    fabricated distribution when too few games rate), so a short retry window
    is right: keep good results for the full hour, retry a bad one on the next
    poller cycle."""
    now = time.time()
    with _lock:
        hit = _cache.get("dist")
        # hit[1] is the distribution -- an empty one means the last attempt
        # failed, so it expires after _FAILURE_TTL instead of _TTL.
        if hit and now - hit[0] < (_TTL if hit[1] else _FAILURE_TTL):
            return
    try:
        # Load the Elo cache first -- it starts empty, and without this every
        # remaining game rates None. run_full_refresh_wnba happens to call
        # refresh_wnba_ratings beforehand, but relying on caller order is what
        # made a cold sim return certainties (see the guard in simulate).
        elo_service_wnba.refresh_ratings()
        # ONE simulation feeds both outputs. Calling simulate() again for the
        # standings would be a second, independent season -- a team's #1-seed
        # probability could then contradict its own win distribution, and it
        # would also re-run the sim on every request.
        dist, wins, teams = simulate(trials=trials, _with_wins=True)
        standings = _standings_from_wins(wins, teams)
        # Bracket off the SAME win matrix, for the same reason the standings
        # are: a separate simulate() call would be a different season, and a
        # team's title odds could then contradict its own seed probability.
        # Fewer trials than the ladders because each one plays up to 19 extra
        # series games in Python -- and a title price is not read to four
        # decimals the way a win-total threshold is.
        bracket = _bracket_from_wins(wins, teams, trials=_BRACKET_TRIALS)
    except Exception:
        log.exception("wnba season sim failed")
        dist, standings, bracket = {}, {}, {}
    with _lock:
        _cache["dist"] = (now, dist, trials)
        _cache["standings"] = standings
        _cache["bracket"] = bracket
    log.info("wnba season sim: %d teams over %d trials", len(dist), trials)


def get_standings() -> dict[str, dict[str, float]]:
    """Cached {team: {"one_seed": p, "playoff": p}} from the last warm()."""
    with _lock:
        return _cache.get("standings") or {}


def get_bracket() -> dict[str, dict[str, float]]:
    """Cached {team: {"semifinal": p, "finals": p, "champion": p}} from warm()."""
    with _lock:
        return _cache.get("bracket") or {}


def get() -> tuple[dict[str, dict[int, int]], int]:
    with _lock:
        hit = _cache.get("dist")
    return (hit[1], hit[2]) if hit else ({}, 0)
