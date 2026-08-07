"""ESPN feed for the two things the MLS playoff model needs that no other
soccer league in this app needs: the live CONFERENCE table, and the real
REMAINING regular-season fixture list.

Why not espn_soccer_client.fetch_standings: that helper is keyed on
STANDINGS_LEAGUE_CODES (the five European leagues) and its docstring says it
returns {} for MLS. That is a limitation of the helper, not of ESPN -- checked
live 2026-08-07, https://site.api.espn.com/apis/v2/sports/soccer/usa.1/standings
returns 200 with both conferences split out (15 teams each) and full points /
goals / rank stats. It is excluded there because the European futures model has
no use for a conference split; this module is where MLS's does.

TWO ESPN QUIRKS THIS MODULE EXISTS TO ABSORB:

1. In ESPN's SOCCER standings, "pointsFor"/"pointsAgainst" are GOALS, and
   "points" is league points. Verified on a real row (Chicago Fire, 9W-2D:
   points=29 == 9*3+2, pointsFor=32, pointsAgainst=23, pointDifferential=9).
   Reading pointsFor as league points would silently rank the whole table by
   goals scored.

2. The scoreboard mixes the postseason into the same usa.1 feed, so "every
   future fixture" is NOT "every remaining regular-season fixture" once the
   bracket is scheduled. event["season"]["slug"] separates them
   ("regular-season" vs "mls-cup", confirmed against a real 2025-12 postseason
   event). The numeric season["type"] is NOT usable for this -- it changes year
   to year (13846 in 2026, 13135 in 2025) while the slug is stable.

The scoreboard also caps at ~100 events per call with no cursor (the same cap
documented in espn_soccer_client), so this sweeps in fixed windows and keeps
going until the fixtures run out rather than guessing a season end date. That
matters in 2026 specifically: the World Cup pushed the MLS regular season out
to 2026-11-08, well past its usual mid-October finish, so any hard-coded cutoff
would silently truncate the schedule.
"""
from __future__ import annotations

import datetime
import logging

from app.clients.base import get_json

log = logging.getLogger("espn_mls_season")

STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/soccer/usa.1/standings"
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard"

EAST = "east"
WEST = "west"

# MLS plays a 34-game regular season. Used only as an integrity check on the
# assembled fixture list -- see playoff_sim_service_mls.
REGULAR_SEASON_GAMES = 34

_WINDOW_DAYS = 14
# Stop after this many consecutive empty windows. Two is enough to ride out a
# genuine fixture-free fortnight (international break) without running the
# sweep to the hard bound every time.
_EMPTY_WINDOWS_TO_STOP = 2
_MAX_WINDOWS = 30


def fetch_conference_table() -> list[dict]:
    """One row per team: raw ESPN display name, conference, league points, goal
    difference, goals for, games played. Team names are returned RAW -- callers
    canonicalize, same contract as elo_service_soccer's rating dict."""
    data = get_json(STANDINGS_URL)
    rows: list[dict] = []
    for group in data.get("children") or []:
        name = group.get("name") or ""
        if "East" in name:
            conference = EAST
        elif "West" in name:
            conference = WEST
        else:
            log.warning("espn mls standings: unrecognized group %r, skipped", name)
            continue
        for entry in ((group.get("standings") or {}).get("entries") or []):
            stats = {s.get("name"): s.get("value") for s in entry.get("stats") or []}
            team = (entry.get("team") or {}).get("displayName")
            if not team:
                continue
            rows.append({
                "team": team,
                "conference": conference,
                "points": int(stats.get("points") or 0),
                # See quirk 1 above: these two are GOALS, not league points.
                "goal_diff": int(stats.get("pointDifferential") or 0),
                "goals_for": int(stats.get("pointsFor") or 0),
                "games_played": int(stats.get("gamesPlayed") or 0),
            })
    return rows


def fetch_remaining_regular_season_fixtures(
    start: datetime.date | None = None,
) -> list[tuple[str, str]]:
    """Every UNPLAYED regular-season fixture from `start` (default today)
    onward, as (home_display_name, away_display_name).

    Sweeps forward in fixed windows until the fixtures genuinely run out rather
    than stopping at an assumed season end -- see module docstring. Filters on
    BOTH status state == "pre" (not yet played) and season slug ==
    "regular-season" (not the playoff bracket)."""
    day = start or datetime.date.today()
    fixtures: list[tuple[str, str]] = []
    seen: set[str] = set()
    empty_streak = 0

    for _ in range(_MAX_WINDOWS):
        hi = day + datetime.timedelta(days=_WINDOW_DAYS)
        try:
            data = get_json(
                f"{SCOREBOARD_URL}?dates={day:%Y%m%d}-{hi:%Y%m%d}&limit=200"
            )
        except Exception:
            log.exception("espn mls scoreboard failed for %s..%s", day, hi)
            break
        events = data.get("events") or []
        if len(events) >= 100:
            # The cap has no cursor, so a full window means fixtures were
            # silently dropped -- loud, because the 34-game check downstream is
            # what would otherwise surface this as a vague shortfall.
            log.warning("espn mls scoreboard: %s..%s returned %d events, at/over the "
                        "~100 cap -- fixtures may be missing", day, hi, len(events))

        found = 0
        for ev in events:
            if (ev.get("season") or {}).get("slug") != "regular-season":
                continue
            comp = (ev.get("competitions") or [{}])[0]
            if ((comp.get("status") or {}).get("type") or {}).get("state") != "pre":
                continue
            ev_id = str(ev.get("id"))
            if ev_id in seen:
                continue
            seen.add(ev_id)
            home = away = None
            for c in comp.get("competitors") or []:
                nm = (c.get("team") or {}).get("displayName")
                if c.get("homeAway") == "home":
                    home = nm
                elif c.get("homeAway") == "away":
                    away = nm
            if home and away:
                fixtures.append((home, away))
                found += 1

        empty_streak = empty_streak + 1 if found == 0 else 0
        if empty_streak >= _EMPTY_WINDOWS_TO_STOP:
            break
        day = hi

    return fixtures
