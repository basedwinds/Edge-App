"""In-process cache of current WNBA Elo ratings, recomputed each poll cycle
from the DB (WnbaGame rows ingested by poller_wnba.py) -- parallel to
elo_service_nba.py. Same not-validated-to-beat-the-market status (see
elo_wnba.py's docstring: market beats this model by 0.008 Brier).
"""
import logging

from app.db.database import SessionLocal
from app.db.models import WnbaGame
from app.models.baseline.elo_wnba import EloState, effective_home_court_adv, update_ratings, win_prob

log = logging.getLogger("elo_service_wnba")

_cache: dict = {"state": None}


def refresh_ratings():
    session = SessionLocal()
    try:
        games = [
            {
                "id": g.id, "season": g.season, "gameday": g.gameday,
                "home_team": g.home_team, "away_team": g.away_team,
                "home_score": g.home_score, "away_score": g.away_score,
                "location": g.location,
            }
            for g in session.query(WnbaGame).filter(WnbaGame.game_type.in_(("REG", "POST"))).all()
        ]
    finally:
        session.close()
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    state = EloState()
    for g in games:
        state.start_season_if_new(g["season"])
        if g.get("home_score") is not None and g.get("away_score") is not None:
            adv = effective_home_court_adv(g["home_team"], g.get("location"))
            update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], adv)
    _cache["state"] = state
    log.info("wnba elo ratings refreshed: %d teams rated", len(state.ratings))


def get_home_win_prob(home_team: str, away_team: str, location: str | None = None) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    adv = effective_home_court_adv(home_team, location)
    return win_prob(state.get(home_team), state.get(away_team), adv)


def get_team_rating(team: str) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    return state.get(team)
