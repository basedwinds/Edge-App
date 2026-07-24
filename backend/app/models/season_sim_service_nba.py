"""In-process cache of the NBA season Monte Carlo results -- parallel to
season_sim_service.py (NFL). Sourced from the DB (NbaGame rows), same
reasoning as elo_service_nba.py -- no live re-fetch every cycle.
"""
import datetime
import logging

from app.db.database import SessionLocal
from app.db.models import NbaGame
from app.models.baseline import elo_service_nba
from app.models.season_sim_nba import run_simulation

log = logging.getLogger("season_sim_service_nba")

_cache: dict = {"results": None, "season": None}


def _target_season(today: datetime.date) -> int:
    """The season whose futures markets are actually live right now -- e.g.
    Kalshi's "KXNBA-27"/Polymarket's "NBA: 2027 Champion" during mid-2026
    means season label 2027 (this app's ending-year convention, see
    nba_data.py). Same month>=7 cutoff as market_matcher_nba.py's own
    season-from-date logic, kept consistent rather than duplicated
    differently. REAL BUG this guards against: naively taking
    max(g.season for g in db) would pick the just-COMPLETED season instead
    (its REG games are all played, so there'd be zero games left to
    simulate -- a degenerate "simulation" that just echoes known results,
    silently mislabeled as next season's futures pricing)."""
    return today.year + 1 if today.month >= 7 else today.year


def refresh():
    state = elo_service_nba._cache.get("state")
    if state is None:
        return

    target_season = _target_season(datetime.date.today())
    session = SessionLocal()
    try:
        reg_rows = session.query(NbaGame).filter(NbaGame.game_type == "REG", NbaGame.season == target_season).all()
    finally:
        session.close()
    if not reg_rows or not any(g.home_score is None for g in reg_rows):
        # REAL, temporary blocker: the target season's schedule isn't
        # published by ESPN yet (confirmed live 2026-07-16) -- see
        # season_sim_nba.py's module docstring. Nothing left to simulate
        # (either no rows at all, or the season somehow already finished)
        # until a real, partially-unplayed schedule appears.
        log.info("season sim nba: no unplayed REG games for season %d yet, skipping", target_season)
        return

    season_games = [
        {
            "home_team": g.home_team, "away_team": g.away_team,
            "home_score": g.home_score, "away_score": g.away_score,
            "location": g.location, "home_rest": g.home_rest, "away_rest": g.away_rest,
        }
        for g in reg_rows
    ]

    results = run_simulation(state.ratings, season_games)
    _cache["results"] = results
    _cache["season"] = target_season
    log.info("nba season sim refreshed: %d teams, season %d", len(results) - 1, target_season)  # -1 for the _LEAGUE entry


def get_results() -> dict[str, dict]:
    return _cache.get("results") or {}
