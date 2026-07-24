"""Free nflverse depth-chart feed (confirmed live 2026-07-14, current season
file exists and is updated regularly: depth_charts_{season}.csv, ~330k rows/
season). Used to confirm which injured players are actual starters (pos_rank
== "1"), instead of injury_rules.py's previous "any notable position match"
approximation for non-QB positions.

Cached in-process for a few hours -- this file is large (a few MB, hundreds
of thousands of rows) and depth charts don't change fast enough to justify
re-downloading and re-parsing it every 5-minute poll cycle.
"""
import csv
import datetime
import io

import httpx

DEPTH_CHART_URL_TEMPLATE = "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv"
CACHE_TTL = datetime.timedelta(hours=6)

_cache: dict = {"season": None, "fetched_at": None, "starters_by_team": None, "qb_backup_by_team": None}


def _normalize_name(name: str) -> str:
    return name.lower().replace(".", "").strip()


def _fetch_depth_chart(season: int) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Single download, two derived views: starters_by_team (rank-1 player at
    ANY position, all positions pooled -- used to confirm a notable injury is
    a real starter) and qb_backup_by_team (specifically the rank-2 QB's full
    name, used to scale the "starting QB out" penalty by backup quality)."""
    url = DEPTH_CHART_URL_TEMPLATE.format(season=season)
    resp = httpx.get(url, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)

    latest_dt_by_team: dict[str, str] = {}
    for row in rows:
        team, dt = row["team"], row["dt"]
        if dt > latest_dt_by_team.get(team, ""):
            latest_dt_by_team[team] = dt

    starters: dict[str, set[str]] = {}
    qb_backup: dict[str, str] = {}
    for row in rows:
        if row["dt"] != latest_dt_by_team.get(row["team"]):
            continue
        if row["pos_rank"] == "1":
            starters.setdefault(row["team"], set()).add(_normalize_name(row["player_name"]))
        if row.get("pos_abb") == "QB" and row["pos_rank"] == "2":
            qb_backup[row["team"]] = row["player_name"]
    return starters, qb_backup


def get_current_starters(season: int, fetch_if_missing: bool = True) -> dict[str, set[str]]:
    starters, _ = _get_depth_chart_cached(season, fetch_if_missing)
    return starters


def get_qb_backup(season: int, fetch_if_missing: bool = True) -> dict[str, str]:
    """Returns {nflverse_team_abbr: backup QB full name} for teams with a
    listed QB2 on the depth chart."""
    _, qb_backup = _get_depth_chart_cached(season, fetch_if_missing)
    return qb_backup


def _get_depth_chart_cached(season: int, fetch_if_missing: bool = True) -> tuple[dict[str, set[str]], dict[str, str]]:
    """`fetch_if_missing`: the POLLER leaves this True (it OWNS keeping the
    cache warm). REQUEST handlers pass False so they never block on the ~13s
    external depth-chart fetch -- a cold cache degrades to no depth-chart data
    (the futures model just skips that adjustment for one poll cycle) instead
    of a request that hangs for 13s and stampedes N concurrent fetches."""
    now = datetime.datetime.utcnow()
    if (
        _cache["starters_by_team"] is not None
        and _cache["season"] == season
        and _cache["fetched_at"] is not None
        and now - _cache["fetched_at"] < CACHE_TTL
    ):
        return _cache["starters_by_team"], _cache["qb_backup_by_team"]

    if not fetch_if_missing:
        return {}, {}

    starters, qb_backup = _fetch_depth_chart(season)
    _cache.update(season=season, fetched_at=now, starters_by_team=starters, qb_backup_by_team=qb_backup)
    return starters, qb_backup


SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}

_position_cache: dict = {}


def _fetch_depth_chart_positions(season: int, before_date: str | None = None) -> dict[str, dict[str, str]]:
    """Returns {team: {pos_abb: player_name}} for QB/RB/WR/TE only, used by
    roster_change_rules.py to compare this-season vs last-season starters at
    those positions. `before_date` (ISO "YYYY-MM-DD") restricts to each
    team's latest snapshot AT OR BEFORE that date -- needed because a given
    season's depth-chart file keeps accumulating timestamped snapshots well
    into the FOLLOWING offseason (confirmed live 2026-07-16: the 2025 file's
    latest snapshot was dated March 2026, already reflecting free-agency
    moves for the 2026 season) -- so "get last season's starters" needs an
    explicit end-of-season cutoff, not just "the file's latest row", or it
    would silently pick up the very offseason changes this signal is trying
    to detect."""
    url = DEPTH_CHART_URL_TEMPLATE.format(season=season)
    resp = httpx.get(url, timeout=60.0, follow_redirects=True)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)

    latest_dt_by_team: dict[str, str] = {}
    for row in rows:
        team, dt = row["team"], row["dt"]
        if before_date and dt[:10] > before_date:
            continue
        if dt > latest_dt_by_team.get(team, ""):
            latest_dt_by_team[team] = dt

    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["dt"] != latest_dt_by_team.get(row["team"]):
            continue
        if row.get("pos_abb") not in SKILL_POSITIONS or row["pos_rank"] != "1":
            continue
        out.setdefault(row["team"], {})[row["pos_abb"]] = row["player_name"]
    return out


def get_skill_position_starters(season: int, before_date: str | None = None, fetch_if_missing: bool = True) -> dict[str, dict[str, str]]:
    cache_key = (season, before_date)
    now = datetime.datetime.utcnow()
    entry = _position_cache.get(cache_key)
    if entry is not None and now - entry["fetched_at"] < CACHE_TTL:
        return entry["data"]
    if not fetch_if_missing:
        return {}  # request path: never block on the external fetch (see _get_depth_chart_cached)
    data = _fetch_depth_chart_positions(season, before_date)
    _position_cache[cache_key] = {"fetched_at": now, "data": data}
    return data
