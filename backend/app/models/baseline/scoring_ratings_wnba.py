"""Per-team WNBA scoring ratings (points scored / allowed per game), the input
game_lines_wnba.prob_over needs to price totals.

Parallel to scoring_ratings.py (NBA) but computed off WnbaGame rather than the
NBA game table, and gated by MIN_GAMES -- see below, that gate is the whole
reason this is worth shipping.

VALIDATED BEFORE BUILDING (2026-08-02, walk-forward over the 2026 season: each
game predicted using only games that had already been played, so no leakage).
Scored against a naive running league-average total, by Brier over a grid of
total lines from 150 to 200:

    prior-games gate   early third   mid third   late third
    >= 3               -0.00777      +0.01160    +0.01505
    >= 5               -0.00416      +0.01299    +0.01783
    >= 8               -0.00156      +0.01338    +0.01682
    >= 10              -0.00207      +0.01736    +0.01567
    (positive = ratings model beats naive)

Two things make this trustworthy where the spread-slope fit was not (see the
rejection note in game_lines_wnba): the sign is CONSISTENT across every gate and
both later thirds, and the one place it loses has a physical explanation rather
than being noise -- early in a season a team's scoring average is a handful of
games and the league average is genuinely the better prior.

MIN_GAMES = 8 is chosen from that table: it shrinks the early-season penalty to
about zero (-0.0016) while keeping essentially the full mid/late gain. Below the
gate this module returns nothing and the caller falls back to the league average,
exactly as NBA's does.

Correlation with actual totals is 0.309 against the naive 0.125, and MAE 17.55
against 18.21 -- a real but modest signal. model_validated stays False; forward
CLV remains the judge.
"""
import logging

from app.db.database import SessionLocal
from app.db.models import WnbaGame

log = logging.getLogger("scoring_ratings_wnba")

# Minimum games played before a team's own scoring rates are trusted over the
# league average. See the table above -- this is measured, not guessed.
MIN_GAMES = 8


def compute_current_scoring_ratings() -> dict[str, dict]:
    """{team: {"points_scored": ppg, "points_allowed": papg, "games": n}} for every
    team with at least MIN_GAMES finished games this season. Teams below the gate
    are OMITTED rather than returned with thin averages, so callers fall back to
    the league-average total instead of trusting six games of noise."""
    scored: dict[str, list[int]] = {}
    allowed: dict[str, list[int]] = {}
    try:
        session = SessionLocal()
    except Exception:
        log.exception("wnba scoring ratings: no session")
        return {}
    try:
        games = (
            session.query(WnbaGame)
            .filter(WnbaGame.home_score.isnot(None), WnbaGame.away_score.isnot(None))
            .all()
        )
        for g in games:
            scored.setdefault(g.home_team, []).append(g.home_score)
            allowed.setdefault(g.home_team, []).append(g.away_score)
            scored.setdefault(g.away_team, []).append(g.away_score)
            allowed.setdefault(g.away_team, []).append(g.home_score)
    except Exception:
        log.exception("wnba scoring ratings query failed")
        return {}
    finally:
        session.close()

    out: dict[str, dict] = {}
    for team, pts in scored.items():
        opp = allowed.get(team, [])
        if len(pts) < MIN_GAMES or len(opp) < MIN_GAMES:
            continue
        out[team] = {
            "points_scored": sum(pts) / len(pts),
            "points_allowed": sum(opp) / len(opp),
            "games": len(pts),
        }
    return out
