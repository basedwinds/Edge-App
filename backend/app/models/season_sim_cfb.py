"""College-football season Monte Carlo -- the win distribution behind Kalshi's
KXNCAAFWINS ladders ("Will <team> win at least N games this season?", 583 open
markets across 69 teams as of 2026-08-02).

Simulates every REMAINING game on each team's schedule from the Elo win
probability, counts wins per simulated season, and returns a per-team histogram.
Games already played contribute their real result, so the answer sharpens as the
season progresses rather than being a preseason guess forever.

Two CFB-specific points:

* The schedule this needs is the FULL season (late August to early December),
  which is wider than the poller's rolling 90-day window. It therefore fetches
  its own season-wide schedule rather than reading CfbGame, and caches the
  result -- a season schedule changes rarely and the sim is thousands of seasons.

* Only games against RATED opponents are simulated. A team's schedule includes
  FCS opponents ESPN's FBS filter lets through, and elo_cfb would price those as
  exactly league-average (BASE_RATING). Those games are instead counted as a
  near-certain win at FCS_WIN_PROB, which is what they overwhelmingly are, rather
  than as a coin flip that would understate every FBS team's win total.
"""
import datetime
import logging
import threading
import time

import numpy as np

from app.clients import espn_cfb_client
from app.models.baseline import elo_cfb, elo_service_cfb

log = logging.getLogger("season_sim_cfb")

# FBS teams beat FCS opponents about 95% of the time. Used only for games whose
# opponent has no rating -- see module docstring.
FCS_WIN_PROB = 0.95

# A CFB season runs late August to early December (conference title games), plus
# bowls/playoff in Dec-Jan which do NOT count toward these regular-season win
# ladders.
_SEASON_START = (8, 15)
_SEASON_END = (12, 15)

_TTL = 3600
_lock = threading.Lock()
_cache: dict = {}


def _season_bounds(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    year = today.year if today.month >= 7 else today.year - 1
    return datetime.date(year, *_SEASON_START), datetime.date(year, *_SEASON_END)


def _fetch_season_games() -> list[dict]:
    start, end = _season_bounds(datetime.date.today())
    events = espn_cfb_client.fetch_scoreboard_events(start, end)
    return [g for g in (espn_cfb_client.parse_event(e) for e in events) if g]


def simulate(trials: int = 4000, games: list[dict] | None = None) -> dict[str, dict[int, int]]:
    """{team: {win count: how many simulated seasons ended there}}.

    Vectorised over trials: each remaining game is one Bernoulli draw per
    simulated season, so the whole season is a (trials x games) boolean matrix
    rather than a Python loop per season."""
    games = _fetch_season_games() if games is None else games
    if not games:
        return {}

    teams = sorted({g["home_team"] for g in games} | {g["away_team"] for g in games})
    idx = {t: i for i, t in enumerate(teams)}
    banked = np.zeros(len(teams), dtype=np.int32)   # wins already achieved
    probs: list[float] = []                          # P(home wins) per remaining game
    pair: list[tuple[int, int]] = []                 # (home idx, away idx)

    for g in games:
        h, a = g["home_team"], g["away_team"]
        if g.get("home_score") is not None and g.get("away_score") is not None:
            winner = h if g["home_score"] > g["away_score"] else a
            banked[idx[winner]] += 1
            continue
        h_rated = elo_service_cfb.is_rated(h)
        a_rated = elo_service_cfb.is_rated(a)
        if h_rated and a_rated:
            p = elo_service_cfb.get_home_win_prob(h, a, bool(g.get("neutral")))
            if p is None:
                continue
        elif h_rated and not a_rated:
            p = FCS_WIN_PROB          # rated home team vs unrated (FCS) visitor
        elif a_rated and not h_rated:
            p = 1.0 - FCS_WIN_PROB
        else:
            continue                   # neither side rated -- nothing to say
        probs.append(float(p))
        pair.append((idx[h], idx[a]))

    counts: dict[str, dict[int, int]] = {t: {} for t in teams}
    if not probs:
        for t in teams:
            counts[t][int(banked[idx[t]])] = trials
        return counts

    p_arr = np.array(probs)
    home_idx = np.array([x[0] for x in pair])
    away_idx = np.array([x[1] for x in pair])
    rng = np.random.default_rng()

    wins = np.tile(banked, (trials, 1)).astype(np.int32)
    chunk = 500
    done = 0
    while done < trials:
        n = min(chunk, trials - done)
        home_wins = rng.random((n, len(p_arr))) < p_arr        # (n, games)
        # np.add.at handles repeated indices correctly (a team plays many games).
        np.add.at(wins[done:done + n], (slice(None), home_idx), home_wins)
        np.add.at(wins[done:done + n], (slice(None), away_idx), ~home_wins)
        done += n

    for t in teams:
        col = wins[:, idx[t]]
        vals, cnt = np.unique(col, return_counts=True)
        counts[t] = {int(v): int(c) for v, c in zip(vals, cnt)}
    return counts


def prob_wins_at_least(dist: dict[int, int] | None, threshold: float, trials: int) -> float | None:
    """P(team finishes with at least `threshold` wins). Kalshi states these as a
    floor_strike of N for an "N+ wins" market (note: an INTEGER floor here, not
    the N-0.5 the soccer points ladders use), so the comparison is >= ceil()."""
    if not dist or not trials:
        return None
    import math
    cutoff = math.ceil(threshold)
    return sum(c for w, c in dist.items() if w >= cutoff) / trials


def warm(trials: int = 4000) -> None:
    """Recompute + cache. Called off the request path by the poller: the season
    fetch is ~100 ESPN calls, far too slow to run inside a request."""
    now = time.time()
    with _lock:
        hit = _cache.get("dist")
        if hit and now - hit[0] < _TTL:
            return
    try:
        dist = simulate(trials=trials)
    except Exception:
        log.exception("cfb season sim failed")
        dist = {}
    with _lock:
        _cache["dist"] = (now, dist, trials)
    log.info("cfb season sim: %d teams simulated over %d trials", len(dist), trials)


def get() -> tuple[dict[str, dict[int, int]], int]:
    """(distribution, trials). Empty until warmed -- callers leave markets
    unpriced rather than pricing off a cold cache."""
    with _lock:
        hit = _cache.get("dist")
    return (hit[1], hit[2]) if hit else ({}, 0)
