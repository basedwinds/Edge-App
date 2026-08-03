"""Parses ESPN WNBA scoreboard events into canonical game dicts for the DB,
and computes per-team rest days from the resulting schedule -- parallel to
nba_data.py. WNBA seasons are labeled by their single calendar year (May-Oct),
unlike the NBA's ending-year convention.
"""
import datetime

from app.clients import espn_wnba_client

_GAME_TYPE = {1: "PRE", 2: "REG", 3: "POST"}


def _parse_event(e: dict) -> dict | None:
    comp = (e.get("competitions") or [{}])[0]
    cs = comp.get("competitors", [])
    if len(cs) != 2:
        return None
    home = next((c for c in cs if c.get("homeAway") == "home"), None)
    away = next((c for c in cs if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    completed = comp.get("status", {}).get("type", {}).get("completed")
    try:
        hs = int(home["score"]) if completed else None
        as_ = int(away["score"]) if completed else None
    except (KeyError, ValueError, TypeError):
        hs = as_ = None
    date_iso = e.get("date") or ""  # e.g. "2026-07-22T19:00Z"
    season = (e.get("season") or {}).get("year")
    stype = (e.get("season") or {}).get("type")
    if season is None or stype not in _GAME_TYPE:
        return None
    return {
        "id": e["id"],
        "season": int(season),
        "game_type": _GAME_TYPE[stype],
        "gameday": date_iso[:10],
        "gametime": date_iso[11:16] if len(date_iso) >= 16 else None,
        "home_team": home["team"]["abbreviation"],
        "away_team": away["team"]["abbreviation"],
        "home_score": hs,
        "away_score": as_,
        "location": "Neutral" if comp.get("neutralSite") else "Home",
        "arena": (comp.get("venue") or {}).get("fullName"),
    }


def _add_rest_days(games: list[dict]) -> None:
    """Days since each team's previous game, from the schedule itself (no
    extra network call) -- same derivation as nba_data's rest computation."""
    ordered = sorted(games, key=lambda g: (g["gameday"], g["id"]))
    last: dict[str, str] = {}
    for g in ordered:
        for side in ("home", "away"):
            t = g[f"{side}_team"]
            if t in last:
                g[f"{side}_rest"] = (datetime.date.fromisoformat(g["gameday"]) - datetime.date.fromisoformat(last[t])).days
            else:
                g[f"{side}_rest"] = None
        last[g["home_team"]] = g["gameday"]
        last[g["away_team"]] = g["gameday"]


def fetch_games(start: datetime.date, end: datetime.date,
                respect_horizon: bool = True) -> list[dict]:
    events = espn_wnba_client.fetch_scoreboard_events(start, end, respect_horizon=respect_horizon)
    games = [g for g in (_parse_event(e) for e in events) if g is not None]
    _add_rest_days(games)
    return games
