"""Keep the Understat xG cache current, so the rating blend does not decay.

WHY THIS EXISTS (#203). #202 wired a goals/xG blend into the soccer attack/defence
ratings (w=0.50, held-out logloss 0.99235 -> 0.98934). The cache it reads was a
one-off crawl ending 2026-05. Ratings update after every match, so without a
refresh every NEW fixture falls back to pure goals -- correct behaviour, but it
means the improvement DECAYS SILENTLY through a season instead of failing
loudly. Silent decay is the failure mode worth automating against.

THE BUILDER SKIPS WHAT IT HAS ALREADY GOT, WHICH IS WRONG FOR A REFRESH.
scripts/build_soccer_xg_cache.py exists to do the initial crawl and deliberately
resumes -- `if key in cache[code]: continue`. That is right for backfilling
twelve seasons and exactly wrong here: the CURRENT season is already in the cache
and grows every week, so a resuming crawl would never fetch another match. This
force-refetches the current season only. Five requests, not sixty.

THE ALIAS MAP MUST BE REBUILT TOO, and this is the part that would rot quietly.
Every August brings promoted clubs whose names the map has never seen, and an
unmapped club silently drops to pure goals for its whole season -- no error, just
a slow loss of the thing #202 shipped.

THE HEADER IS THE WHOLE TRICK. GET understat.com/getLeagueData/{league}/{season}
404s to a plain request and answers only with `X-Requested-With: XMLHttpRequest`.
A User-Agent alone is not enough; a Referer is not enough. Understat stopped
embedding the data in the page HTML, so there is no fallback to scrape.

NEVER RAISES. A refresh failure must leave the previous cache in place and let
ratings keep using it, not break the soccer rating rebuild for 33 leagues.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE = "https://understat.com"
CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "soccer_xg_cache.json"

LEAGUES = {"EPL": "E0", "La_liga": "SP1", "Bundesliga": "D1",
           "Serie_A": "I1", "Ligue_1": "F1"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "X-Requested-With": "XMLHttpRequest",   # without this the endpoint 404s
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def current_season(today: dt.date | None = None) -> int:
    """Understat labels a season by the year it STARTS -- 2025/26 is "2025".
    European seasons begin in August, so anything from July onward belongs to
    the year just started; January-June still belongs to the previous year."""
    d = today or dt.date.today()
    return d.year if d.month >= 7 else d.year - 1


def _fetch_season(client: httpx.Client, slug: str, season: int) -> list[dict]:
    r = client.get(f"{BASE}/getLeagueData/{slug}/{season}",
                   headers={**HEADERS, "Referer": f"{BASE}/league/{slug}"})
    r.raise_for_status()
    rows = []
    for m in r.json().get("dates", []):
        # Unplayed fixtures carry no result and no xG. Skipped rather than
        # stored as nulls so a consumer never has to guess whether 0.0 means
        # "no shots" or "not played yet".
        if not m.get("isResult"):
            continue
        try:
            rows.append({
                "date": m["datetime"][:10],
                "home": m["h"]["title"],
                "away": m["a"]["title"],
                "goals_h": int(m["goals"]["h"]),
                "goals_a": int(m["goals"]["a"]),
                "xg_h": float(m["xG"]["h"]),
                "xg_a": float(m["xG"]["a"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def refresh(season: int | None = None) -> int:
    """Re-fetch the current season for all five leagues and rebuild the alias
    map. Returns matches written, 0 on any failure. Never raises."""
    season = season if season is not None else current_season()
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    except Exception:
        log.exception("soccer xG cache unreadable -- refresh aborted, leaving it alone")
        return 0

    written = 0
    try:
        with httpx.Client(timeout=60, follow_redirects=True, headers=HEADERS) as client:
            client.get(f"{BASE}/league/EPL")   # establish a session like a browser
            for slug, code in LEAGUES.items():
                try:
                    rows = _fetch_season(client, slug, season)
                except Exception as exc:
                    # One league failing must not discard the other four.
                    log.warning("soccer xG refresh: %s %s failed (%s) -- keeping cached copy",
                                code, season, type(exc).__name__)
                    continue
                if not rows:
                    log.warning("soccer xG refresh: %s %s returned 0 played matches -- "
                                "keeping cached copy rather than blanking it", code, season)
                    continue
                cache.setdefault(code, {})[str(season)] = rows
                written += len(rows)
    except Exception:
        log.exception("soccer xG refresh failed outright -- cache untouched")
        return 0

    if not written:
        return 0
    try:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.exception("soccer xG cache write failed")
        return 0

    _rebuild_alias_map()
    _invalidate()
    log.info("soccer xG refreshed: %d played matches for season %s", written, season)
    return written


def _rebuild_alias_map() -> None:
    """Promoted clubs arrive every August with unseen names; an unmapped club
    silently falls back to pure goals for a whole season. Imported lazily and
    defensively -- `scripts` is a namespace package and may not be importable
    depending on how the process was started, and a missing alias rebuild is far
    less bad than a refresh job that crashes."""
    try:
        from scripts.build_understat_alias_map import main as build_aliases
        build_aliases()
    except Exception:
        log.exception("understat alias rebuild failed -- existing map kept, new "
                      "clubs will price on pure goals until this is fixed")


def _invalidate() -> None:
    try:
        from app.models.baseline import soccer_xg
        soccer_xg._cache.clear()
    except Exception:
        log.exception("could not clear the soccer_xg module cache")
