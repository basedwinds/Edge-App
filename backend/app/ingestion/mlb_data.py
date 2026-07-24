"""MLB schedule/results/probable-pitcher ingestion, pulled from
statsapi_mlb_client.py. Parallel to nfl_data.py/nba_data.py, same
"parallel modules per sport" architecture call.

Unlike NBA (no bulk file existed, had to chunk ESPN's scoreboard in 7-day
windows), MLB Stats API's own /schedule endpoint returns a full season in one
call with no hidden cap (confirmed live) -- closer to NFL's nflverse
situation than NBA's. Team IDs come back numeric from /schedule (no
abbreviation field there) -- resolved via a name->abbreviation map built from
/teams, confirmed live to cover all 30 real teams.
"""
import datetime as dt

from app.clients import statsapi_mlb_client

# This app's canonical MLB team-abbreviation convention: MLB Stats API's own
# /teams abbreviations, which also happen to match Kalshi's per-team ticker
# suffixes exactly (confirmed live: KXMLBWINS-CWS, KXMLBWINS-ATH, KXMLB-26-AZ
# all use this convention, not ESPN's). ESPN differs on exactly two teams
# (confirmed live via a real scoreboard pull, not assumed): Diamondbacks
# ("ARI" on ESPN/Polymarket vs "AZ" here) and White Sox ("CHW" on ESPN vs
# "CWS" here) -- same category of cross-source quirk as NFL's JAX/JAC.
ESPN_TO_STATSAPI_ABBR = {"ARI": "AZ", "CHW": "CWS"}


# In-process cache, TTL 24h -- REAL performance bug caught live 2026-07-17:
# market_matcher_mlb.py::match_kalshi_event_ticker calls team_abbreviations()
# once PER MARKET ROW being matched (not once per refresh cycle), which
# without caching meant one real statsapi.mlb.com/teams network call per row
# -- hundreds of calls per refresh cycle, visibly slowing every poll. Not
# hardcoded instead of cached, since that would silently go stale if a team
# relocates/rebrands mid-project (already happened once: Athletics'
# abbreviation is "ATH", not the historical "OAK") -- a day-old cache is a
# fine tradeoff for data that changes at most once a year.
_team_abbr_cache: dict = {"map": None, "fetched_at": None}
_TEAM_ABBR_CACHE_TTL_SECONDS = 24 * 3600


def _team_abbr_map() -> dict[int, str]:
    """team_id -> canonical abbreviation, cached (see module-level note
    above) rather than hardcoded."""
    now = dt.datetime.utcnow()
    fetched_at = _team_abbr_cache["fetched_at"]
    if fetched_at is None or (now - fetched_at).total_seconds() > _TEAM_ABBR_CACHE_TTL_SECONDS:
        _team_abbr_cache["map"] = {t["id"]: t["abbreviation"] for t in statsapi_mlb_client.get_teams()}
        _team_abbr_cache["fetched_at"] = now
    return _team_abbr_cache["map"]


def team_abbreviations() -> set[str]:
    return set(_team_abbr_map().values())


def _parse_game(g: dict, abbr_by_id: dict[int, str]) -> dict | None:
    teams = g.get("teams", {})
    away, home = teams.get("away", {}), teams.get("home", {})
    away_id = away.get("team", {}).get("id")
    home_id = home.get("team", {}).get("id")
    away_abbr = abbr_by_id.get(away_id)
    home_abbr = abbr_by_id.get(home_id)
    if away_abbr is None or home_abbr is None:
        return None  # non-MLB-team opponent (spring-training exhibition vs. an unaffiliated/college squad)
    if away.get("splitSquad") or home.get("splitSquad"):
        return None  # spring-training split-squad game -- real game, but not a meaningful team-strength signal

    completed = g.get("status", {}).get("abstractGameState") == "Final"
    game_date = g.get("officialDate")  # ISO date, already the LOCAL game date (unlike gameDate, which is UTC instant)
    # "HH:MM" UTC start time, parsed from gameDate (e.g. "2026-07-19T16:15:00Z") --
    # needed to disambiguate doubleheaders (523 of 23,864 team-days in the cached
    # dataset share a same-day, same-matchup pair, ~2.2% -- not negligible) since
    # Kalshi's own event ticker embeds a UTC HHMM component for exactly this reason.
    gametime = None
    raw_dt = g.get("gameDate", "")
    if "T" in raw_dt:
        gametime = raw_dt.split("T", 1)[1][:5]

    return {
        "id": str(g["gamePk"]),
        "season": int(g["season"]),
        "game_type": g.get("gameType"),  # R | S | F | D | L | W | A
        "game_number": g.get("gameNumber", 1),  # disambiguates doubleheader games sharing officialDate
        "gameday": game_date,
        "gametime": gametime,
        "away_team": away_abbr,
        "home_team": home_abbr,
        "away_score": away.get("score") if completed else None,
        "home_score": home.get("score") if completed else None,
        "away_probable_pitcher": (away.get("probablePitcher") or {}).get("fullName"),
        "home_probable_pitcher": (home.get("probablePitcher") or {}).get("fullName"),
        "away_probable_pitcher_id": (away.get("probablePitcher") or {}).get("id"),
        "home_probable_pitcher_id": (home.get("probablePitcher") or {}).get("id"),
        "venue": (g.get("venue") or {}).get("name"),
    }


def fetch_games(start: dt.date, end: dt.date, game_type: str = "R") -> list[dict]:
    """Pulls every MLB game of `game_type` in [start, end], inclusive, in a
    SINGLE request (confirmed live: no cap on date-range width, unlike ESPN's
    NBA endpoint) -- no chunking needed.

    Deduplicated by gamePk (real bug caught live 2026-07-17, first poller
    run): a postponed/rescheduled game can appear under BOTH its original and
    makeup date within the same range, and inserting the same id twice in one
    unflushed batch throws a sqlite UNIQUE-constraint IntegrityError before
    upsert_mlb_games's own session.get()-then-add existence check ever gets a
    chance to see the first one. Same dict-keyed dedup pattern nba_data.py's
    fetch_games already uses, for the same class of reason (there, overlapping
    7-day fetch windows; here, a game relisted under two dates)."""
    abbr_by_id = _team_abbr_map()
    raw = statsapi_mlb_client.get_schedule(f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", game_type=game_type)
    games_by_id: dict[str, dict] = {}
    for g in raw:
        parsed = _parse_game(g, abbr_by_id)
        if parsed is not None:
            games_by_id[parsed["id"]] = parsed
    return list(games_by_id.values())


def compute_rest_days(games: list[dict]) -> None:
    """Mutates each game dict in place, adding away_rest/home_rest -- whole
    days since that team's previous game of ANY type in this dataset. Same
    "no prior game -> None, not a guessed default" convention as nba_data.py.
    MLB's daily-game schedule (no weekly bye) makes 0-days-rest the NORM, not
    an outlier -- unlike NFL/NBA, a rest-day signal here would need to be
    checked against real data for something unusual (e.g. a getaway-day-into-
    a-cross-country-series gap), not just "0 vs not-0", before it's worth
    building into the situational layer."""
    last_played: dict[str, dt.date] = {}
    for game in sorted(games, key=lambda g: (g["gameday"], g["game_number"])):
        game_date = dt.date.fromisoformat(game["gameday"])
        for side, team_key in (("home", "home_team"), ("away", "away_team")):
            team = game[team_key]
            prev = last_played.get(team)
            game[f"{side}_rest"] = (game_date - prev).days if prev is not None else None
            last_played[team] = game_date
