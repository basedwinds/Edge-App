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

_TTL = 3600
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


def simulate(trials: int = 4000, games: list[dict] | None = None) -> dict[str, dict[int, int]]:
    """{team: {win count: how many simulated seasons ended there}}.

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
        return {}

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
        return {}

    counts: dict[str, dict[int, int]] = {t: {} for t in teams}
    if not probs:
        # Season genuinely complete: every game has a real result, so a point mass
        # at the final win count is correct rather than a fabricated certainty.
        for t in teams:
            counts[t][int(banked[idx[t]])] = trials
        return counts

    p_arr = np.array(probs)
    hi = np.array([x[0] for x in pair])
    ai = np.array([x[1] for x in pair])
    rng = np.random.default_rng()

    wins = np.tile(banked, (trials, 1)).astype(np.int32)
    done = 0
    while done < trials:
        n = min(500, trials - done)
        hw = rng.random((n, len(p_arr))) < p_arr
        np.add.at(wins[done:done + n], (slice(None), hi), hw)
        np.add.at(wins[done:done + n], (slice(None), ai), ~hw)
        done += n

    for t in teams:
        vals, cnt = np.unique(wins[:, idx[t]], return_counts=True)
        counts[t] = {int(v): int(c) for v, c in zip(vals, cnt)}
    return counts


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
    call per day across the season."""
    now = time.time()
    with _lock:
        hit = _cache.get("dist")
        if hit and now - hit[0] < _TTL:
            return
    try:
        # Load the Elo cache first -- it starts empty, and without this every
        # remaining game rates None. run_full_refresh_wnba happens to call
        # refresh_wnba_ratings beforehand, but relying on caller order is what
        # made a cold sim return certainties (see the guard in simulate).
        elo_service_wnba.refresh_ratings()
        dist = simulate(trials=trials)
    except Exception:
        log.exception("wnba season sim failed")
        dist = {}
    with _lock:
        _cache["dist"] = (now, dist, trials)
    log.info("wnba season sim: %d teams over %d trials", len(dist), trials)


def get() -> tuple[dict[str, dict[int, int]], int]:
    with _lock:
        hit = _cache.get("dist")
    return (hit[1], hit[2]) if hit else ({}, 0)
