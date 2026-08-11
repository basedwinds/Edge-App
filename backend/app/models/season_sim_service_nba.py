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

# NBA regular season: 30 teams, 82 games each. The floor sits well under 82 so a
# few ESPN gaps cannot silence a real season, while the partial-publication case
# that caused the bug (6-18 games per team) fails it by an order of magnitude.
FULL_SEASON_GAMES = 82
MIN_GAMES_PER_TEAM = 70
MIN_TEAMS = 30


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

    # THE SCHEDULE MUST BE COMPLETE, NOT MERELY PRESENT.
    #
    # The check above only asks whether SOME unplayed rows exist. ESPN publishes
    # the season in pieces, so during the summer it returns a partial schedule --
    # and a partial schedule does not simulate a short season, it simulates a
    # WRONG one, because teams do not get equal numbers of games.
    #
    # Caught live 2026-08-11 with 163 of ~1,230 games published and teams
    # holding between 6 and 18 fixtures. A team with 6 scheduled games cannot
    # out-win a team with 18, so the sim ranked teams BY HOW MANY GAMES ESPN HAD
    # PUBLISHED FOR THEM rather than by how good they are. Five teams came out at
    # worst_record = 1.0000 SIMULTANEOUSLY (SAC, BKN, NO, MIL, CHI) -- the group
    # summed to 20.68 where exactly one team can finish worst -- and four of them
    # were staked at $2.50 each against a market pricing them 0.11-0.225. Those
    # were the largest apparent edges in the entire futures book, +78 to +90pp,
    # and every one was an artifact.
    #
    # Every NBA season future rides on this sim (division_winner,
    # playoff_qualifier, championship, win_total, best/worst record), so the gate
    # belongs here rather than on the one market where it happened to be visible.
    #
    # 82 games per team is the NBA regular season. The floor is set well below it
    # so a handful of ESPN gaps or postponements cannot silence a real season,
    # while 6-18 fails by a mile -- the two cases are three binary orders of
    # magnitude apart, so the exact threshold is not load-bearing.
    per_team: dict[str, int] = {}
    for g in reg_rows:
        per_team[g.home_team] = per_team.get(g.home_team, 0) + 1
        per_team[g.away_team] = per_team.get(g.away_team, 0) + 1
    thinnest = min(per_team.values()) if per_team else 0
    if len(per_team) < MIN_TEAMS or thinnest < MIN_GAMES_PER_TEAM:
        log.warning(
            "season sim nba: schedule for %d is INCOMPLETE (%d teams, thinnest has %d of "
            "%d games) -- refusing to simulate; futures stay unpriced until ESPN finishes "
            "publishing", target_season, len(per_team), thinnest, FULL_SEASON_GAMES,
        )
        _cache["results"] = None
        _cache["season"] = None
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
