"""In-process cache of the MLB season Monte Carlo results -- parallel to
season_sim_service_nba.py. Sourced from the DB (MlbGame rows), same
reasoning as elo_service_mlb.py -- no live re-fetch every cycle.

Unlike NBA (schedule not published yet for the target season, a REAL
temporary blocker confirmed live) MLB's season is already in progress right
now (2026-07-17) with a full real schedule already ingested by
poller_mlb.py -- target season is simply the current calendar year, no
month-based "which season are futures actually for" cutoff needed the way
NBA's/NFL's convention requires.
"""
import datetime
import logging

from app.db.database import SessionLocal
from app.db.models import MlbGame
from app.models.baseline import elo_service_mlb
from app.models.season_sim_mlb import run_simulation

log = logging.getLogger("season_sim_service_mlb")

_cache: dict = {"results": None, "season": None}


def refresh():
    state = elo_service_mlb._cache.get("state")
    if state is None:
        return

    target_season = datetime.date.today().year
    session = SessionLocal()
    try:
        reg_rows = session.query(MlbGame).filter(MlbGame.game_type == "R", MlbGame.season == target_season).all()
    finally:
        session.close()
    if not reg_rows or not any(g.home_score is None for g in reg_rows):
        # Real, not a design choice: nothing left to simulate once every REG
        # game has a final score (end of season) or the schedule hasn't
        # appeared yet -- same "no unplayed games, skip rather than produce
        # a degenerate all-known-outcome simulation" guard as NBA's version.
        log.info("season sim mlb: no unplayed REG games for season %d yet, skipping", target_season)
        return

    season_games = [
        {
            "home_team": g.home_team, "away_team": g.away_team,
            "home_score": g.home_score, "away_score": g.away_score,
            "location": g.location,
        }
        for g in reg_rows
    ]

    results = run_simulation(state.ratings, season_games)
    _cache["results"] = results
    _cache["season"] = target_season
    log.info("mlb season sim refreshed: %d teams, season %d", len(results) - 1, target_season)  # -1 for the _LEAGUE entry


def get_results() -> dict[str, dict]:
    return _cache.get("results") or {}
