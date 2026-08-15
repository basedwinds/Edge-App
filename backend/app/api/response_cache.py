"""Short-TTL in-process cache for the heavy read endpoints (each sport's
/markets and /futures list, plus the cross-platform divergence + CLV-bucket
reports). These recompute model_prob/kelly/snapshots over thousands of rows and
hit a 5.5M-row snapshot table, so under the 5-minute data pollers' write load a
single one takes 5-13s -- and the combined "/all" page fires ~10 at once, which
starves them. Caching is safe here because the underlying data only changes
once per ~5-minute poll cycle, so a ~45s stale read is never meaningfully out
of date, and it removes the recompute + DB contention entirely on cache hits.

Only GET list endpoints are cached (never POST/PUT: placing a bet, editing
settings). A just-placed bet or a settings change can therefore take up to the
TTL to reflect on these lists -- an acceptable trade for making every page load
instant. A per-key lock collapses a thundering herd (concurrent identical
requests) into a single computation.
"""
import asyncio
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.api.start_gate import apply_start_gate

log = logging.getLogger(__name__)

# Generous TTL because a background warmer (scheduler.py::run_cache_warm)
# refreshes these every ~150s off the request path -- so user requests should
# essentially always hit a warm entry, even for the intrinsically-slow
# endpoints (tennis/markets is ~31s to compute over 3,600 rows). The TTL is the
# fallback ceiling if the warmer misses a cycle.
# Lowered from 300s on 2026-08-03. The live-match gates (volume, decided,
# started) are evaluated when the payload is BUILT and then frozen for the whole
# TTL. A real case: Alejo Sanchez Quilez vs Rafael Izquierdo Luque was cached
# while its market had traded only 2,890 -- correctly under the live-trading
# ceiling at that instant -- then went live and traded up to 39,653, while the
# cache kept serving it as a $20 recommendation. A stale PRICE is a minor
# annoyance; a stale SAFETY DECISION recommends a bet on a match already in
# play, so the window has to be short.
# 2026-08-03, SECOND revision, after the first one caused a user-visible
# regression. Lowering this to 60s (for the good reason above) ignored the fact
# that the TTL and the WARMER are a matched pair: the warmer ran every 200s, so a
# 60s TTL left the cache EMPTY for ~140s of every cycle. During that hole a
# request had to compute live -- and the combined /all page fires ~20 endpoints
# at once -- so tennis appeared for about a minute, vanished for two, and came
# back, over and over. That is precisely the flicker the user reported.
#
# The constants are now sized against a MEASURED full warm pass: 61.7s across 20
# endpoints (slowest: /tennis/futures 14.6s, /tennis/markets 11.9s). A 60s TTL
# was not merely mismatched with the 200s interval -- it was shorter than one
# pass, so no interval could have kept it covered.
#
# The invariant to preserve when touching either number:
#     CACHE_TTL_SECONDS  >  warm interval + one full pass
# Currently 180 > 90 + ~62, so every entry is refreshed with roughly 30s to
# spare. See scheduler.py's cache_warm job -- change the two together or the
# flicker comes straight back.
#
# 2026-08-15, THIRD revision. The invariant above is now VIOLATED and cannot be
# restored by tuning it: a measured full warm pass is 290s (slowest
# /tennis/markets 60.1s, /markets/cross-platform-divergences 57.4s,
# /soccer/markets 35.6s, /markets 32.5s), so the requirement reads 180 > 90+290.
# Because the pass exceeds the interval, APScheduler runs passes back-to-back
# and each entry is refreshed once per ~290-380s while expiring at 180s -- every
# entry sat COLD for roughly a third of the time, and whichever route the user
# landed on then computed live, past the frontend's 18s guard(). That is the
# "Incomplete board: X did not load in time" banner, reported repeatedly.
#
# THIS IS NOT A SCHEDULING PROBLEM. 290s of compute per 180s window is over
# 100% duty cycle, so no interval, ordering or priority scheme keeps everything
# fresh. Concurrency does not rescue it either: measured at 4 workers the pass
# only fell to 219s, and individual routes got much WORSE (divergences 57s ->
# 176s) because these are CPU-bound model computes contending with the pollers'
# SQLite writes. The choice is therefore to serve something slightly stale or to
# make the user wait a minute -- and the second is what was already happening.
#
# So the answer is the follow-up this file has carried since 2026-08-03: --
#   "re-evaluate the cheap time-based gates (already_started/already_decided)
#    when a cached payload is SERVED, so cache age cannot produce a stale safety
#    decision at all"
# -- now implemented in app/api/start_gate.py and wired for all thirteen sports.
# With the started-gate re-applied at serve time, an expired entry is safe to
# serve, so STALE_SERVE_SECONDS below buys the coverage the TTL alone cannot.
CACHE_TTL_SECONDS = 180
# How long past the TTL an entry may still be served, with the start gate
# re-applied, instead of making the user wait for a live recompute.
#
# Sized against the MEASURED worst-case refresh period, not chosen: pass 290s
# plus up to one 90s interval of idle before the next pass = 380s. 420 covers
# that with ~40s of headroom. Past this point the warmer is not merely slow but
# absent (crashed, shut down, wedged), and computing live is the right answer
# again -- a genuinely restarting backend SHOULD show the banner, which is what
# it says.
#
# What staleness costs: a PRICE up to TTL+grace old. That is bounded and small
# next to what the frontend already does -- guard() falls back to a
# sessionStorage "last good" copy of UNBOUNDED age with no gate applied at all
# (see Combined.tsx). Serving a bounded, gated payload is strictly better on
# both axes than the fallback it replaces.
STALE_SERVE_SECONDS = 420
# The warmer sends this header to force a recompute+recache even when the
# current entry is still fresh, so the cache never ages out from under a user.
REFRESH_HEADER = "x-cache-refresh"

