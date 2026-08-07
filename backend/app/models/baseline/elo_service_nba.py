"""In-process cache of current NBA Elo ratings, recomputed each poll cycle --
parallel to elo_service.py (NFL), but sourced from the DB (NbaGame rows
already ingested by poller_nba.py) rather than re-fetching from ESPN. NFL's
version can re-fetch live every cycle because nflverse is one cheap CSV
request; NBA's historical puller is a ~4.5-minute, hundreds-of-request
chunked pull (see nba_data.py) that must NOT be repeated every 5 minutes.

Same "not validated to beat the market" status as the NFL Elo -- see
backtest_moneyline_nba.py's docstring for why this can't even be tested
against a real market yet (no free historical NBA odds source found).
"""
import logging

from app.db.database import SessionLocal
from app.db.models import NbaGame
from app.models.baseline.elo_nba import EloState, effective_home_court_adv, win_prob, update_ratings

log = logging.getLogger("elo_service_nba")

_cache: dict = {"state": None}


def refresh_ratings():
    session = SessionLocal()
    try:
        games = [
            {
                "id": g.id, "season": g.season, "game_type": g.game_type, "gameday": g.gameday,
                "home_team": g.home_team, "away_team": g.away_team,
                "home_score": g.home_score, "away_score": g.away_score,
                "location": g.location, "home_rest": g.home_rest, "away_rest": g.away_rest,
            }
            for g in session.query(NbaGame).filter(NbaGame.game_type.in_(("REG", "POST", "PLAYIN"))).all()
        ]
    finally:
        session.close()
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    state = EloState()
    for g in games:
        state.start_season_if_new(g["season"])
        if g.get("home_score") is not None and g.get("away_score") is not None:
            adv = effective_home_court_adv(g["home_team"], g.get("location"), g.get("home_rest"), g.get("away_rest"))
            update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], adv)
    _cache["state"] = state
    log.info("nba elo ratings refreshed: %d teams rated", len(state.ratings))


def is_rated(team: str) -> bool:
    """Whether this team string exists in the rating history at all -- see
    elo_service.py::is_rated for why a 1500 fallback is a fabrication at
    scoring time rather than a neutral prior."""
    state = _cache.get("state")
    return bool(state) and team in state.ratings


def get_home_win_prob(
    home_team: str, away_team: str, location: str | None = None, home_rest: int | None = None, away_rest: int | None = None
) -> float | None:
    state = _cache.get("state")
    if state is None:
        return None
    if not (is_rated(home_team) and is_rated(away_team)):
        return None
    home_r = state.get(home_team)
    away_r = state.get(away_team)
    adv = effective_home_court_adv(home_team, location, home_rest, away_rest)
    return win_prob(home_r, away_r, adv)


def get_team_rating(team: str) -> float | None:
    """Raw rating, no home-court term -- distinct from get_home_win_prob's
    matchup function. Mirrors elo_service.py's NFL equivalent."""
    state = _cache.get("state")
    if state is None or not is_rated(team):
        return None
    return state.get(team)


def get_elo_diff(home_team: str, away_team: str, location: str | None = None, home_rest: int | None = None, away_rest: int | None = None) -> float | None:
    """The home-perspective rating-diff-plus-home-court quantity game_lines_nba.py's
    margin/total model needs (same quantity elo_nba.win_prob's `diff` computes
    internally)."""
    state = _cache.get("state")
    if state is None:
        return None
    if not (is_rated(home_team) and is_rated(away_team)):
        return None
    home_r = state.get(home_team)
    away_r = state.get(away_team)
    adv = effective_home_court_adv(home_team, location, home_rest, away_rest)
    return (home_r + adv) - away_r
