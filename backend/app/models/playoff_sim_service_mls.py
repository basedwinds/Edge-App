"""In-process cache of the MLS Cup playoff Monte Carlo -- same shape as
season_sim_service_mlb / season_sim_service_nba (module-level _cache, a
refresh() the scheduler calls, a getter the router calls).

This one caches harder than its siblings do, and for a specific reason: unlike
the DB-sourced MLB/NBA services, assembling this model's inputs costs a live
ESPN standings call plus a swept sequence of scoreboard calls. Doing that
per-request would put ~10 network round trips in front of every futures page
load. The simulation itself is cheap (10,000 seasons in ~3s), so it runs at
refresh time and the router only ever reads a dict.

n_simulations is 10,000 here rather than the 3,000 the European league sim uses.
That sim is cached per request, so its cost is on the hot path; this one is not.
And it matters more here: a bracket spreads probability over 30 teams, so real
contenders land near 2-3%, where 3,000 samples carry roughly +/-0.3pp of Monte
Carlo noise -- not negligible against a 10pp edge gate that would be deciding
whether to stake real money on that number.

KNOWN LIMITATION, deliberately not fixed here: once the bracket is actually
under way, this re-simulates it from the seeds every time rather than honouring
results already in. Until the regular season ends (2026-11-08) that branch is
unreachable, and pricing a partially-played bracket needs the per-series state,
which is a separate piece of work from the seeding model this is.
"""
from __future__ import annotations

import datetime
import logging

from app.clients import espn_mls_season
from app.clients.espn_mls_season import REGULAR_SEASON_GAMES
from app.ingestion.market_matcher_soccer import canonical_team_key
from app.models.baseline import elo_service_soccer
from app.models.playoff_sim_mls import MlsTeamState, simulate_mls_postseason

log = logging.getLogger("playoff_sim_service_mls")

N_SIMULATIONS = 10_000

# Minimum gap between real recomputes. THIS EXISTS BECAUSE OF A REAL INCIDENT
# (2026-08-08): refresh() was called from run_full_refresh_soccer, which runs
# every 5 MINUTES. Each call is 10,000 simulations over ~242 remaining fixtures
# plus 10 live ESPN requests -- so the backend sat at 100% of a core
# continuously and hammered a free API ~2,880 times a day, for a league table
# that changes at most once a day. The app showed "updated 42m ago" and never
# advanced because the worker never got out of this loop.
#
# The scheduler now runs this on its own slow job, but the TTL is the actual
# guarantee: any caller, at any frequency, gets a cached result until the data
# could plausibly have changed. Cheap to keep, and it makes the cost of a
# mistaken call site zero instead of catastrophic.
MIN_REFRESH_INTERVAL = datetime.timedelta(hours=6)

# Aggregate missing-fixture budget before this refuses to price at all.
#
# The check is (games played + fixtures still scheduled) == 34 for every team.
# A small shortfall is REAL and benign: MLS postpones matches (weather, cup
# conflicts) and a postponed game can sit unscheduled for weeks, so demanding a
# perfect 34 across all 30 teams would blank the whole market over one rained-out
# fixture. What this is actually guarding against is a TRUNCATED fetch -- the
# scoreboard's ~100-event cap or a sweep that stopped early -- and that failure
# mode is not subtle: it drops a chunk of the calendar, so it shows up as a large
# shortfall spread across many teams, not one or two games. Measured live
# 2026-08-07 the real total was 0 (all 30 teams at exactly 34).
MAX_TOTAL_FIXTURE_SHORTFALL = 10

_cache: dict = {"result": None, "refreshed_at": None, "table": None}


def refresh(force: bool = False):
    last = _cache.get("refreshed_at")
    if not force and last and (datetime.datetime.utcnow() - last) < MIN_REFRESH_INTERVAL:
        return  # still fresh -- see MIN_REFRESH_INTERVAL

    state = elo_service_soccer.get_rating_state("MLS")
    if state is None:
        log.info("mls playoff sim: no MLS rating state yet, skipping")
        return

    try:
        table_rows = espn_mls_season.fetch_conference_table()
        raw_fixtures = espn_mls_season.fetch_remaining_regular_season_fixtures()
    except Exception:
        log.exception("mls playoff sim: ESPN fetch failed, keeping previous result")
        return

    if not table_rows:
        log.warning("mls playoff sim: empty standings, skipping")
        return

    table = [
        MlsTeamState(
            team=canonical_team_key(r["team"]),
            conference=r["conference"],
            points=r["points"],
            goal_diff=r["goal_diff"],
            goals_for=r["goals_for"],
        )
        for r in table_rows
    ]
    known = {t.team for t in table}
    played = {canonical_team_key(r["team"]): r["games_played"] for r in table_rows}

    fixtures: list[tuple[str, str]] = []
    dropped = 0
    for home, away in raw_fixtures:
        h, a = canonical_team_key(home), canonical_team_key(away)
        if h in known and a in known:
            fixtures.append((h, a))
        else:
            dropped += 1
    if dropped:
        # A fixture naming a team that isn't in the standings means the name
        # mapping has drifted -- the exact failure that left La Liga unpriced.
        log.warning("mls playoff sim: %d fixtures dropped, team not in standings", dropped)

    remaining = {t: 0 for t in known}
    for h, a in fixtures:
        remaining[h] += 1
        remaining[a] += 1
    shortfall = sum(max(0, REGULAR_SEASON_GAMES - (played[t] + remaining[t])) for t in known)
    if shortfall > MAX_TOTAL_FIXTURE_SHORTFALL:
        log.error(
            "mls playoff sim: fixture list looks truncated (%d team-games missing vs a "
            "%d-game season, budget %d) -- refusing to price rather than simulate a "
            "short season", shortfall, REGULAR_SEASON_GAMES, MAX_TOTAL_FIXTURE_SHORTFALL,
        )
        return
    if shortfall:
        log.info("mls playoff sim: %d team-games unscheduled (within budget)", shortfall)

    result = simulate_mls_postseason(state, table, fixtures, n_simulations=N_SIMULATIONS)
    if result.unrated_teams:
        log.error("mls playoff sim: unrated teams %s -- not pricing", result.unrated_teams)
        return

    _cache["result"] = result
    _cache["table"] = {t.team: t for t in table}
    _cache["refreshed_at"] = datetime.datetime.utcnow()
    log.info("mls playoff sim: %d sims over %d remaining fixtures, %d teams",
             result.n_simulations, len(fixtures), len(table))


def get_result():
    return _cache.get("result")


def get_table():
    return _cache.get("table") or {}


def refreshed_at():
    return _cache.get("refreshed_at")