# key -> (expires_at, status_code, body_bytes, media_type)
_cache: dict[str, tuple[float, int, bytes, str | None]] = {}
# Counts stale serves per key so the board scan can tell "the warmer is keeping
# up" from "the warmer has quietly fallen behind and every read is stale".
_stale_serves: dict[str, int] = {}
_locks: dict[str, asyncio.Lock] = {}

_EXPLICIT = {
    "/markets",
    "/markets/futures",
    "/markets/cross-platform-divergences",
    "/placed-bets/clv-buckets",
}


def _cacheable(method: str, path: str) -> bool:
    if method != "GET":
        return False
    if path in _EXPLICIT:
        return True
    # /nba/markets, /mlb/futures, /wnba/markets, ... (but NOT
    # /markets/{id}/reasoning, which ends in neither).
    return path.endswith("/markets") or path.endswith("/futures")


def _fresh(key: str):
    entry = _cache.get(key)
    if entry and entry[0] > time.time():
        return Response(content=entry[2], status_code=entry[1], media_type=entry[3])
    return None


def _stale(key: str):
    """An EXPIRED entry, still young enough to serve, with the started-gate
    re-applied. Returns None once it is too old to be trustworthy.

    The gate runs only here, not on fresh hits: a fresh payload is at most
    CACHE_TTL_SECONDS old, which is the staleness the routers' own gates were
    already sized against, and re-parsing 11k soccer rows on every fast hit
    would tax the common path to fix the rare one."""
    entry = _cache.get(key)
    if not entry:
        return None
    age_past_ttl = time.time() - entry[0]
    if age_past_ttl <= 0 or age_past_ttl > STALE_SERVE_SECONDS:
        return None
    body, gated = apply_start_gate(entry[2])
    _stale_serves[key] = _stale_serves.get(key, 0) + 1
    if gated:
        log.info("cache: served %s stale by %.0fs, start-gate cleared %d stake(s)",
                 key, age_past_ttl, gated)
    return Response(content=body, status_code=entry[1], media_type=entry[3])


def stale_serve_counts() -> dict[str, int]:
    """Read by scripts/board_artifact_scan.py -- a key that is ALWAYS served
    stale means the warm pass has outgrown TTL + STALE_SERVE_SECONDS and the
    two constants need re-sizing against a fresh measurement."""
    return dict(_stale_serves)


class ResponseCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not _cacheable(request.method, path):
            return await call_next(request)

        key = f"{path}?{request.url.query}"
        # The warmer forces a recompute so it can refresh a still-fresh entry.
        force = request.headers.get(REFRESH_HEADER) == "1"
        if not force:
            hit = _fresh(key)
            if hit is not None:
                return hit
            # Expired but recent: serve it rather than make the user wait out a
            # 20-60s recompute. The warmer's next pass replaces it. See
            # STALE_SERVE_SECONDS.
            hit = _stale(key)
            if hit is not None:
                return hit

        lock = _locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not force:
                hit = _fresh(key)  # another request may have filled it while we waited
                if hit is None:
                    hit = _stale(key)
                if hit is not None:
                    return hit
            response = await call_next(request)
            body = b"".join([chunk async for chunk in response.body_iterator])
            if response.status_code == 200:
                _cache[key] = (time.time() + CACHE_TTL_SECONDS, response.status_code, body, response.media_type)
            # Rebuild a plain Response from the captured body (the original's
            # body_iterator is now consumed). media_type carries the JSON
            # content-type; Starlette recomputes content-length.
            return Response(content=body, status_code=response.status_code, media_type=response.media_type)
