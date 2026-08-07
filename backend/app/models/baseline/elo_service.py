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


def is_rated(team: str) -> bool:
    """Has this exact team string ever appeared in the rating history?

    EloState.get() falls back to BASE_RATING (1500) for anything it doesn't
    know, which is correct while TRAINING (a team's first game has to start
    somewhere) and a fabrication at SCORING time -- 1500 then means "never
    heard of them", not "average team", and nothing downstream can tell the
    difference. See the audit note on get_home_win_prob."""
    state = _cache.get("state")
    return bool(state) and team in state.ratings


def get_home_win_prob(home_team: str, away_team: str, location: str | None = None) -> float | None:
    """None if either team is unrated, rather than a number built on a 1500
    stand-in.

    NFL has a fixed 32-team roster and an audit on 2026-08-06 found ZERO
    unrated team references across all 65 scheduled games with active markets,
    so this guard changes nothing today -- it is here because the only way to
    reach it is a NAME-RESOLUTION break between the schedule source and the
    rating source, and that failure is silent by construction. The same
    fabrication reached the UI in MMA (two unrated debutants priced at exactly
    0.500, drawing a real $10 stake off a phantom +20.5pp edge) and in soccer
    it is live right now, where La Liga sides arrive as "Real Betis" while the
    ratings are keyed "betis". A wrong number is worse than no number."""
    state = _cache.get("state")
    if state is None:
        return None
    if not (is_rated(home_team) and is_rated(away_team)):
        return None
    home_r = state.get(home_team)
    away_r = state.get(away_team)
    hfa = effective_home_field_adv(home_team, location)
    return win_prob(home_r, away_r, hfa)


def get_team_rating(team: str) -> float | None:
    """Raw current Elo rating, no home-field adjustment -- used as a plain
    "how strong is this team right now" yardstick (see schedule_spot_rules.py)
    rather than a matchup win probability. None for an unrated team (see
    is_rated) so a caller can't mistake 1500-as-unknown for 1500-as-average."""
    state = _cache.get("state")
    if state is None or not is_rated(team):
        return None
    return state.get(team)
