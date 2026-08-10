"""Is the app's in-memory model state built yet?

WHY THIS EXISTS. Every rating service keeps its state in a process-local cache
that is empty on startup and filled by the first poll. Until then a sport's
markets endpoint either blocks for minutes while it rebuilds ratings, or
answers instantly with every model_prob null. Both look like a broken app: an
endless spinner, or a full table of blanks with no explanation.

Measured cold-start costs on this machine: soccer /markets 159s, tennis
/futures 350s, and racing championship futures stay unpriced for ~8 minutes
because the racing poller's first run is staggered that far after boot. None of
that is a fault -- it is 240k soccer matches and a dozen Monte Carlo sims being
built -- but the UI had no way to say so.

DESIGN CONSTRAINT: this endpoint must be INSTANT and must never itself trigger
a rebuild. It only reads the caches other code has already filled -- no DB
query, no HTTP, no refresh_ratings() call. A readiness probe that is slow, or
that warms things as a side effect, is worse than none: it would queue behind
the very work it is reporting on.
"""
from __future__ import annotations

import importlib
import logging

from fastapi import APIRouter

router = APIRouter(prefix="/warmup", tags=["warmup"])
log = logging.getLogger("warmup")

# (label, module path, callable taking the module's _cache and returning bool).
# Each sport's cache has its own shape, so the readiness test is per-service
# rather than a generic "is _cache truthy" -- several initialise to a dict of
# empty containers, which is truthy while holding nothing.
_SERVICES: list[tuple[str, str, str]] = [
    ("nfl", "app.models.baseline.elo_service", "state"),
    ("nba", "app.models.baseline.elo_service_nba", "state"),
    ("wnba", "app.models.baseline.elo_service_wnba", "state"),
    ("mlb", "app.models.baseline.elo_service_mlb", "state"),
    ("cfb", "app.models.baseline.elo_service_cfb", "state"),
    ("mma", "app.models.baseline.elo_service_mma", "state"),
    ("tennis", "app.models.baseline.elo_service_tennis", "state"),
    ("cs2", "app.models.baseline.elo_service_cs2", "state"),
    ("lol", "app.models.baseline.elo_service_lol", "state"),
    ("valorant", "app.models.baseline.elo_service_valorant", "state"),
    ("cod", "app.models.baseline.elo_service_cod", "state"),
    ("soccer", "app.models.baseline.elo_service_soccer", "states_by_league"),
    ("racing", "app.models.baseline.racing_ratings", "*"),
]


def _is_warm(module_path: str, key: str) -> bool | None:
    """True/False, or None when the service cannot be inspected at all."""
    try:
        mod = importlib.import_module(module_path)
    except Exception:  # noqa: BLE001 - a missing module is a real "unknown"
        return None
    cache = getattr(mod, "_cache", None)
    if cache is None:
        return None
    if key == "*":
        return bool(cache)
    value = cache.get(key)
    # A dict-shaped cache (soccer's states_by_league, racing's per-series map)
    # is only warm once it actually holds entries; an empty dict is the
    # INITIAL value, not a result.
    return bool(value)


@router.get("")
def warmup_status():
    """{ready, warm, total, services:{name: bool|null}} -- for a loading state.

    `ready` is deliberately NOT "every service warm". Sports out of season
    legitimately never fill (there is nothing to rate), and gating the whole UI
    on them would leave a permanent loading screen in, say, February. It means
    "enough is built that the pages will show real numbers": the caches fill
    within a poll cycle of each other, so a majority warm is the honest signal
    that boot is done rather than in progress.
    """
    services: dict[str, bool | None] = {}
    for label, module_path, key in _SERVICES:
        services[label] = _is_warm(module_path, key)

    known = [v for v in services.values() if v is not None]
    warm = sum(1 for v in known if v)
    total = len(known)
    return {
        "ready": bool(total) and warm >= max(1, total // 2),
        "warm": warm,
        "total": total,
        "services": services,
    }
