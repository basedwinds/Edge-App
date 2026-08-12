"""Memoize a loader that parses large on-disk JSON caches, invalidating when
those files actually change.

WHY THIS EXISTS (measured 2026-08-12, py-spy on the live worker). `json.loads`
was the single largest self-time consumer in the whole process -- 18.7% of all
CPU -- because `soccer_data.load_matches()` re-reads and re-parses **122 MB** of
JSON on every call, and `soccer_markets.list_soccer_futures` called it ONCE PER
DIVISION inside its league loop. At 1.26s a call and 24 divisions that is 30
seconds of identical work per request, and because CPython holds the GIL through
`json.loads` it did not merely make soccer slow -- it starved every other request
thread in the process. One `/soccer/futures` + `/racing/futures` pair was
measured taking 82% of all CPU across an 85s window while `/soccer/markets`, in
flight the whole time, got 1.7s and took 77 seconds to answer.

Keyed on (path, mtime_ns, size) of every input file rather than a TTL, because
these caches are rebuilt by scripts and pollers at unpredictable times: a TTL
either serves stale ratings after a rebuild or throws the work away for nothing.
Stat-ing ~25 files costs well under a millisecond against a 1.26s parse.

THE RETURNED OBJECT IS SHARED AND MUST BE TREATED AS READ-ONLY. Callers were
checked before this was introduced: elo_service_soccer.refresh_ratings copies
each row (`{**m, ...}`) before touching it, and season_sim_soccer.
current_season_table plus integrity_checks only read. A caller that mutates a
row would now corrupt every other caller's view, which no TTL-free cache can
defend against -- so mutate a copy, never the row.
"""
from __future__ import annotations

import functools
import os
import threading
from pathlib import Path
from typing import Callable, Iterable


def _signature(paths: Iterable[Path]) -> tuple:
    """(path, mtime_ns, size) per input file. A missing file contributes a
    None entry rather than raising, so a cache that has not been built yet
    still produces a stable key -- and starts producing a different one the
    moment it appears."""
    sig = []
    for p in sorted(paths):
        try:
            st = os.stat(p)
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None, None))
    return tuple(sig)


def memoize_on_files(inputs: Callable[[], Iterable[Path]]):
    """Cache a zero-argument loader until one of `inputs()` changes on disk.

    `inputs` is a callable, not a fixed list, because some loaders glob a
    directory -- a league cache added after import must still invalidate.
    """
    def decorator(fn):
        lock = threading.Lock()
        state: dict = {"key": None, "value": None}

        @functools.wraps(fn)
        def wrapper():
            key = _signature(inputs())
            cached = state
            if cached["key"] == key:
                return cached["value"]
            # Only one thread parses; the rest wait and take its result. Without
            # this, the ~10 endpoints the combined page fires at once would each
            # start their own parse on a cold cache -- the same thundering herd
            # bucket_clv_stats already had to be fixed for.
            with lock:
                if state["key"] == key:      # filled while we waited
                    return state["value"]
                value = fn()
                state.update(key=key, value=value)
                return value

        wrapper.cache_clear = lambda: state.update(key=None, value=None)
        return wrapper
    return decorator
