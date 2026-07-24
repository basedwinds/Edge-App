"""Load every sport's Elo/rating cache straight from the DB on startup.

Each elo_service_*.refresh_ratings() reads only the local DB (already populated
and persisted across restarts) and computes ratings in-process -- no network.
The 5-minute market pollers (run_full_refresh_*) ALSO call these, but they do
so at the END of a slow, network-bound refresh that can take minutes or fail
partway (Kalshi/Polymarket/data-source I/O). That left a real window right
after every restart where a request -- or worse, the response-cache warmer
(scheduler.py::run_cache_warm), which then caches the result for up to the
full TTL -- computed model_prob=None ("no baseline yet") for a whole sport
because its Elo hadn't loaded yet. Esports were the visible casualty: cs2 and
valorant recommended picks silently vanished after a restart until a later
warm cycle happened to recompute once the poller had finally populated Elo.

Warming the ratings here -- DB-only, so a couple seconds for all ten -- makes
model_prob correct from the first request, independent of the slow pollers.
Best-effort per sport: one sport's failure never blocks the others.
"""
import logging

from app.models.baseline import (
    elo_service,
    elo_service_cs2,
    elo_service_lol,
    elo_service_mlb,
    elo_service_mma,
    elo_service_nba,
    elo_service_soccer,
    elo_service_tennis,
    elo_service_valorant,
    elo_service_wnba,
    racing_ratings,
)

log = logging.getLogger("warm_all")

_SERVICES = [
    ("nfl", elo_service),
    ("nba", elo_service_nba),
    ("wnba", elo_service_wnba),
    ("mlb", elo_service_mlb),
    ("mma", elo_service_mma),
    ("tennis", elo_service_tennis),
    ("soccer", elo_service_soccer),
    ("valorant", elo_service_valorant),
    ("cs2", elo_service_cs2),
    ("lol", elo_service_lol),
    ("racing", racing_ratings),
]


def warm_all_elo():
    """Refresh every sport's rating cache from the DB. Best-effort per sport."""
    for name, service in _SERVICES:
        try:
            service.refresh_ratings()
        except Exception:
            log.exception("startup Elo warm failed for %s", name)
