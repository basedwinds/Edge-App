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
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

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
# This does mean a safety decision can now be up to ~3 minutes old rather than
# 1. That is still better than the 300s it was before today, but it is a
# stop-gap, not the real answer: the right fix is to re-evaluate the cheap
# time-based gates (already_started/already_decided) when a cached payload is
# SERVED, so cache age cannot produce a stale safety decision at all. Logged as
# the follow-up rather than bolted on here, since it touches every sport's
# payload shape, not just tennis.
CACHE_TTL_SECONDS = 180
# The warmer sends this header to force a recompute+recache even when the
# current entry is still fresh, so the cache never ages out from under a user.
REFRESH_HEADER = "x-cache-refresh"

# key -> (expires_at, status_code, body_bytes, media_type)
_cache: dict[str, tuple[float, int, bytes, str | None]] = {}
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

        lock = _locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not force:
                hit = _fresh(key)  # another request may have filled it while we waited
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
