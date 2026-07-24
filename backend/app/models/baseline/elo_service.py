"""In-process cache of current Elo ratings, recomputed each poll cycle.

Elo did NOT beat the historical market in backtesting (see Phase 2 findings --
backend/scripts/backtest_moneyline.py and backtest_moneyline_gbm.py). It's
still surfaced in the UI as a reference estimate, but every API response
carries `model_validated: false` so the frontend can label it honestly rather
than presenting it as a proven trading signal.
"""
import logging

from app.ingestion import nfl_data
from app.models.baseline.elo import EloState, effective_home_field_adv, win_prob, update_ratings

log = logging.getLogger("elo_service")

_cache: dict = {"state": None}


def refresh_ratings():
    games = nfl_data.fetch_games()
    games = [g for g in games if g["game_type"] in ("REG", "POST")]
    games.sort(key=lambda g: (g["season"], g["week"]))

    state = EloState()
    for g in games:
        state.start_season_if_new(g["season"])
        if g.get("home_score") is not None and g.get("away_score") is not None:
            hfa = effective_home_field_adv(g["home_team"], g.get("location"))
            update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], hfa)
    _cache["state"] = state
    log.info("elo ratings refreshed: %d teams rated", len(state.ratings))


def get_home_win_prob(home_team: str, away_team: str, location: str | None = None) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    home_r = state.get(home_team)
    away_r = state.get(away_team)
    hfa = effective_home_field_adv(home_team, location)
    return win_prob(home_r, away_r, hfa)


def get_team_rating(team: str) -> float | None:
    """Raw current Elo rating, no home-field adjustment -- used as a plain
    "how strong is this team right now" yardstick (see schedule_spot_rules.py)
    rather than a matchup win probability."""
    state = _cache.get("state")
    if state is None:
        return None
    return state.get(team)
