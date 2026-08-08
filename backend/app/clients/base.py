import logging
import time

import httpx

log = logging.getLogger("clients.base")

_client = httpx.Client(timeout=30.0, headers={"User-Agent": "nfl-edge-app/0.1"})


def get_json(url: str, retries: int = 4, follow_redirects: bool = False):
    """follow_redirects defaults off (httpx's own default) to keep every
    existing caller's behavior identical. Kalshi's own catalog endpoints
    301 on some path shapes -- a bare `/series/` redirects to `/series` --
    so callers hitting those pass follow_redirects=True rather than
    silently getting a 301 body with no JSON payload."""
    for attempt in range(retries):
        try:
            resp = _client.get(url, follow_redirects=follow_redirects)
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError:
            raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1 * (attempt + 1))
    raise RuntimeError(f"failed after {retries} retries: {url}")


def _stop_paging(results: list, where: str) -> bool:
    """Should a mid-pagination HTTP error end the walk instead of killing the run?

    REAL BUG this fixes. Polymarket's Gamma API refuses offsets past a ceiling --
    /events?tag_slug=soccer 422s at offset 2100 -- and get_json re-raises 4xx
    without retrying. So paginate propagated it and took the WHOLE soccer refresh
    down: not one skipped page, but every market for the sport silently ceasing to
    update once inventory grew past that point. The same shape as the Polymarket
    tennis crash that stopped all tennis prices earlier, and just as invisible.

    An error partway through a walk means "the API will not page further", which
    is a stopping condition, not a failure. An error on the FIRST page means the
    request itself is wrong -- wrong endpoint, bad auth, dead host -- and must
    still raise loudly rather than quietly returning nothing.

    So: stop and warn if we already have rows, re-raise if we have none.
    """
    if not results:
        return False
    log.warning("paginate: stopping early at %s after %d rows (upstream refused to page further)",
                where, len(results))
    return True


def paginate(url_builder, list_key: str | None, limit: int = 200, cursor_style: str = "cursor"):
    """Shared pagination helper.

    cursor_style="cursor": Kalshi-style, response has `cursor` field, url_builder(cursor) -> url
    cursor_style="offset": Polymarket-style, url_builder(offset) -> url, response is a bare list
    """
    results = []
    if cursor_style == "cursor":
        cursor = ""
        while True:
            try:
                d = get_json(url_builder(cursor))
            except httpx.HTTPStatusError:
                if not _stop_paging(results, "cursor"):
                    raise
                break
            items = d.get(list_key, []) if list_key else d
            results.extend(items)
            cursor = d.get("cursor") or ""
            if not cursor or not items:
                break
    else:
        offset = 0
        while True:
            try:
                d = get_json(url_builder(offset))
            except httpx.HTTPStatusError:
                if not _stop_paging(results, f"offset {offset}"):
                    raise
                break
            items = d if list_key is None else d.get(list_key, [])
            if not items:
                break
            results.extend(items)
            offset += limit
            if len(items) < limit:
                break
    return results


# --- BATCHED KALSHI MARKET FETCH (2026-08-08) ------------------------------
# Shared because the bug was shared: every Kalshi client had a byte-identical
# get_markets_for_event that issued ONE HTTP CALL PER EVENT, and each was
# called in a loop over that series' open events.
#
# MEASURED. Per-step timing of the soccer poller showed a 784s pass against a
# 300s interval, with its "kalshi markets" step alone at 415s -- 1.4x the whole
# interval -- and swinging 174s -> 415s between consecutive passes. That swing
# is rate-limit backoff, not workload: the app had logged 805 Kalshi 429s since
# startup, and get_json above sleeps 2*(attempt+1)s per 429 up to four retries,
# so one throttled call can burn 12s doing nothing. Ten sports' pollers share
# one quota, so the per-event pattern was starving all of them at once.
#
# Kalshi returns every market for a whole SERIES in a single request. The series
# is recoverable from the event ticker, which is always "{SERIES}-{SUFFIX}". So
# this fetches the series once, groups by event_ticker, and memoizes briefly.
#
# FALLS BACK RATHER THAN GUESSING: an event absent from its series batch gets
# the original per-event request. A miss costs one call, never a wrong answer.
# A failed batch is not cached, so it retries next cycle instead of poisoning it.
_MARKET_BATCH_TTL_SECONDS = 120  # under the 300s poll interval: one refetch per cycle
_market_batch_cache: dict[tuple[str, str], tuple[float, dict[str, list[dict]]]] = {}


def markets_for_event(base_url: str, event_ticker: str) -> list[dict]:
    """Every market on `event_ticker`, served from a per-series batch."""
    series = (event_ticker or "").split("-", 1)[0].strip()
    if series:
        key = (base_url, series)
        cached = _market_batch_cache.get(key)
        if not cached or (time.time() - cached[0]) >= _MARKET_BATCH_TTL_SECONDS:
            def url_builder(cursor):
                url = f"{base_url}/markets?series_ticker={series}&status=open&limit=1000"
                return f"{url}&cursor={cursor}" if cursor else url

            try:
                grouped: dict[str, list[dict]] = {}
                for m in paginate(url_builder, list_key="markets", cursor_style="cursor"):
                    ev = m.get("event_ticker")
                    if ev:
                        grouped.setdefault(ev, []).append(m)
                _market_batch_cache[key] = (time.time(), grouped)
                cached = _market_batch_cache[key]
            except Exception:
                cached = None
        if cached:
            hit = cached[1].get(event_ticker)
            if hit is not None:
                return hit
    return get_json(f"{base_url}/markets?event_ticker={event_ticker}").get("markets", [])
