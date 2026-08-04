"""gol.gg fallback for LoL results, because Leaguepedia will not come back.

REAL BUG this exists for: 223 finished LoL matches carried winner=None, so 359
placed LoL bets could never settle. The cause was NOT a transient outage.
`lol.fandom.com`'s cargoquery answers HTTP **200** with a body of
`{"error":{"code":"ratelimited"}}` -- MediaWiki reports errors in the payload,
not the status line -- and it has done so continuously for days. Retrying is
already automatic (run_full_refresh_lol, every 30 min) and already correct; it
simply cannot succeed, so no amount of waiting fixes it. Three User-Agents
(default httpx, descriptive, browser-like) were all refused identically, so it
is IP-level, not client-shaped. Lifting it needs an authenticated account.

gol.gg publishes the same results with no such gate and is confirmed live. The
per-game page already has a parser here (scripts/build_lol_game_lineup_cache.py,
built 2026-07-21 for the player model), and lol_lower_tier.py already aggregates
those games into series with a winner and a map score. This module supplies the
only missing pieces: keeping the game cache CURRENT, and joining the result onto
the LolMatch rows a bet settles against.

Two properties worth knowing before trusting it:

  * gol.gg trails real time by roughly 6 days (measured 2026-08-04: newest game
    was 2026-07-29). So this settles matches on a lag and will never grade
    yesterday's game. That is fine for settlement -- a bet resolves late rather
    than never -- but it is NOT a live results feed.
  * Game ids are dense and sequential, so the newest id is found by probing
    forward rather than by scraping a listing page. That matters because gol.gg's
    LISTING pages are JavaScript shells with nothing in the HTML; the per-game
    pages are plain server-rendered HTML. The earlier "gol.gg is a JS shell"
    finding was about listings only.

Unmatched rows stay winner=None and are retried, exactly as the Leaguepedia
path did. Nothing here ever guesses a winner.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("lol_results_golgg")

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"

# Politeness: the same delay the original one-off crawl used, and a per-cycle cap
# so catching up a backlog is spread over several polls instead of one long
# burst. At 30-min polls a ~400-game backlog clears in a couple of hours.
REQUEST_DELAY_SECONDS = 1.4
MAX_GAMES_PER_CYCLE = 120
# How many dead ids in a row mean "the tail, not a gap". gol.gg's id space has
# real holes, so this can't be 1 -- but it must stay finite or a caught-up
# crawler probes forever. Measured over the 10,000 cached ids: 1,550 gaps, the
# LARGEST 21 dead ids long. 60 is ~3x that worst case.
GAP_PROBE = 60

_client = httpx.Client(
    timeout=30.0,
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
)


def _parse_game(html: str) -> dict | None:
    """Delegates to the crawler that already proved this parse against real
    pages, rather than keeping a second copy of the selectors in sync."""
    from app.ingestion.lol_golgg_parse import parse_game

    return parse_game(html)


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.exception("gol.gg game cache unreadable -- treating as empty")
        return {}


def _save_cache(cache: dict) -> None:
    """Atomic replace. The Elo pool (lol_lower_tier/lol_lineups) reads this same
    file from the request path, so a half-written file would surface as a model
    that silently lost its lower-tier ratings."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, CACHE_PATH)


def _fetch(gid: int) -> dict | None:
    resp = _client.get(f"https://gol.gg/game/stats/{gid}/page-game/")
    return _parse_game(resp.text) if resp.status_code == 200 else None


def crawl_new_games(max_games: int = MAX_GAMES_PER_CYCLE) -> int:
    """Fetch ids newer than the newest cached one. Returns games newly stored.

    Network only -- call OUTSIDE the DB write lock. Ids that hold no game (real
    gaps in gol.gg's id space) are cached as null so they are probed once, not
    forever, which is also what stops the cursor stalling on a gap.
    """
    cache = _load_cache()
    numeric = [int(k) for k in cache if str(k).isdigit()]
    cursor = max(numeric) if numeric else 70000

    fetched: dict[int, dict | None] = {}
    consecutive_dead = 0
    gid = cursor + 1
    while len(fetched) < max_games and consecutive_dead < GAP_PROBE:
        try:
            parsed = _fetch(gid)
        except httpx.HTTPError:
            # Transient: leave UNrecorded so the next cycle retries this id
            # rather than reading a network blip as "no game here".
            time.sleep(REQUEST_DELAY_SECONDS)
            gid += 1
            continue
        fetched[gid] = parsed
        consecutive_dead = 0 if parsed else consecutive_dead + 1
        gid += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    # THE rule that keeps this crawler able to catch up: never persist a null
    # for an id ABOVE the newest real game. Those ids aren't gaps, they are
    # simply not published yet -- gol.gg trails ~6 days. Caching them dead would
    # advance the cursor past them, and because a cached id is never re-probed,
    # every game that later lands there would be invisible forever. Gaps BELOW a
    # confirmed real game are genuine and cached as null so they cost one probe.
    highest_real = max((g for g, p in fetched.items() if p), default=None)
    if highest_real is None:
        return 0
    real = 0
    for g, parsed in fetched.items():
        if g <= highest_real:
            cache[str(g)] = parsed
            real += 1 if parsed else 0

    _save_cache(cache)
    log.info("gol.gg crawl: probed %d ids from %d, stored through %d, %d real games",
             len(fetched), cursor + 1, highest_real, real)
    return real


def golgg_result_rows() -> list[dict]:
    """Cached gol.gg games aggregated into series rows.

    Reuses lol_lower_tier's aggregation -- the same (date, unordered pair)
    grouping already validated for the Elo tier-expansion -- so series
    reconstruction has exactly one implementation. `exclude_pairs` is empty here
    on purpose: that argument exists to keep the Elo pool from double-counting
    matches it already has, which is irrelevant when the question is only "what
    was the final score".
    """
    from app.models.baseline.lol_lower_tier import build_lower_tier_matches

    return build_lower_tier_matches(exclude_pairs=set())
