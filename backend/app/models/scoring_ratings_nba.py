"""Free per-team trailing points-scored/allowed, used by game_lines_nba.py
to estimate a game's expected total points. Parallel to scoring_ratings.py
(NFL), sourced from the DB (NbaGame rows) rather than a live re-fetch --
same reasoning as elo_service_nba.py.

Checked against real data before building (2026-07-16, n=13,868 REG games
with sufficient trailing history, 2014-2025, within-season only -- no
cross-season leakage of "current form"): the blend reduces residual std
from 22.29 (naive league-mean-only) to 18.64 (game total) / from 13.30 to
11.51 (single-team total) -- a MUCH larger improvement than NFL's own modest
14.14->13.85 (game_lines.py), consistent with NBA scoring being more
pace/rotation-driven and less possession-scarce than NFL, so recent form
carries more real signal. Window size was grid-searched {5,8,10,15,20}
rather than assumed -- 15 was chosen (18.59 std, near the plateau; 20 was
marginally better at 18.585 but not worth the extra games-required lag).
"""
from app.db.database import SessionLocal
from app.db.models import NbaGame

ROLLING_WINDOW = 15
MIN_GAMES_FOR_RATING = 3


def compute_current_scoring_ratings() -> dict[str, dict]:
    """Returns {team: {"points_scored": float, "points_allowed": float}} --
    each team's trailing mean over its last ROLLING_WINDOW played REG games
    THIS SEASON (falls back to fewer games early in the season; team absent
    from the dict if it's played fewer than MIN_GAMES_FOR_RATING games)."""
    session = SessionLocal()
    try:
        games = (
            session.query(NbaGame)
            .filter(NbaGame.game_type == "REG", NbaGame.home_score.isnot(None))
            .all()
        )
    finally:
        session.close()
    if not games:
        return {}
    season = max(g.season for g in games)
    games = [g for g in games if g.season == season]
    games.sort(key=lambda g: g.gameday)

    history: dict[str, list[tuple[int, int]]] = {}
    for g in games:
        history.setdefault(g.home_team, []).append((g.home_score, g.away_score))
        history.setdefault(g.away_team, []).append((g.away_score, g.home_score))

    out: dict[str, dict] = {}
    for team, results in history.items():
        recent = results[-ROLLING_WINDOW:]
        if len(recent) < MIN_GAMES_FOR_RATING:
            continue
        out[team] = {
            "points_scored": sum(r[0] for r in recent) / len(recent),
            "points_allowed": sum(r[1] for r in recent) / len(recent),
        }
    return out
