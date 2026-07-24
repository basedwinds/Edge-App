"""In-process cache of the season Monte Carlo results, recomputed each poll
cycle -- same pattern as elo_service.py (which this depends on for current
ratings). Not validated to beat the market any more than Elo itself is; the
API layer carries the same model_validated: false honesty label.
"""
import logging

from app.ingestion import nfl_data
from app.models.baseline import elo_service
from app.models.season_sim import run_simulation

log = logging.getLogger("season_sim_service")

_cache: dict = {"results": None}


def refresh():
    state = elo_service._cache.get("state")
    if state is None:
        return

    games = nfl_data.fetch_games()
    reg_games = [g for g in games if g["game_type"] == "REG"]
    if not reg_games:
        return
    season = max(g["season"] for g in reg_games)
    season_games = [g for g in reg_games if g["season"] == season]

    results = run_simulation(state.ratings, season_games)
    _cache["results"] = results
    log.info("season sim refreshed: %d teams, season %d", len(results), season)


def get_results() -> dict[str, dict]:
    return _cache.get("results") or {}
