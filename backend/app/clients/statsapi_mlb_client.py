"""Free, no-auth MLB Stats API client -- confirmed live 2026-07-17
(unauthenticated, no key needed, unlike the initial assumption this needed
verifying). This is the canonical schedule/results/pitcher source for MLB,
playing the role nflverse's games.csv plays for NFL: a single bulk call
returns a full season with no hidden cap (confirmed live: a 2025-03-27..
2025-09-28 range returned all 2,462 real REG games in one request) --
genuinely better than what NBA had to settle for (ESPN's ~100-event
7-day-chunked workaround, see espn_nba_client.py's docstring), so MLB does
NOT need that chunking pattern.

Team abbreviations here (this app's canonical MLB convention, matching
Kalshi's own per-team ticker suffixes -- confirmed live for the two teams
that differ from ESPN's convention: AZ not ARI, CWS not CHW) come from
/teams. Also exposes probablePitcher via the schedule endpoint's `hydrate`
param (confirmed live, days-ahead availability) -- the single biggest
MLB-specific baseline design fact: unlike NFL/NBA, single-game outcomes are
dominated by the starting-pitcher matchup, not just team strength (538's own
public MLB Elo methodology blends team Elo with a separate starting-pitcher
rating for exactly this reason).
"""
from app.clients.base import get_json

BASE = "https://statsapi.mlb.com/api/v1"


def get_teams() -> list[dict]:
    d = get_json(f"{BASE}/teams?sportId=1&activeStatus=Y")
    return d.get("teams", [])


def get_schedule(start_date: str, end_date: str, game_type: str = "R", hydrate_pitchers: bool = True) -> list[dict]:
    """Returns raw per-date `dates[].games[]` entries flattened into one list.
    game_type: "R" (regular season), "F,D,L,W" (postseason rounds), "S" (spring
    training), "A" (all-star). Confirmed live: no cap on date-range width."""
    hydrate = "&hydrate=probablePitcher" if hydrate_pitchers else ""
    url = f"{BASE}/schedule?sportId=1&startDate={start_date}&endDate={end_date}&gameType={game_type}{hydrate}"
    d = get_json(url)
    games = []
    for date_entry in d.get("dates", []):
        games.extend(date_entry.get("games", []))
    return games


def get_pitching_stats_by_date_range(start_date: str, end_date: str, season: int) -> list[dict]:
    """Bulk cumulative pitching stat lines for EVERY pitcher (not just ERA-
    title-qualified ones -- confirmed live that the default `playerPool` is
    "QUALIFIED" and silently returns only ~52 pitchers instead of the real
    ~369+ who started a game; `playerPool=ALL` is required) across
    [start_date, end_date], in ONE request. Used to build a point-in-time
    (no-leakage) starting-pitcher rating snapshot for a given date, without
    the ~369-call-per-season cost of per-pitcher gameLog endpoints."""
    url = (
        f"{BASE}/stats?stats=byDateRange&group=pitching&season={season}&sportId=1"
        f"&limit=2000&playerPool=ALL&startDate={start_date}&endDate={end_date}"
    )
    d = get_json(url)
    stats = d.get("stats", [])
    return stats[0].get("splits", []) if stats else []


def get_boxscore(game_pk: str | int) -> dict:
    """Per-game pitching lines (IP/pitch count per pitcher, in appearance
    order via `teams.{home,away}.pitchers`) -- the lightweight boxscore
    endpoint, not the full play-by-play live feed (confirmed live: ~175KB vs
    ~855KB for the same game, identical pitcher IP/pitches fields). Used to
    derive bullpen workload (relief innings/pitches), which the schedule and
    byDateRange endpoints don't expose -- there's no bulk-across-games
    version of this, so building a season's worth of bullpen data costs one
    call per game."""
    return get_json(f"{BASE}/game/{game_pk}/boxscore")


def get_season_hitting_stats(season: int) -> list[dict]:
    """Bulk CURRENT-season cumulative hitting stat lines for every batter
    (again `playerPool=ALL`, not the default "QUALIFIED" -- same trap as
    pitching stats). Used as the value proxy for injury_rules_mlb.py's
    position-player severity multiplier (OPS), same role NBA's per-athlete
    PPG lookup plays -- but MLB's bulk endpoint returns every batter in ONE
    call, so unlike NBA's per-player-request pattern this can just be
    fetched whole and cached, no need to scope to only the injured set."""
    url = f"{BASE}/stats?stats=season&group=hitting&season={season}&sportId=1&limit=2000&playerPool=ALL"
    d = get_json(url)
    stats = d.get("stats", [])
    return stats[0].get("splits", []) if stats else []
