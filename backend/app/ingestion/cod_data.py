"""Live Call of Duty match ingestion from breakingpoint.gg's tRPC API.

Sibling of cs2_data.py / lol_data.py, and the FIRST sport in this app with a
first-class live flag from its own source.

WHY THAT MATTERS MORE THAN IT SOUNDS. Every other sport here decides "has this
match started?" by comparing a platform-supplied start time against the clock,
and that has now failed twice in one day:

  * Soccer, 2026-08-09: Kalshi's occurrence_datetime said 14:30Z for a match
    that kicked off at 11:30Z, so a LIVE 1-0 match was recommended as a bet.
  * Call of Duty, the same afternoon, found while wiring this file: Kalshi's
    occurrence_datetime said 17:00Z for Team Heretics vs Team Falcons, which
    was already live at 13:00Z with Heretics leading 2-0. The market price
    (Falcons 0.37) was a LIVE price; the model's 0.68 was a PRE-MATCH number,
    and the 31pp "edge" between them was pure artifact.

breakingpoint's fetchLiveMatches returns exactly the matches in progress, and
every row carries status in {upcoming, live, complete}. So CoD does not have to
INFER liveness from a clock it cannot trust -- it is told. `is_live` is stored
on the row and the router gates on it directly.

The start time is still recorded and still corrected, because a match can be
live before any poll observes it. Belt and braces: the flag is the primary
guard, the clock is the backstop.

THE ENDPOINTS (plain GET, ?input={"json":{...}}):

    cached.matches.fetchMatchesPage   {seasonId, status, cdlOnly, teamIds:[],
                                       eventIds:[], pageSize}
                                      -> {"pages":[{"data":[...]}]}  (or the
                                      page directly on a direct call)
    cached.matches.fetchLiveMatches   {seeOnlyCDL} -> [...]
    matches.fetchMatchStatusCounts    {seasonId, cdlOnly} -> counts

fetchMatchesPage is used for the schedule because it is the only one that
carries `best_of`; fetchLiveMatches and fetchUpcomingMatches both return it as
null, and a Bo7 priced as a Bo5 is a materially different probability.

RATE LIMITING: deliberately slow, and slower than this needs. See
build_cod_match_cache_bp.py for why this project is now cautious about a host's
tolerance regardless of what robots.txt does or does not say.
"""
from __future__ import annotations

import datetime
import json
import logging
import time
import urllib.parse

import httpx

log = logging.getLogger("cod_data")

BASE = "https://breakingpoint.gg/api/trpc"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) nfl-edge-app/1.0 "
      "(personal research; github.com/basedwinds/Edge-App)")

DELAY = 1.5
PAGE_SIZE = 100

_client = httpx.Client(timeout=45.0, headers={"User-Agent": UA,
                                              "Referer": "https://breakingpoint.gg/"})


def _call(procedure: str, payload: dict):
    inp = urllib.parse.quote(json.dumps({"json": payload}))
    try:
        r = _client.get(f"{BASE}/{procedure}?input={inp}")
    except httpx.HTTPError:
        log.exception("cod %s: transport error", procedure)
        return None
    finally:
        time.sleep(DELAY)
    if r.status_code != 200:
        # Loud. A silent empty return here reads as "no CoD matches today",
        # which is indistinguishable from a real quiet day -- the exact failure
        # mode that made the Liquipedia rate-limit invisible for an hour.
        log.error("cod %s: HTTP %s -- %s", procedure, r.status_code, r.text[:160])
        return None
    try:
        return r.json()["result"]["data"]["json"]
    except (KeyError, ValueError):
        log.exception("cod %s: unexpected response envelope", procedure)
        return None


def _season_id(today: datetime.date | None = None) -> int:
    """breakingpoint's seasonId is just the calendar year."""
    return (today or datetime.date.today()).year


def _normalise(row: dict) -> dict | None:
    t1 = (row.get("team1") or {}).get("name")
    t2 = (row.get("team2") or {}).get("name")
    if not t1 or not t2:
        return None  # an unfilled bracket slot -- real, and not a match yet
    status = (row.get("status") or "").lower()
    a, b = row.get("team_1_score"), row.get("team_2_score")
    winner = None
    if status == "complete" and a is not None and b is not None:
        winner = "team_a" if a > b else "team_b" if b > a else None
    when = row.get("datetime")
    day = (when or "")[:10]
    if not day:
        return None  # undated rows cannot be ordered, and order is everything
    return {
        "source": "breakingpoint",
        "source_match_id": f"bp:{row.get('id')}",
        "event_name": str(row.get("event_id") or "") or None,
        "match_date": day,
        "estimated_start_time": when,
        "team_a": t1,
        "team_b": t2,
        "best_of": row.get("best_of"),
        "maps_won_a": a,
        "maps_won_b": b,
        "winner": winner,
        # The whole point of this file -- see the module docstring.
        "is_live": status == "live",
        "status": status,
    }


def fetch_live_match_ids() -> set[str]:
    """source_match_ids of matches IN PROGRESS right now, straight from the
    source rather than inferred from a start time."""
    data = _call("cached.matches.fetchLiveMatches", {"seeOnlyCDL": False})
    if not data:
        return set()
    return {f"bp:{r.get('id')}" for r in data if r.get("id") is not None}


def fetch_matches() -> list[dict]:
    """Upcoming + live + recently completed, one page each.

    fetchMatchesPage is the source of truth here because it is the only
    procedure that carries best_of. fetchLiveMatches is then used purely to
    stamp is_live, since a row can be listed under 'upcoming_live' while
    already in play."""
    rows: dict[str, dict] = {}
    season = _season_id()
    for status in ("upcoming_live", "completed"):
        data = _call("cached.matches.fetchMatchesPage", {
            "seasonId": season, "status": status, "cdlOnly": False,
            "teamIds": [], "eventIds": [], "pageSize": PAGE_SIZE,
        })
        if not data:
            continue
        page = data["pages"][0] if isinstance(data, dict) and "pages" in data else data
        batch = page.get("data") if isinstance(page, dict) else None
        if not batch:
            continue
        for raw in batch:
            row = _normalise(raw)
            if row is not None:
                rows[row["source_match_id"]] = row

    live_ids = fetch_live_match_ids()
    for mid in live_ids:
        if mid in rows:
            rows[mid]["is_live"] = True
    if live_ids:
        log.info("cod: %d match(es) reported LIVE by the source", len(live_ids))

    return sorted(rows.values(), key=lambda r: (r["match_date"], r["source_match_id"]))
