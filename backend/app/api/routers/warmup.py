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
a rebuild. Rating state is read straight from the caches other code has already
filled -- no HTTP, no refresh_ratings() call. The one DB touch is a grouped
COUNT of active markets over an indexed column, used to tell "still building"
apart from "out of season, nothing to build"; it reads, never writes, and never
warms anything. A readiness probe that is slow, or that warms things as a side
effect, is worse than none: it would queue behind the very work it reports on.
"""
from __future__ import annotations

import importlib
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.db.models import Market

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


# Racing's three Kalshi series are one registry sport and one rating service.
_MARKET_SPORT_TO_SERVICE = {"f1": "racing", "nascar": "racing", "irl": "racing"}


@router.get("")
def warmup_status(session: Session = Depends(get_session)):
    """{ready, warm, total, pending, services} -- for a loading state.

    READY MEANS "every sport that has live markets has built its ratings".
    Not "all warm", and not a fraction.

    A fraction was the first attempt and it was wrong in the way that matters:
    it reported ready at 6 of 13 while soccer, racing and all four esports were
    still cold. Those are precisely the slow ones the banner exists for -- the
    ball sports warm in seconds -- so it cleared itself exactly when the user
    still needed it. "All warm" is the opposite failure: a sport out of season
    has nothing to rate and never fills, which would pin the banner up forever.

    Live-market count is the honest discriminator between "still building" and
    "nothing to build", and it costs one grouped COUNT over an indexed column.
    Everything else here stays a pure cache read -- this endpoint must never
    trigger the work it reports on, or it queues behind it.
    """
    services: dict[str, bool | None] = {}
    for label, module_path, key in _SERVICES:
        services[label] = _is_warm(module_path, key)

    try:
        rows = (session.query(Market.sport, func.count(Market.id))
                .filter(Market.status.in_(("active", "open")))
                .group_by(Market.sport).all())
        active = {_MARKET_SPORT_TO_SERVICE.get(sp, sp) for sp, n in rows if sp and n}
    except Exception:  # noqa: BLE001 - readiness must never 500
        log.exception("warmup could not count active markets; falling back to all-warm")
        active = {k for k, v in services.items() if v is not None}

    # Only sports with something to price gate readiness.
    gating = {k: v for k, v in services.items() if v is not None and k in active}
    pending = sorted(k for k, v in gating.items() if not v)
    known = [v for v in services.values() if v is not None]
    return {
        "ready": not pending,
        "warm": sum(1 for v in known if v),
        "total": len(known),
        "pending": pending,
        "services": services,
    }
