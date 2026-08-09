"""Build data/cod_historical_match_cache.json from breakingpoint.gg.

REPLACES build_cod_match_cache.py, which is marked DO NOT RUN: it crawled
Liquipedia and got this app's IP banned site-wide, taking live CS2 fixture
ingestion down with it (see that file, and poller_cs2.LIQUIPEDIA_COOLDOWN_KEY).

WHY THIS SOURCE IS STRICTLY BETTER, not merely available:

  * It is a JSON API, not wikitext. No brace-balancing, no template parsing,
    no shortcode resolution step that can silently mis-attach a team.
  * Teams arrive as REAL NAMES ("OpTic Gaming", "FaZe Esports"), not
    Liquipedia's "tx"/"mia" shortcodes. That deletes a whole class of join
    bug -- and market titles use real names too.
  * Rows carry `best_of` and `winner_id` directly. The Liquipedia route had
    to infer series scores by counting per-map winner= fields and excluding
    winner=skip.
  * `fetchMatchStatusCounts` reports the expected total up front, so a
    truncated crawl is DETECTABLE. The Liquipedia failure was invisible
    precisely because nothing said how much should have come back.
  * It is not a host any shipped sport depends on.

THE API. breakingpoint.gg is a Next.js app over tRPC; the same procedures its
own pages call are reachable over plain GET:

    /api/trpc/<procedure>?input={"json":{...}}

  cached.matches.fetchMatchesPage   infinite query. input:
      {seasonId, status, cdlOnly, teamIds:[], eventIds:[], pageSize}
      -> {"pages":[{"data":[...], "nextCursor": N}]}
  matches.fetchMatchStatusCounts    {seasonId, cdlOnly} -> {upcoming, live, completed}

Confirmed live 2026-08-09: seasonId 2026 reports 1,836 completed matches
across all divisions (314 CDL-only).

RATE LIMITING. There is no robots.txt, so nothing is disallowed -- but "not
disallowed" is exactly what was true of Liquipedia this morning. DELAY is set
well above what this needs and the crawl is resumable, because the cost of
being wrong about a host's tolerance is now a known quantity around here.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
MATCH_CACHE = DATA_DIR / "cod_historical_match_cache.json"

BASE = "https://breakingpoint.gg/api/trpc"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) nfl-edge-app/1.0 "
      "(personal research; github.com/basedwinds/Edge-App)")

# Deliberately generous. See the module docstring.
DELAY = 2.0
PAGE_SIZE = 100

# The CDL restructured repeatedly; seasonId is just a year here. Walk back far
# enough to cover the app's useful history and let empty seasons fall out.
SEASONS = list(range(2020, 2027))

_client = httpx.Client(timeout=45.0, headers={"User-Agent": UA,
                                              "Referer": "https://breakingpoint.gg/"})


def _call(procedure: str, payload: dict):
    inp = urllib.parse.quote(json.dumps({"json": payload}))
    try:
        r = _client.get(f"{BASE}/{procedure}?input={inp}")
    except httpx.HTTPError as exc:
        print(f"    {procedure}: transport error {type(exc).__name__}")
        return None
    finally:
        time.sleep(DELAY)
    if r.status_code != 200:
        # Loud, not silent. The Liquipedia crawl printed "0 matches" and let a
        # 429 read as "this source has no data"; that is not repeated here.
        print(f"    {procedure}: HTTP {r.status_code} -- {r.text[:120]}")
        return None
    try:
        return r.json()["result"]["data"]["json"]
    except (KeyError, ValueError) as exc:
        print(f"    {procedure}: unexpected envelope ({exc})")
        return None


def status_counts(season: int) -> dict | None:
    return _call("matches.fetchMatchStatusCounts",
                 {"seasonId": season, "cdlOnly": False})


def completed_matches(season: int, expected: int) -> list[dict]:
    """Page through one season's completed matches.

    `expected` comes from fetchMatchStatusCounts and is checked at the end --
    a crawl that silently returns a fraction is the exact failure this whole
    file exists because of."""
    rows, cursor, pages = [], None, 0
    while True:
        payload = {"seasonId": season, "status": "completed", "cdlOnly": False,
                   "teamIds": [], "eventIds": [], "pageSize": PAGE_SIZE}
        if cursor is not None:
            payload["cursor"] = cursor
        data = _call("cached.matches.fetchMatchesPage", payload)
        if not data:
            break
        # The procedure is an infinite query: prefetched state nests under
        # "pages", a direct call returns the page itself. Accept both.
        page = data["pages"][0] if isinstance(data, dict) and "pages" in data else data
        batch = page.get("data") if isinstance(page, dict) else None
        if not batch:
            break
        rows.extend(batch)
        pages += 1
        cursor = page.get("nextCursor")
        if cursor is None:
            break
    print(f"  season {season}: {len(rows)} completed over {pages} pages (expected {expected})")
    if expected and len(rows) < expected:
        print(f"    WARNING: short by {expected - len(rows)} -- do NOT treat this season as complete")
    return rows


def normalise(row: dict, season: int) -> dict | None:
    t1 = (row.get("team1") or {}).get("name")
    t2 = (row.get("team2") or {}).get("name")
    if not t1 or not t2:
        return None
    a, b = row.get("team_1_score"), row.get("team_2_score")
    if a is None or b is None or (a == 0 and b == 0):
        return None  # scheduled, forfeited or not yet scored
    day = (row.get("datetime") or "")[:10]
    if not day:
        return None  # undated rows cannot be ordered, and order is everything
    return {
        "source": "breakingpoint",
        "source_match_id": f"bp:{row.get('id')}",
        "season": season,
        "event_id": row.get("event_id"),
        "round": (row.get("round") or {}).get("name"),
        "match_date": day,
        "datetime": row.get("datetime"),
        "team_a": t1,
        "team_b": t2,
        "score_a": int(a),
        "score_b": int(b),
        "best_of": row.get("best_of"),
        "winner": t1 if a > b else t2 if b > a else None,
    }


def main() -> None:
    existing = {}
    if MATCH_CACHE.exists():
        try:
            for m in json.loads(MATCH_CACHE.read_text(encoding="utf-8")):
                existing[m["source_match_id"]] = m
        except Exception:
            existing = {}
    print(f"starting from {len(existing)} cached matches\n")

    for season in SEASONS:
        counts = status_counts(season)
        if not counts:
            print(f"  season {season}: no counts returned, skipping")
            continue
        expected = int(counts.get("completed") or 0)
        if expected == 0:
            print(f"  season {season}: 0 completed, skipping")
            continue
        for row in completed_matches(season, expected):
            norm = normalise(row, season)
            if norm:
                existing[norm["source_match_id"]] = norm

    out = sorted(existing.values(), key=lambda r: (r["match_date"], r["source_match_id"]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MATCH_CACHE.write_text(json.dumps(out, indent=1), encoding="utf-8")

    print(f"\nwrote {len(out)} matches -> {MATCH_CACHE}")
    if out:
        teams = {r["team_a"] for r in out} | {r["team_b"] for r in out}
        print(f"date range {out[0]['match_date']} .. {out[-1]['match_date']}, {len(teams)} distinct teams")


if __name__ == "__main__":
    main()
