"""NBA schedule/results ingestion, pulled from ESPN's public scoreboard
endpoint via 7-day-chunked date-range queries (see espn_nba_client.py's
docstring for why: no nflverse-equivalent bulk file exists for NBA, and
ESPN's own range param hard-caps at ~100 events per request regardless of
window width -- confirmed live, a wider range returns FEWER events, not
more).

ESPN's season.type codes, confirmed live against real 2024-25 data rather
than assumed (play-in is its own type, NOT lumped into postseason as might be
guessed): 1=preseason, 2=regular-season, 3=post-season, 5=play-in. Kept as
four distinct game_type values (PRE/REG/POST/PLAYIN) rather than collapsing
play-in into POST, since it's a genuinely different competitive context
(win-or-go-home for a single seed, not a best-of-7 series).
"""
import datetime as dt
import time

import httpx

from app.clients.espn_nba_client import SCOREBOARD_URL, TEAM_ABBREVIATIONS

_SEASON_TYPE_MAP = {1: "PRE", 2: "REG", 3: "POST", 5: "PLAYIN"}


def _score(competitor: dict, completed: bool) -> int | None:
    """ESPN returns "0" as a placeholder score for unplayed games, not null
    -- same trap documented in preseason_data.py for NFL. Gate on the
    event's own status.type.completed flag instead."""
    if not completed:
        return None
    raw = competitor.get("score")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_event(event: dict) -> dict | None:
    comp = event["competitions"][0]
    competitors = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    if "home" not in competitors or "away" not in competitors:
        return None
    home, away = competitors["home"], competitors["away"]
    home_abbr = home["team"].get("abbreviation")
    away_abbr = away["team"].get("abbreviation")
    # Not every non-real-team entry is caught by "missing abbreviation" --
    # Rising Stars-style squads ("Team LeBron") have none, but the All-Star
    # GAME itself uses "EAST"/"WEST" as real-looking abbreviation values (17
    # REG-dated games affected, confirmed live 2026-07-16). NBA preseason
    # also includes real games against international club teams (Real
    # Madrid, Chinese/Australian league teams, Team USA Olympic exhibitions
    # -- 84 PRE games affected) that are real games but not meaningful for a
    # 30-team NBA Elo model. Filtering to the real 30-team set catches both
    # categories in one check, rather than an ever-growing blocklist.
    if home_abbr not in TEAM_ABBREVIATIONS or away_abbr not in TEAM_ABBREVIATIONS:
        return None
    completed = bool((event.get("status") or {}).get("type", {}).get("completed"))

    iso_dt = comp.get("date") or event.get("date") or ""
    gameday, _, gametime = iso_dt.partition("T")

    season = event.get("season") or {}
    game_type = _SEASON_TYPE_MAP.get(season.get("type"), "OTHER")

    return {
        "id": event["id"],
        "season": season.get("year"),  # ESPN convention: labeled by the season's ENDING year
        "game_type": game_type,
        "gameday": gameday,
        "gametime": gametime.rstrip("Z")[:5] or None,
        "away_team": away["team"]["abbreviation"],
        "home_team": home["team"]["abbreviation"],
        "away_score": _score(away, completed),
        "home_score": _score(home, completed),
        "location": "Neutral" if comp.get("neutralSite") else "Home",
        "arena": (comp.get("venue") or {}).get("fullName"),
    }


