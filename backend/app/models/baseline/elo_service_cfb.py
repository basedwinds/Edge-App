"""In-process cache of current college-football Elo ratings -- parallel to
elo_service_wnba.py, with one structural difference that matters.

WNBA/NBA rebuild ratings purely from their DB game rows, which is fine because
those tables hold whole seasons. CFB CANNOT do that: the poller only keeps a
~90-day window (espn_cfb_client.FORWARD_DAYS), so the DB never contains prior
seasons -- and elo_cfb ships SEASON_REGRESSION = 0.0 precisely because college
ratings are supposed to CARRY FORWARD year over year. Replaying DB rows alone
would start every team at 1500 in week 1 and throw away the exact signal the
constants were validated on.

So the replay is seeded from data/cfb_game_cache.json (4,836 FBS games,
2021-2025 -- the same file the constants were derived from) and then continues
through the current season's DB rows. Games already present in the cache are
skipped by id when the DB rows are applied, so a game cannot be counted twice
if the window overlaps the cache's tail.
"""
import json
import logging
from pathlib import Path

from app.db.database import SessionLocal
from app.db.models import CfbGame
from app.models.baseline.elo_cfb import (
    EloState,
    effective_home_field_adv,
    update_ratings,
    win_prob,
)

log = logging.getLogger("elo_service_cfb")

# parents[4] == the repo root (this file is backend/app/models/baseline/), so the
# cache resolves to <repo>/data. parents[3] would point at backend/data, which
# does not exist -- same path racing_ratings.py uses.
_DATA_DIR = Path(__file__).resolve().parents[4] / "data"
_CACHE_FILE = _DATA_DIR / "cfb_game_cache.json"

_cache: dict = {"state": None}


def _historical_games() -> list[dict]:
    """Prior-season games from the derivation cache. Empty (with a warning) if
    the file is missing -- the model still runs, it just starts flat, which is
    worth a log line rather than a silent quality drop."""
    try:
        raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.warning("cfb elo: historical cache unreadable at %s -- ratings will start flat", _CACHE_FILE)
        return []
    rows = list(raw.values()) if isinstance(raw, dict) else list(raw)
    out = []
    for g in rows:
        if g.get("home_score") is None or g.get("away_score") is None:
            continue
        out.append({
            "id": str(g.get("id")),
            "season": int(g["season"]),
            "gameday": g["date"],
            "home_team": g["home_abbr"],
            "away_team": g["away_abbr"],
            "home_score": g["home_score"],
            "away_score": g["away_score"],
            "neutral": 1 if g.get("neutral") else 0,
        })
    return out


def _db_games() -> list[dict]:
    session = SessionLocal()
    try:
        return [
            {
                "id": g.id, "season": g.season, "gameday": g.gameday,
                "home_team": g.home_team, "away_team": g.away_team,
                "home_score": g.home_score, "away_score": g.away_score,
                "neutral": g.neutral or 0,
            }
            for g in session.query(CfbGame).filter(CfbGame.game_type.in_(("REG", "POST"))).all()
        ]
    finally:
        session.close()


def refresh_ratings():
    hist = _historical_games()
    seen = {g["id"] for g in hist}
    # De-dupe by ESPN event id: the poller's back-window can overlap the cache's
    # tail, and replaying a game twice would double its rating impact.
    live = [g for g in _db_games() if g["id"] not in seen]
    games = hist + live
    games.sort(key=lambda g: (g["season"], g["gameday"], g["id"]))

    state = EloState()
    applied = 0
    for g in games:
        state.start_season_if_new(g["season"])
        if g.get("home_score") is not None and g.get("away_score") is not None:
            adv = effective_home_field_adv(bool(g.get("neutral")))
            update_ratings(state, g["home_team"], g["away_team"], g["home_score"], g["away_score"], adv)
            applied += 1
    _cache["state"] = state
    log.info(
        "cfb elo ratings refreshed: %d teams rated from %d games (%d historical + %d live)",
        len(state.ratings), applied, len(hist), len(live),
    )


def get_home_win_prob(home_team: str, away_team: str, neutral: bool = False) -> float | None:
    """P(home team wins). None when ratings aren't warm yet -- callers leave the
    market unpriced rather than pricing off a cold cache."""
    state = _cache.get("state")
    if state is None:
        return None
    adv = effective_home_field_adv(neutral)
    return win_prob(state.get(home_team), state.get(away_team), adv)


def rating(team: str) -> float | None:
    state = _cache.get("state")
    return None if state is None else state.get(team)


def is_rated(team: str) -> bool:
    """Whether this team has ever been seen. A team absent from both the cache
    and the DB gets BASE_RATING from EloState.get, which would silently price an
    unknown FCS opponent as league-average -- callers should check this first."""
    state = _cache.get("state")
    return bool(state and team in state.ratings)
