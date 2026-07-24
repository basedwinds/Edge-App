"""Free preseason schedule, pulled from ESPN's scoreboard endpoint (already
used for injuries/standings -- app/clients/espn_client.py) since nflverse's
games.csv (the source for every other NflGame row in this app) publishes
ZERO preseason games, ever -- confirmed by checking every distinct
`game_type` value in the cached dataset (REG/WC/DIV/CON/SB only). Preseason
is the ONLY NFL market inventory Kalshi/Polymarket list this time of year
(regular season doesn't start until September), so without this, ~76% of
live tracked markets (100/132, confirmed 2026-07-14) were showing up
completely unmatched -- no game context, no model comparison at all.

Deliberately NOT fed into the Elo baseline (see elo_service.py's game_type
filter and the explicit model_prob=None handling in api/routers/markets.py)
-- preseason results are dominated by which backups/starters a coach
decides to play that week, not real team strength, so a regular-season Elo
rating would produce a confident-looking but likely-meaningless number here.

Returns the same shape market_catalog.upsert_nfl_games already expects
(every optional nflverse field just comes back None/missing, which that
function already handles via .get()).
"""
import httpx

from app.clients.espn_client import ESPN_TO_NFLVERSE_ABBR

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
PRESEASON_WEEKS = (1, 2, 3, 4)  # Hall of Fame Game (week 1, single game) + 3 full weeks


def _to_nflverse_abbr(espn_abbr: str) -> str:
    return ESPN_TO_NFLVERSE_ABBR.get(espn_abbr, espn_abbr)


def _score(competitor: dict, completed: bool) -> int | None:
    """ESPN returns "0" as a placeholder score for games that haven't been
    played yet, not null/absent -- trusting it without the event's own
    status.type.completed flag would make every unplayed preseason game look
    like a 0-0 final, which downstream code (e.g. poller.py's "already
    played, skip" check) reads as a real final score."""
    if not completed:
        return None
    raw = competitor.get("score")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fetch_preseason_games(season: int) -> list[dict]:
    games = []
    for week in PRESEASON_WEEKS:
        resp = httpx.get(SCOREBOARD_URL, params={"seasontype": 1, "week": week, "dates": season}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()

        for event in data.get("events", []):
            comp = event["competitions"][0]
            competitors = {c.get("homeAway"): c for c in comp.get("competitors", [])}
            if "home" not in competitors or "away" not in competitors:
                continue
            home, away = competitors["home"], competitors["away"]
            home_abbr = _to_nflverse_abbr(home["team"]["abbreviation"])
            away_abbr = _to_nflverse_abbr(away["team"]["abbreviation"])
            completed = bool((event.get("status") or {}).get("type", {}).get("completed"))

            iso_dt = comp.get("date") or event.get("date") or ""
            gameday, _, gametime = iso_dt.partition("T")

            games.append(
                {
                    "game_id": f"{season}_PRE{week}_{away_abbr}_{home_abbr}",
                    "season": season,
                    "week": week,
                    "game_type": "PRE",
                    "gameday": gameday,
                    "gametime": gametime.rstrip("Z")[:5] or None,
                    "away_team": away_abbr,
                    "home_team": home_abbr,
                    "away_score": _score(away, completed),
                    "home_score": _score(home, completed),
                    "location": "Neutral" if comp.get("neutralSite") else "Home",
                    "stadium": (comp.get("venue") or {}).get("fullName"),
                }
            )
    return games
