"""LoL match data ingestion -- queries Leaguepedia's real Cargo API
(lol.fandom.com/api.php?action=cargoquery), NOT plain HTML scraping (unlike
vlr.gg/liquipedia.net) -- Leaguepedia (a Fandom wiki) genuinely supports
Cargo, confirmed live 2026-07-19 via a real rate-limit error response (not
an "unrecognized action" error the way the identical query against
Liquipedia's counterstrike wiki failed) -- proof the endpoint is real, just
needs polite pacing.

REAL TABLE/FIELD NAMES used below are grounded in Leaguepedia's own
documentation and a real, actively-used open-source client
(github.com/mrtolkien/leaguepedia_parser's actual field_names.py, fetched
directly 2026-07-19), NOT guessed:
  - `MatchSchedule` table: Team1, Team2, Team1Score, Team2Score, Winner,
    DateTime_UTC, BestOf, OverviewPage -- SERIES-level (this app's LolMatch
    shape), covers both upcoming AND completed matches (confirmed via
    Leaguepedia's own Module:CargoDeclare/MatchSchedule page).
  - `ScoreboardGames` table (NOT used here, but confirmed real too): GameId,
    MatchId, Team1, Team2, Winner, DateTime_UTC, N_GameInMatch -- individual
    MAP-level rows, results-only (no upcoming placeholder rows) -- a future
    increment could use this for historical map-level Elo training data,
    not attempted in this first pass (see elo_service_lol.py's cold-start
    caveat).

RATE LIMITING (important, hit live during this build): Leaguepedia's Cargo
endpoint specifically (not the rest of its MediaWiki API) throttles
anonymous/unauthenticated requests -- confirmed live: repeated cargoquery
calls returned {"error":{"code":"ratelimited", ...}} for several minutes
straight while plain action=query calls succeeded immediately. This module
makes exactly ONE cargoquery call per poll cycle (mirrors ufcstats_client.py's
REQUEST_DELAY_SECONDS politeness discipline, just at the "one call per
5-minute poller tick" level instead of a per-page delay) -- if this still
trips the rate limit in production, the fix is a longer poller interval for
LoL specifically, not a workaround around the limit.

Direct HTTP page fetches of lol.fandom.com (e.g. its wiki article pages) ARE
Cloudflare-gated (confirmed live: a plain httpx GET of a Module: page hit a
real Cloudflare Turnstile challenge page) -- but the api.php endpoint itself
is NOT behind that same gate (confirmed: real JSON responses, including the
real rate-limit error itself, came back directly). This module only ever
calls api.php, never a rendered wiki page, for exactly this reason."""
from __future__ import annotations

import datetime as dt
import logging
import time

import httpx

log = logging.getLogger("lol_data")

API_URL = "https://lol.fandom.com/api.php"

_client = httpx.Client(
    timeout=30.0,
    headers={"User-Agent": "nfl-edge-app/0.1 (personal research project; contact via GitHub)"},
)

_FIELDS = "Team1,Team2,Team1Score,Team2Score,Winner,DateTime_UTC,BestOf,OverviewPage"


def cargoquery(extra_params: dict, max_retries: int = 4, base_delay_seconds: float = 20.0) -> list[dict]:
    """Shared, resilient wrapper for every cargoquery call this module makes
    -- both the live poller's fetch_matches() below AND
    scripts/build_lol_match_cache.py's historical crawl. REAL, noisier-than-
    expected rate limiting confirmed live (2026-07-19): NOT a simple fixed
    per-request cooldown -- back-to-back probes at 5/10/15/20/30/40s gaps
    each independently succeeded OR failed with no clean pattern, consistent
    with a token-bucket budget shared across this session's own recent
    traffic rather than a flat "wait N seconds" rule. Exponential backoff
    (starting at base_delay_seconds, capped, retried up to max_retries
    times) is the robust answer regardless of the exact underlying rule --
    same "don't guess the precise limit, just back off and retry" discipline
    as base.py's own Kalshi 429 handling, scaled up for a much more
    aggressive limit on this specific endpoint."""
    params = {"action": "cargoquery", "format": "json", **extra_params}
    delay = base_delay_seconds
    for attempt in range(max_retries):
        resp = _client.get(API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        if "error" not in data:
            return data.get("cargoquery", [])
        if data["error"].get("code") != "ratelimited":
            raise RuntimeError(f"leaguepedia cargoquery error: {data['error']}")
        log.info("leaguepedia rate-limited (attempt %d/%d), backing off %.0fs", attempt + 1, max_retries, delay)
        time.sleep(delay)
        delay = min(delay * 1.5, 300)
    raise RuntimeError(f"leaguepedia cargoquery still rate-limited after {max_retries} attempts")


def _parse_datetime_utc(raw: str | None) -> dt.datetime | None:
    """Leaguepedia's own DateTime_UTC format is "YYYY-MM-DD HH:MM:SS"."""
    if not raw:
        return None
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def parse_cargo_row(row: dict) -> dict | None:
    title = row.get("title", row)  # cargoquery's real response shape wraps each row's fields under "title"
    team_a, team_b = title.get("Team1"), title.get("Team2")
    if not team_a or not team_b:
        return None
    start_dt = _parse_datetime_utc(title.get("DateTime_UTC"))
    if start_dt is None:
        return None

    winner_raw = title.get("Winner")
    winner = None
    if winner_raw == "1":
        winner = "team_a"
    elif winner_raw == "2":
        winner = "team_b"

    def _to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    best_of = _to_int(title.get("BestOf"))
    overview_page = title.get("OverviewPage") or ""
    source_match_id = f"{overview_page}:{team_a}:{team_b}:{title.get('DateTime_UTC')}"

    return {
        "source": "leaguepedia",
        "source_match_id": source_match_id,
        "event_name": overview_page,
        "match_date": start_dt.date().isoformat(),
        "estimated_start_time": start_dt.isoformat(),
        "team_a": team_a,
        "team_b": team_b,
        "best_of": best_of,
        "maps_won_a": _to_int(title.get("Team1Score")),
        "maps_won_b": _to_int(title.get("Team2Score")),
        "winner": winner,
    }


def fetch_matches(days_back: int = 3, days_forward: int = 14) -> list[dict]:
    """Queries MatchSchedule for matches within [today - days_back, today +
    days_forward] -- covers both recently-decided AND upcoming matches in
    one call (same "one page/call IS the schedule" pattern as cs2_data.py/
    valorant_data.py, just via a real API query instead of HTML)."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=days_back)).isoformat()
    end = (today + dt.timedelta(days=days_forward)).isoformat()
    where = f'DateTime_UTC >= "{start}" AND DateTime_UTC <= "{end} 23:59:59"'
    rows = cargoquery({
        "tables": "MatchSchedule",
        "fields": _FIELDS,
        "where": where,
        "order_by": "DateTime_UTC",
        "limit": 500,
    })
    matches = []
    for row in rows:
        parsed = parse_cargo_row(row)
        if parsed is not None:
            matches.append(parsed)
    return matches
