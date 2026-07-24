"""NBA Summer League schedule, pulled from a SEPARATE ESPN league slug
(basketball/nba-summer, confirmed live 2026-07-16) -- NOT part of the main
basketball/nba scoreboard endpoint nba_data.py uses (confirmed: the main
endpoint returns 0 events on real Summer League dates with live Kalshi/
Polymarket markets). Same discovery-shaped gap as this project's NFL
preseason_data.py, and the same reason it exists: right now (mid-July),
Summer League is the ONLY real NBA game inventory either platform lists, so
without this, every live Summer League market would show up unmatched.

Two real quirks caught by checking live data instead of assuming this feed
works like the main one:
  1. This separate endpoint mislabels its own `season.year` as the season's
     STARTING year (2026 for games in July 2026, ahead of the 2026-27
     season) -- the OPPOSITE of the main NBA endpoint's convention (labeled
     by the ENDING year). Handled here by hardcoding game_type="SUMMER" and
     deriving `season` the same way this app's other ending-year-labeled
     data does (season = the year the real following season will END in),
     not trusting this endpoint's own `season.year` field.
  2. `season.slug` here reads "regular-season" even though these are
     exhibition Summer League games -- another reason not to reuse
     nba_data.py's `_SEASON_TYPE_MAP` logic for this feed.

Deliberately NOT fed into the Elo baseline (same reasoning as NFL preseason):
Summer League rosters are backups/two-way/rookie players, not real team
strength.
"""
import httpx

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba-summer/scoreboard"


def _score(competitor: dict, completed: bool) -> int | None:
    if not completed:
        return None
    raw = competitor.get("score")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fetch_games(start_yyyymmdd: str, end_yyyymmdd: str) -> list[dict]:
    """Single date-range call -- unlike nba_data.py's chunking, Summer League
    runs barely 2 weeks/year so this never approaches ESPN's ~100-event cap."""
    resp = httpx.get(SCOREBOARD_URL, params={"dates": f"{start_yyyymmdd}-{end_yyyymmdd}", "limit": 100}, timeout=30.0)
    resp.raise_for_status()
    games = []
    for event in resp.json().get("events", []):
        comp = event["competitions"][0]
        competitors = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in competitors or "away" not in competitors:
            continue
        home, away = competitors["home"], competitors["away"]
        completed = bool((event.get("status") or {}).get("type", {}).get("completed"))

        iso_dt = comp.get("date") or event.get("date") or ""
        gameday, _, gametime = iso_dt.partition("T")

        # This endpoint's season.year is the STARTING year of next season
        # (e.g. 2026 for the 2026-27 season's Summer League) -- this app's
        # ending-year convention for that same season is 2027.
        starting_year = (event.get("season") or {}).get("year")
        season = starting_year + 1 if starting_year else None

        games.append(
            {
                "id": event["id"],
                "season": season,
                "game_type": "SUMMER",
                "gameday": gameday,
                "gametime": gametime.rstrip("Z")[:5] or None,
                "away_team": away["team"]["abbreviation"],
                "home_team": home["team"]["abbreviation"],
                "away_score": _score(away, completed),
                "home_score": _score(home, completed),
                "location": "Neutral" if comp.get("neutralSite") else "Home",  # Summer League is Vegas/SLC, nominal "home" only
                "arena": (comp.get("venue") or {}).get("fullName"),
            }
        )
    return games
