"""Free, no-auth ESPN endpoints for NBA -- confirmed live 2026-07-16 (mirrors
the existing espn_client.py pattern for NFL, kept in its own file rather than
merged into that one since the URLs/quirks are sport-specific and that file's
own functions are hardcoded to NFL, per this project's "parallel modules per
sport" architecture call).

stats.nba.com (what the unofficial `nba_api` package wraps) was tried first
and failed outright (connection refused, not just a 403) -- same class of
problem as this user's other projects hitting Cloudflare-gated sites (see
[[project_cs2_betting_model]]). ESPN's public site API is the same
known-reliable free source already used for NFL injuries/standings/preseason.

ESPN's scoreboard endpoint hard-caps at ~100 events per request regardless of
how wide a `dates=` range is requested (confirmed live: a 30-day December
2024 window returned exactly 100, a wider ~6-month range returned FEWER, not
more) -- nba_data.py chunks historical pulls into 7-day windows to stay
safely under that cap rather than trusting a wide range.
"""
import re

from app.clients.base import get_json

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
ATHLETE_STATS_URL = "https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{id}/stats"

_ATHLETE_ID_RE = re.compile(r"/id/(\d+)/")

# ESPN's own abbreviations are this app's canonical NBA team-code convention
# (no nflverse equivalent exists for NBA). Confirmed live against
# .../nba/teams: all 30 teams accounted for. No cross-source rename map is
# needed yet since nothing else has been matched against ESPN's codes yet --
# add one here (mirroring ESPN_TO_NFLVERSE_ABBR in espn_client.py) the moment
# Kalshi/Polymarket team-name matching (Phase 2) finds a mismatch.
TEAM_ABBREVIATIONS = {
    "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GS",
    "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NO", "NY",
    "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SA", "TOR", "UTAH", "WSH",
}

# ESPN's numeric team IDs, confirmed live 2026-07-16 against .../nba/teams --
# needed for the roster endpoint (fetch_current_coach), which takes ESPN's
# own numeric ID, not the team abbreviation.
TEAM_ESPN_ID = {
    "ATL": "1", "BOS": "2", "BKN": "17", "CHA": "30", "CHI": "4", "CLE": "5",
    "DAL": "6", "DEN": "7", "DET": "8", "GS": "9", "HOU": "10", "IND": "11",
    "LAC": "12", "LAL": "13", "MEM": "29", "MIA": "14", "MIL": "15", "MIN": "16",
    "NO": "3", "NY": "18", "OKC": "25", "ORL": "19", "PHI": "20", "PHX": "21",
    "POR": "22", "SAC": "23", "SA": "24", "TOR": "28", "UTAH": "26", "WSH": "27",
}
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{id}/roster"


def fetch_all_injuries() -> dict[str, list[dict]]:
    """Returns {espn_team_abbr: [ {player_name, position, status, athlete_id}, ... ]}.
    Confirmed live 2026-07-16: only 28 of 30 teams currently listed (the 2
    absent teams simply have no reported injuries right now, not a data
    gap), position granularity is coarser than NFL's (only G/F/C -- no
    PG/SG/SF/PF split), and only "Out"/"Day-To-Day" statuses are in use this
    far before the season (more of NFL's richer vocabulary -- Questionable/
    Doubtful/IR -- may appear once games start counting for real).

    athlete_id is NOT a direct field on this endpoint's athlete object
    (confirmed live) -- parsed out of the athlete's own player-card link URL
    (".../id/{id}/{slug}") instead, used by fetch_player_season_avg_points
    for injury_rules_nba.py's player-value proxy."""
    data = get_json(INJURIES_URL)
    out: dict[str, list[dict]] = {}
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            athlete = inj.get("athlete") or {}
            status = inj.get("status", "")
            team_abbr = (athlete.get("team") or {}).get("abbreviation")
            if not team_abbr:
                continue
            athlete_id = None
            for link in athlete.get("links", []):
                m = _ATHLETE_ID_RE.search(link.get("href", ""))
                if m:
                    athlete_id = m.group(1)
                    break
            out.setdefault(team_abbr, []).append(
                {
                    "player_name": athlete.get("displayName", ""),
                    "position": (athlete.get("position") or {}).get("abbreviation", ""),
                    "status": status,
                    "athlete_id": athlete_id,
                }
            )
    return out


def fetch_player_season_avg_points(athlete_id: str) -> float | None:
    """Returns the player's most recent season's average points per game, or
    None if unavailable (rookie with no NBA games yet, request failure,
    etc. -- an honest "unknown," not a guess). Confirmed live 2026-07-16:
    the `statistics` array is ordered oldest-to-newest season, so the LAST
    entry is the most recent; `labels`/`stats` are parallel arrays with
    "PTS" always last. Deliberately called only for currently-injured
    players (a small, scoped set), not the whole league -- this endpoint is
    one call per player, too expensive to pre-fetch broadly."""
    try:
        data = get_json(ATHLETE_STATS_URL.format(id=athlete_id))
    except Exception:
        return None
    averages = next((c for c in data.get("categories", []) if c.get("name") == "averages"), None)
    if not averages or not averages.get("statistics"):
        return None
    labels = averages.get("labels", [])
    if "PTS" not in labels:
        return None
    pts_idx = labels.index("PTS")
    latest = averages["statistics"][-1]
    try:
        return float(latest["stats"][pts_idx])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def fetch_standings(season: int) -> dict[str, dict]:
    """Returns {espn_team_abbr: {"clincher": str|None}} -- confirmed live
    2026-07-16 that NBA standings expose the same per-team "clincher" stat
    NFL's fetch_standings already uses, just nested one level deeper (inside
    each team entry's `stats` array, not a top-level field)."""
    data = get_json(f"{STANDINGS_URL}?season={season}")
    out: dict[str, dict] = {}
    for conf in data.get("children", []):
        for entry in (conf.get("standings") or {}).get("entries", []):
            team_abbr = (entry.get("team") or {}).get("abbreviation")
            if not team_abbr:
                continue
            clincher = None
            for stat in entry.get("stats", []):
                if stat.get("name") == "clincher":
                    clincher = stat.get("displayValue") or None
                    break
            out[team_abbr] = {"clincher": clincher}
    return out


def fetch_current_coach(team_abbr: str) -> str | None:
    """Returns the team's current head coach full name, or None. Confirmed
    live 2026-07-16: the roster endpoint's top-level `coach` field is a
    single-entry list, {id, firstName, lastName, experience} -- no
    historical per-game coach data exists for NBA the way nflverse publishes
    for NFL, so detecting a CHANGE needs this app's own longitudinal
    snapshot tracking (see coach_rules_nba.py) rather than a one-shot
    historical pull."""
    team_id = TEAM_ESPN_ID.get(team_abbr)
    if team_id is None:
        return None
    data = get_json(ROSTER_URL.format(id=team_id))
    coaches = data.get("coach") or []
    if not coaches:
        return None
    c = coaches[0]
    first, last = c.get("firstName", ""), c.get("lastName", "")
    return f"{first} {last}".strip() or None
