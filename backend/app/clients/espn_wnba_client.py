"""ESPN WNBA schedule client (free public API, same host the NBA/MLB clients
use). Parallel to espn_nba_client.py but far simpler -- WNBA needs only the
schedule/scoreboard for the moneyline Elo build, plus the injuries feed that
backs the WNBA availability adjustment (see injury_rules_wnba.py). No coach or
standings layer is wired for WNBA.
"""
import datetime
import logging
import re

import httpx

from app.clients.base import get_json

log = logging.getLogger("espn_wnba_client")

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
_UA = {"User-Agent": "Mozilla/5.0"}


FORWARD_DAYS = 14


def fetch_scoreboard_events(start: datetime.date, end: datetime.date,
                            respect_horizon: bool = True) -> list[dict]:
    """Raw ESPN event dicts across [start, end], one scoreboard call per day.
    WNBA plays ~mid-May through mid-Oct; callers pass a season-wide window.

    REAL BUG fixed 2026-08-02: the loop stopped at `datetime.date.today()`, so it
    only ever ingested games up to TODAY and the schedule could never contain an
    UPCOMING game. Kalshi lists a game's markets a day or more ahead, so those
    markets had no game row to link to -- they stayed unlinked, couldn't be priced
    or settled, and showed up as the standing health-check warning "N active WNBA
    Kalshi market(s) with no game/match link". Confirmed live: ESPN's 2026-08-03
    scoreboard returns LV@ATL, SEA@NY and PHX@CHI (exactly the pairings Kalshi was
    pricing) while our table had only TOR@GS that day -- and TOR@GS was there only
    because it's a late tip that falls on Aug 3 in UTC but appears on the Aug 2
    scoreboard.

    Bounded to FORWARD_DAYS ahead rather than the caller's full season `end`: the
    fetch is one HTTP call per day, so honouring a season-wide end would add ~100
    calls per refresh for schedule that barely changes. Two weeks comfortably
    covers the window in which markets get listed."""
    # respect_horizon=False is for the SEASON SIM, which needs the whole
    # remaining schedule. The clamp is right for the poller (one HTTP call per
    # day, and markets only list ~2 weeks out) but silently truncates anything
    # asking a season-wide question: measured 2026-08-02, the clamp left teams
    # with at most 38 of their 44 games, which understates every win total in a
    # way nothing surfaces as an error.
    horizon = (datetime.date.today() + datetime.timedelta(days=FORWARD_DAYS)
               if respect_horizon else end)
    out = []
    with httpx.Client(timeout=30.0, headers=_UA) as client:
        day = start
        while day <= end and day <= horizon:
            try:
                r = client.get(SCOREBOARD, params={"dates": day.strftime("%Y%m%d")})
                if r.status_code == 200:
                    out.extend(r.json().get("events", []))
            except httpx.HTTPError:
                pass
            day += datetime.timedelta(days=1)
    return out


# --- injuries -------------------------------------------------------------
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries"
_ATHLETE_ID_RE = re.compile(r"/id/(\d+)/")


def fetch_all_injuries() -> dict[str, list[dict]]:
    """{espn_team_abbr: [{player_name, position, status, athlete_id}, ...]}.

    Direct sibling of espn_nba_client.fetch_all_injuries -- same endpoint
    shape, wnba path. Confirmed live 2026-08-06: all 15 teams present, 39
    players listed, statuses "Out" and "Day-To-Day", positions G/F/C.

    Unlike college football (6 teams / 7 players across ~136 FBS programs,
    which is why no CFB layer was built), this is real, complete coverage.

    athlete_id is not a direct field -- parsed out of the player-card link,
    same as the NBA version.
    """
    data = get_json(INJURIES_URL)
    out: dict[str, list[dict]] = {}
    for team in (data or {}).get("injuries", []):
        for inj in team.get("injuries", []):
            athlete = inj.get("athlete") or {}
            team_abbr = (athlete.get("team") or {}).get("abbreviation")
            if not team_abbr:
                continue
            athlete_id = None
            for link in athlete.get("links", []):
                m = _ATHLETE_ID_RE.search(link.get("href", ""))
                if m:
                    athlete_id = m.group(1)
                    break
            out.setdefault(team_abbr, []).append({
                "player_name": athlete.get("displayName", ""),
                "position": (athlete.get("position") or {}).get("abbreviation", ""),
                "status": inj.get("status", ""),
                "athlete_id": athlete_id,
            })
    return out


ATHLETE_STATS_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{id}/stats"


def fetch_player_season_avg_minutes(athlete_id: str) -> float | None:
    """Most recent season's minutes per game, or None if unavailable.

    MINUTES, not points, on purpose: injury_rules_wnba's weight was calibrated
    on top-3-BY-MINUTES players, so tiering the live rule on scoring would
    price something other than what was measured.

    NOTE THE DIFFERENCE FROM THE NBA VERSION. espn_nba_client's equivalent
    takes the LAST label because "PTS" is last there. It is NOT last on the
    WNBA endpoint -- confirmed live 2026-08-06, the labels run
    GP, GS, MIN, PTS, OR, DR, ... , FT, FT%, PF -- so this looks the column up
    BY NAME. Copying the positional assumption across would have silently
    returned personal fouls.

    `statistics` is ordered oldest-to-newest season, so the last entry is the
    most recent. Returns None (honest unknown, not a guess) on any failure or
    for a player with no season yet.
    """
    try:
        data = get_json(ATHLETE_STATS_URL.format(id=athlete_id))
    except Exception:
        return None
    averages = next((c for c in (data or {}).get("categories", []) if c.get("name") == "averages"), None)
    if not averages or not averages.get("statistics"):
        return None
    labels = averages.get("labels") or []
    if "MIN" not in labels:
        return None
    stats = (averages["statistics"][-1] or {}).get("stats") or []
    idx = labels.index("MIN")
    if idx >= len(stats):
        return None
    try:
        return float(stats[idx])
    except (TypeError, ValueError):
        return None