def _fetch_window(start: dt.date, end: dt.date, retries: int = 4) -> list[dict]:
    """A ~500-request multi-season backfill (see build_nba_schedule_cache.py)
    hits an occasional transient 5xx from ESPN's edge (confirmed live: a 502
    surfaced after ~3.5min/~150 clean requests) -- retried with backoff here
    rather than in the shared clients/base.py::get_json helper, since that
    helper only retries 429s/connection errors and immediately re-raises any
    HTTPStatusError, and broadening its retry behavior for every other sport
    is out of scope for this ingestion module."""
    for attempt in range(retries):
        try:
            resp = httpx.get(SCOREBOARD_URL, params={"dates": f"{start:%Y%m%d}-{end:%Y%m%d}", "limit": 200}, timeout=30.0)
            resp.raise_for_status()
            return resp.json().get("events", [])
        except (httpx.HTTPStatusError, httpx.TransportError):
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_games(start: dt.date, end: dt.date) -> list[dict]:
    """Pulls every NBA game (pre/regular/play-in/post-season) whose date
    falls in [start, end], inclusive, chunked into 7-day windows to stay
    under ESPN's per-request cap. Returns raw per-game dicts (NOT yet
    rest-day-annotated -- see compute_rest_days), deduplicated by ESPN's own
    event id across overlapping window boundaries.
    """
    games_by_id: dict[str, dict] = {}
    window_start = start
    while window_start <= end:
        window_end = min(window_start + dt.timedelta(days=6), end)
        for event in _fetch_window(window_start, window_end):
            game = _parse_event(event)
            if game is not None:
                games_by_id[game["id"]] = game
        window_start = window_end + dt.timedelta(days=1)
    return list(games_by_id.values())


def compute_rest_days(games: list[dict]) -> None:
    """Mutates each game dict in place, adding away_rest/home_rest -- whole
    days since that team's previous game of ANY type (preseason/play-in/
    postseason all count, same "any prior game" convention nflverse uses).
    A team's first game in the dataset gets None (no prior game to diff
    against), not a guessed default -- same "unknown, not zero" pattern used
    elsewhere in this app. This is expected to matter MORE for NBA than it
    did for NFL: back-to-back games (0 days rest) are common and are a
    much stronger, better-documented fatigue effect than NFL's weekly gaps.
    """
    last_played: dict[str, dt.date] = {}
    for game in sorted(games, key=lambda g: g["gameday"]):
        game_date = dt.date.fromisoformat(game["gameday"])
        for side, team_key in (("home", "home_team"), ("away", "away_team")):
            team = game[team_key]
            prev = last_played.get(team)
            game[f"{side}_rest"] = (game_date - prev).days if prev is not None else None
            last_played[team] = game_date


def build_team_schedule_index(games: list[dict]) -> dict[tuple[int, str], list[dict]]:
    """Returns {(season, team): [{game_id, gameday, opponent, was_home, won}, ...]}
    sorted chronologically -- used by schedule_spot_rules_nba.py's trap-game/
    letdown-spot lookahead. Adapted from market_matcher.py's NFL
    build_opponent_index, but keyed by chronological ORDER within a team's
    own schedule rather than a shared "week" number -- the NBA's day-to-day
    schedule has no week concept, so "this team's previous/next game" is the
    right translation of NFL's "last week's/next week's opponent," not an
    approximation of it."""
    by_team: dict[tuple[int, str], list[dict]] = {}
    for g in games:
        if g["game_type"] != "REG":
            continue
        season = g["season"]
        home, away = g["home_team"], g["away_team"]
        home_score, away_score = g.get("home_score"), g.get("away_score")
        won_home = won_away = None
        if home_score is not None and away_score is not None:
            won_home = home_score > away_score
            won_away = away_score > home_score
        entry_home = {"game_id": g["id"], "gameday": g["gameday"], "opponent": away, "was_home": True, "won": won_home}
        entry_away = {"game_id": g["id"], "gameday": g["gameday"], "opponent": home, "was_home": False, "won": won_away}
        by_team.setdefault((season, home), []).append(entry_home)
        by_team.setdefault((season, away), []).append(entry_away)
    for key in by_team:
        by_team[key].sort(key=lambda e: e["gameday"])
    return by_team


def get_adjacent_games(schedule_index: dict, season: int, team: str, game_id: str) -> tuple[dict | None, dict | None]:
    """Returns (previous_game, next_game) relative to `game_id` in this
    team's own chronological schedule for `season` -- None for either end
    if there's no prior/next REG game (season boundary), same "no gap to
    guess at" convention as NFL's version."""
    entries = schedule_index.get((season, team), [])
    for i, entry in enumerate(entries):
        if entry["game_id"] == game_id:
            prev_entry = entries[i - 1] if i > 0 else None
            next_entry = entries[i + 1] if i + 1 < len(entries) else None
            return prev_entry, next_entry
    return None, None
