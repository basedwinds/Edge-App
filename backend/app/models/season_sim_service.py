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
    # BOTH bail-outs below used to return SILENTLY, and that cost real time on
    # 2026-08-06: every NFL sim future (division winner, conference champion,
    # stage of elimination, playoff seed, playoff host, 1-seed -- 576 markets)
    # showed unpriced with nothing anywhere saying why. A cold cache and a
    # working-but-not-yet-run cache look identical from the API, so the only way
    # to tell them apart was to reproduce the whole refresh by hand.
    #
    # A silent return is the worst option here precisely because the failure is
    # invisible downstream: get_results() returns {} and every consumer just
    # renders "no projection", which reads like an intentional gap rather than a
    # broken step.
    state = elo_service._cache.get("state")
    if state is None:
        log.warning("season sim skipped: elo ratings cache is empty "
                    "(refresh_ratings must run first)")
        return

    games = nfl_data.fetch_games()
    reg_games = [g for g in games if g["game_type"] == "REG"]
    if not reg_games:
        log.warning("season sim skipped: no REG games in the %d fetched", len(games))
        return
    season = max(g["season"] for g in reg_games)
    season_games = [g for g in reg_games if g["season"] == season]

    results = run_simulation(state.ratings, season_games)
    _cache["results"] = results
    log.info("season sim refreshed: %d teams, season %d", len(results), season)


def get_results() -> dict[str, dict]:
    return _cache.get("results") or {}
