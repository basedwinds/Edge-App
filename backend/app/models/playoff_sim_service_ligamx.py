"""Cached Liga MX Liguilla results, one per open torneo.

WHY CACHED. playoff_sim_ligamx runs 10k simulations over 18 teams and
precomputes 306 ordered goal grids. That is fast (well under a second) but the
INPUTS are not: the current table is rebuilt from football-data's Liga MX
history, which is a network fetch. The soccer futures route is already the
slowest in the app, so it reads a cached result rather than assembling one per
request -- the same reason playoff_sim_service_mls exists.

TWO TORNEOS, TWO RESULTS. Kalshi lists KXLIGAMX-27APER and KXLIGAMX-27CLA at the
same time and they are separate championships, so this keys results by the
router's group_label ("Liga MX Apertura" / "Liga MX Clausura"). Merging them
would produce a 36-team field that never plays.

THEY NEED DIFFERENT INPUTS, WHICH IS THE WHOLE REASON THIS IS NOT ONE CALL:
  * The Apertura is UNDER WAY, so it is simulated from the points already
    banked plus only its unplayed pairings. Replaying it from 0-0 would price a
    runaway leader level with everyone.
  * The Clausura has NOT KICKED OFF, so an empty table and a full synthetic
    round-robin is correct -- exactly how the European leagues are priced
    pre-season.
A torneo with no played games yet naturally falls into the second case without a
special branch: its table is all zeros and every pairing is still to come.
"""
from __future__ import annotations

import datetime
import logging
import threading

from app.models.baseline import elo_service_soccer
from app.models.playoff_sim_ligamx import (
    LigaMxTeamState, balanced_round_robin, simulate_ligamx_torneo,
)

log = logging.getLogger("playoff_sim_service_ligamx")

N_SIMULATIONS = 10000
_TTL_SECONDS = 3600

_cache: dict[str, dict] = {}
_lock = threading.Lock()

APERTURA = "Liga MX Apertura"
CLAUSURA = "Liga MX Clausura"


def _torneo_of(d: datetime.date) -> str:
    """Liga MX plays Apertura (Jul-Dec) then Clausura (Jan-May). The split is by
    calendar month, which is how the history was segmented when the format was
    derived -- see playoff_sim_ligamx's docstring."""
    return APERTURA if d.month >= 7 else CLAUSURA


def _load_current_torneo(label: str) -> tuple[list[LigaMxTeamState], list[tuple[str, str]]]:
    """(table, remaining fixtures) for the named torneo, from football-data.

    Only games ALREADY PLAYED in the current instance of that torneo count
    toward the table. Every pairing not yet played becomes a remaining fixture,
    with hosting balanced -- a synthetic calendar that is lopsided hands out real
    probability, because the goal model prices home advantage.
    """
    from app.clients import football_data_client

    df = football_data_client.fetch_extra_division("MEX1")
    import pandas as pd

    df = df.copy()
    df["dt"] = pd.to_datetime(df["Date"], errors="coerce", dayfirst=True)
    df = df.dropna(subset=["dt"])
    today = datetime.date.today()
    # WINDOW THE CURRENT INSTANCE EXPLICITLY. Matching on the torneo LABEL alone
    # matches every Apertura back to 2012 and sums them into one table -- a team
    # then shows liguilla=1.000 off fourteen seasons of accumulated points. That
    # is the second time an implausible 1.000 exposed a bad window here, so the
    # bound is now a real date range rather than a recency heuristic.
    if label == APERTURA:
        start = datetime.date(today.year if today.month >= 7 else today.year - 1, 7, 1)
    else:
        start = datetime.date(today.year, 1, 1)
    rows = [r for r in df.itertuples()
            if start <= r.dt.date() <= today and _torneo_of(r.dt.date()) == label]

    # ONLY AN IN-PROGRESS TORNEO CONTRIBUTES A TABLE. This guard exists because
    # the naive "within the last N days" version was WRONG and looked right: on
    # 2026-08-11 the market prices the 2027 Clausura (KXLIGAMX-27CLA), which has
    # not kicked off, but the most recent Clausura in the history is the 2026 one
    # that FINISHED in May. Feeding that in made the sim run a Liguilla off a
    # completed table -- every top team came back at liguilla=1.000, which is the
    # tell. A finished or long-dormant torneo means the one being priced has not
    # started, so the correct input is an empty table and a full round-robin.
    STALE_DAYS = 45
    if rows:
        last = max(r.dt.date() for r in rows)
        if (today - last).days > STALE_DAYS:
            rows = []

    key = elo_service_soccer.canonical_team_key
    pts: dict[str, int] = {}
    gd: dict[str, int] = {}
    gf: dict[str, int] = {}
    played: set[tuple[str, str]] = set()
    for r in rows:
        h, a = key(str(r.HomeTeam)), key(str(r.AwayTeam))
        try:
            fh, fa = int(r.FTHG), int(r.FTAG)
        except Exception:
            continue
        played.add(tuple(sorted((h, a))))
        for t in (h, a):
            pts.setdefault(t, 0); gd.setdefault(t, 0); gf.setdefault(t, 0)
        gf[h] += fh; gf[a] += fa
        gd[h] += fh - fa; gd[a] += fa - fh
        if fh > fa:
            pts[h] += 3
        elif fa > fh:
            pts[a] += 3
        else:
            pts[h] += 1; pts[a] += 1
    return pts, gd, gf, played


def _build(label: str, teams: list[str]):
    """`teams` is the canonical key list from the LIVE MARKET, not from the
    history: the market's field is the authority on who is actually in this
    torneo, and a promoted or relegated club would otherwise be missed."""
    state = elo_service_soccer.get_rating_state("MEX1")
    pts, gd, gf, played = _load_current_torneo(label)
    table = [LigaMxTeamState(team=t, points=pts.get(t, 0), goal_diff=gd.get(t, 0),
                             goals_for=gf.get(t, 0)) for t in teams]
    # Remaining = every pairing of the market's field that has not been played.
    remaining = [(h, a) for h, a in balanced_round_robin(teams)
                 if tuple(sorted((h, a))) not in played]
    log.info("ligamx %s: %d teams, %d games already played, %d remaining",
             label, len(teams), len(played), len(remaining))
    return simulate_ligamx_torneo(state, table, remaining,
                                  n_simulations=N_SIMULATIONS, seed=11)


def get_result(label: str, teams: list[str]):
    """Cached LigaMxPlayoffResult for one torneo, or None if it cannot be built.

    Never raises: a futures route must not 500 because a history fetch failed.
    On failure the previous result is kept if there is one, which is better than
    blanking every Liga MX row on one bad fetch.
    """
    now = datetime.datetime.utcnow()
    with _lock:
        hit = _cache.get(label)
        if hit and (now - hit["at"]).total_seconds() < _TTL_SECONDS:
            return hit["result"]
    try:
        result = _build(label, teams)
    except Exception:
        log.exception("ligamx sim failed for %s; keeping any previous result", label)
        with _lock:
            hit = _cache.get(label)
        return hit["result"] if hit else None
    with _lock:
        _cache[label] = {"at": now, "result": result}
    return result
