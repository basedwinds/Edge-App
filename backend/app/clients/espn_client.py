"""Free, no-auth ESPN endpoints for NFL injury reports and standings --
confirmed live 2026-07-14 (both return all 32 teams in one call, no API key
required):
https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries
https://site.api.espn.com/apis/v2/sports/football/nfl/standings?season={year}

ESPN's team abbreviations differ from nflverse's in two spots (confirmed by
cross-checking a live pull): Rams "LAR" -> nflverse "LA", Washington "WSH" ->
nflverse "WAS". Everything else matches, including Jacksonville as "JAX"
(unlike Kalshi, which uses "JAC" -- see market_matcher.py).
"""
from app.clients.base import get_json

INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

ESPN_TO_NFLVERSE_ABBR = {
    "LAR": "LA",
    "WSH": "WAS",
}


def fetch_all_injuries() -> dict[str, list[dict]]:
    """Returns {nflverse_team_abbr: [ {player_name, position, status}, ... ]}"""
    data = get_json(INJURIES_URL)
    out: dict[str, list[dict]] = {}
    for team in data.get("injuries", []):
        for inj in team.get("injuries", []):
            athlete = inj.get("athlete") or {}
            # inj["status"] is a plain string (e.g. "Out"); athlete["status"]
            # is a separate nested dict that mirrors it -- use the string.
            status = inj.get("status", "")
            team_abbr = (athlete.get("team") or {}).get("abbreviation")
            if not team_abbr:
                continue
            team_abbr = ESPN_TO_NFLVERSE_ABBR.get(team_abbr, team_abbr)
            out.setdefault(team_abbr, []).append(
                {
                    "player_name": athlete.get("displayName", ""),
                    "position": (athlete.get("position") or {}).get("abbreviation", ""),
                    "status": status,
                }
            )
    return out


def fetch_standings(season: int) -> dict[str, dict]:
    """Returns {nflverse_team_abbr: {"clincher": str|None, "seed": int|None}}.

    ESPN computes the full tiebreaker tree itself and exposes the result as a
    single "clincher" code per team (confirmed live 2026-07-14 against a
    completed season): "*" = clinched #1 seed (bye), "z" = clinched division,
    "y" = clinched a wildcard/playoff berth, "e" = mathematically eliminated,
    missing/blank = still in contention. Reusing this rather than re-deriving
    NFL's real tiebreaker rules (division record, common games, strength of
    victory, ...) ourselves -- same reasoning as sourcing injuries from ESPN's
    own report instead of parsing box scores.
    """
    data = get_json(f"{STANDINGS_URL}?season={season}")
    out: dict[str, dict] = {}
    for conference in data.get("children", []):
        entries = (conference.get("standings") or {}).get("entries", [])
        for entry in entries:
            abbr = (entry.get("team") or {}).get("abbreviation")
            if not abbr:
                continue
            abbr = ESPN_TO_NFLVERSE_ABBR.get(abbr, abbr)
            stats = {s.get("name"): s.get("displayValue") for s in entry.get("stats", [])}
            clincher = stats.get("clincher")
            seed = stats.get("playoffSeed")
            out[abbr] = {
                "clincher": clincher if clincher and clincher != "-" else None,
                "seed": int(seed) if seed and seed.isdigit() else None,
            }
    return out


def fetch_half_scores(start: str, end: str) -> list[dict]:
    """First-half scores for completed NFL games in a date window.

    Returns [{gameday, home_abbr, away_abbr, home_score_1h, away_score_1h,
    home_score, away_score}], team abbreviations already mapped to nflverse.

    WHY THIS EXISTS: nflverse (this app's NFL schedule/score source) publishes
    only the FINAL score, so the 1H/2H winner markets (KXNFL1H / KXNFL2H) had
    no way to settle. ESPN's scoreboard carries per-quarter `linescores` on
    each competitor -- confirmed live 2026-08-06, and the same pull is what
    measured HALF_TIE_RATE over 856 games.

    The first half is quarters 1+2; the second half is deliberately NOT
    returned, because it is exactly final-minus-first and storing it twice
    invites the two disagreeing. Games without four quarters of linescores
    (in progress, postponed, or a stale record) are skipped rather than
    partially reported.

    `start`/`end` are YYYYMMDD. ESPN caps a scoreboard response, so callers
    should ask week-by-week rather than for a whole season at once -- the same
    truncation that silently cost the soccer pipeline every result after April.
    """
    # get_json takes a URL only (no params kwarg), so the query is inlined.
    data = get_json(f"{SCOREBOARD_URL}?dates={start}-{end}&limit=300")
    out = []
    for event in (data or {}).get("events", []):
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
            continue
        sides = {}
        for c in comp.get("competitors", []):
            lines = c.get("linescores") or []
            if len(lines) < 4:
                sides = {}
                break
            try:
                first_half = int(float(lines[0].get("value") or 0)) + int(float(lines[1].get("value") or 0))
                final = int(c.get("score"))
            except (TypeError, ValueError):
                sides = {}
                break
            abbr = (c.get("team") or {}).get("abbreviation")
            sides[c.get("homeAway")] = (ESPN_TO_NFLVERSE_ABBR.get(abbr, abbr), first_half, final)
        if "home" not in sides or "away" not in sides:
            continue
        date = event.get("date")
        out.append({
            "gameday": date[:10] if date else None,
            "home_abbr": sides["home"][0], "away_abbr": sides["away"][0],
            "home_score_1h": sides["home"][1], "away_score_1h": sides["away"][1],
            "home_score": sides["home"][2], "away_score": sides["away"][2],
        })
    return out
