"""In-process cache of current MLB Elo ratings (team + starting-pitcher
blend) -- parallel to elo_service_nba.py, sourced from the DB (MlbGame rows
already ingested by poller_mlb.py) rather than re-fetching from MLB Stats
API on every cycle.

Same "not validated to beat the market" status as NFL/NBA's Elo -- see
backtest_moneyline_mlb.py's docstring for why this can't be tested against a
real market yet (no free historical MLB odds source found). The team-Elo +
pitcher-blend combination itself IS validated against real historical
outcomes (see elo_mlb.py/pitcher_ratings_mlb.py's own docstrings) -- what's
unvalidated is specifically "beats the market," the same gap every other
sport in this app has.
"""
import datetime
import logging

from app.db.database import SessionLocal
from app.db.models import MlbGame
from app.models.baseline.elo_mlb import EloState, HOME_FIELD_ADV, NEUTRAL_SITE_HOME_FIELD_ADV, win_prob, update_ratings
from app.models.pitcher_ratings_mlb import PitcherRatingCache

log = logging.getLogger("elo_service_mlb")

_cache: dict = {"state": None}
_pitcher_cache = PitcherRatingCache()


def _season_start(season: int) -> datetime.date:
    """Same generous, cheap-to-over-cover approximation this app already
    uses for other sports' window boundaries (e.g. poller_nba.py's Summer
    League window) -- not exact opening day, just early enough to include
    every real game."""
    return datetime.date(season, 3, 1)


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
            for g in session.query(MlbGame).filter(MlbGame.game_type.in_(("R", "F", "D", "L", "W"))).all()
        ]
    finally:
        session.close()
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    state = EloState()
    for g in games:
        state.start_season_if_new(g["season"])
        if g.get("home_score") is not None and g.get("away_score") is not None:
            adv = NEUTRAL_SITE_HOME_FIELD_ADV if g.get("location") == "Neutral" else HOME_FIELD_ADV
            update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], adv)
    _cache["state"] = state
    log.info("mlb elo ratings refreshed: %d teams rated", len(state.ratings))


def get_elo_diff(
    home_team: str, away_team: str, season: int, location: str | None = None,
    home_pitcher_id=None, away_pitcher_id=None,
) -> float | None:
    """The home-perspective rating-diff-plus-home-field-plus-pitcher-blend
    quantity game_lines_mlb.py's margin model needs (same quantity
    win_prob's `diff` computes internally) -- mirrors elo_service_nba.py's
    identical helper. 0.0 pitcher adjustment when either starter's stats
    aren't available yet, same graceful degrade as get_home_win_prob.

    None if either team is unrated -- guarding here also covers
    get_home_win_prob, which computes its probability from this diff."""
    state = _cache.get("state")
    if state is None:
        return None
    if not (is_rated(home_team) and is_rated(away_team)):
        return None
    home_r = state.get(home_team)
    away_r = state.get(away_team)
    adv = NEUTRAL_SITE_HOME_FIELD_ADV if location == "Neutral" else HOME_FIELD_ADV
    pitcher_adj = _pitcher_cache.get_adjustment(season, _season_start(season), home_pitcher_id, away_pitcher_id)
    return (home_r + pitcher_adj + adv) - away_r


def get_home_win_prob(
    home_team: str, away_team: str, season: int, location: str | None = None,
    home_pitcher_id=None, away_pitcher_id=None,
) -> float | None:
    """Team Elo blended with the starting-pitcher signal, when both
    starters' current-season stats are available and pass the innings-
    pitched floor (see pitcher_ratings_mlb.py) -- 0.0 pitcher adjustment
    otherwise, same as walk-forward's use_mov-style graceful degrade."""
    state = _cache.get("state")
    if state is None:
        return None
    away_r = state.get(away_team)
    diff = get_elo_diff(home_team, away_team, season, location, home_pitcher_id, away_pitcher_id)
    if diff is None:
        return None
    # diff already has home_field_adv (and the pitcher blend) folded in via
    # get_elo_diff -- pass home_field_adv=0 here so win_prob doesn't add it
    # a second time.
    return win_prob(diff + away_r, away_r, home_field_adv=0.0)


def get_combined_era(season: int, home_pitcher_id=None, away_pitcher_id=None) -> float | None:
    """Thin wrapper around PitcherRatingCache.get_combined_era -- see its own
    docstring. Used by the RFI (run-in-1st-inning) model."""
    return _pitcher_cache.get_combined_era(season, _season_start(season), home_pitcher_id, away_pitcher_id)


def get_combined_kbb(season: int, home_pitcher_id=None, away_pitcher_id=None) -> float | None:
    """Both starters' current-season K-BB%, or None. Used by the game-total
    model (#199) -- see game_lines_mlb.expected_total. Same shape and same
    "unknown = no adjustment" contract as get_combined_era above."""
    return _pitcher_cache.get_combined_kbb(season, _season_start(season), home_pitcher_id, away_pitcher_id)


def is_rated(team: str) -> bool:
    """Whether this team string exists in the rating history at all -- see
    elo_service.py::is_rated for why a 1500 fallback is a fabrication at
    scoring time rather than a neutral prior."""
    state = _cache.get("state")
    return bool(state) and team in state.ratings


def get_team_rating(team: str) -> float | None:
    state = _cache.get("state")
    if state is None or not is_rated(team):
        return None
    return state.get(team)
