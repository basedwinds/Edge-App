import logging
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.response_cache import ResponseCacheMiddleware
from sqlalchemy.orm import Session

from app.api.routers import backtests, catalog, cfb_markets, cs2_markets, health as health_router, lol_markets, markets, mlb_markets, mma_markets, nba_markets, placed_bets, racing_markets, settings as settings_router, soccer_markets, tennis_markets, valorant_markets, wnba_markets
from app.config import settings
from app.db.database import get_session, init_db
from app.db.models import Setting
from app.ingestion.poller import LAST_REFRESH_KEY, run_full_refresh
from app.ingestion.poller_nba import run_full_refresh_nba
from app import sports as app_sports
from app.ingestion.poller_wnba import run_full_refresh_wnba
from app.ingestion.poller_cfb import run_full_refresh_cfb
from app.ingestion.poller_mlb import run_full_refresh_mlb
from app.ingestion.poller_mma import run_full_refresh_mma
from app.ingestion.poller_tennis import run_full_refresh_tennis
from app.ingestion.poller_soccer import run_full_refresh_soccer
from app.ingestion.poller_valorant import run_full_refresh_valorant
from app.ingestion.poller_cs2 import run_full_refresh_cs2
from app.ingestion.poller_lol import run_full_refresh_lol
from app.ingestion.poller_racing import run_full_refresh_racing
from app.ingestion.poller_lock import serialized
from app.models.baseline.warm_all import warm_all_elo
from app import scheduler as scheduler_module
from app import shutdown

logging.basicConfig(level=logging.INFO)


STARTUP_POLLER_STAGGER_SECONDS = 20


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Every background timer started here goes through _schedule so it is DAEMON
    # and gets cancelled on shutdown -- see the long note at the poller loop
    # below for the hang this prevents. Defined up front because the Elo warm
    # timer fires before the pollers are scheduled.
    startup_timers: list[threading.Timer] = []

    def _schedule(delay: float, fn):
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()
        startup_timers.append(timer)
    # Load every sport's Elo/rating cache straight from the DB FIRST, on its
    # own background thread firing immediately (t=0), before the staggered
    # market pollers below and long before the response-cache warmer's first
    # run (~8.7 min in). This is DB-only (a couple seconds for all ten) and
    # exists to close a real regression: the slow, network-bound
    # run_full_refresh_* pollers are what used to load Elo, so right after a
    # restart a request -- or the cache warmer, which then CACHES the result --
    # could compute model_prob=None ("no baseline yet") for a whole sport
    # whose Elo hadn't loaded yet. Esports picks (cs2/valorant) silently
    # vanished after every restart until a later warm cycle happened to
    # recompute. See warm_all.py's own docstring for the full story.
    _schedule(0, warm_all_elo)
    scheduler_module.start()
    # Kick off the first refresh in background threads so app startup isn't
    # blocked on network calls to Kalshi/Polymarket/nflverse. Staggered via
    # threading.Timer (not a blocking sleep -- app startup still isn't
    # delayed) purely to avoid all 9 sports hammering their own external
    # APIs at the exact same instant, not to prevent DB contention -- that's
    # now poller_lock.py::db_write_lock's own job, applied INSIDE each
    # sport's own refresh_*() functions around just their DB-write blocks
    # (2026-07-20 fix -- see that module's own docstring for the full real-
    # bug story on why the OLD whole-function serialized() wrapping here
    # caused real problems of its own: one sport's slow network retries
    # could block every OTHER sport's own scheduled refresh, and it
    # eventually compounded into a real QueuePool exhaustion outage).
    # run_full_refresh_* now run fully unwrapped/concurrent -- each one's
    # own network I/O never blocks another sport's, and DB writes still
    # can't collide since every sport's own write step takes the same
    # shared lock internally.
    # A half-added sport used to be silent: CFB was once missing from the alert
    # endpoints, the per-game cap and the sidebar, each with a different symptom
    # and none raising. This makes it loud at startup instead.
    _reg_problems = app_sports.check_registry_consistency()
    if _reg_problems:
        for _p in _reg_problems:
            logging.getLogger("sports").error("SPORT REGISTRY: %s", _p)

    pollers = [
        run_full_refresh, run_full_refresh_nba, run_full_refresh_wnba, run_full_refresh_mlb, run_full_refresh_mma,
        run_full_refresh_tennis, run_full_refresh_soccer, run_full_refresh_valorant,
        run_full_refresh_cs2, run_full_refresh_lol, run_full_refresh_racing,
        run_full_refresh_cfb,
    ]
    # EVERY startup timer MUST be daemon, and must be cancelled on shutdown.
    #
    # threading.Timer inherits daemon status from the thread that CREATES it,
    # and this lifespan runs on the non-daemon MainThread -- so these were all
    # non-daemon, and threading._shutdown() joined every one of them on the way
    # out. Diagnosed live with py-spy (2026-08-03): after a reload or redeploy
    # the worker sat in _shutdown() waiting on a Timer thread parked inside a
    # Leaguepedia cargoquery (poller_lol.refresh_lol_results), which
    # poller_lock's own docstring already documents can burn 100+ seconds on
    # that API's rate limit. Meanwhile uvicorn's reloader PARENT still held the
    # listening socket, so connections were accepted instantly and then never
    # answered: every endpoint, /health included, hung for minutes while the
    # process sat at 0% CPU looking alive.
    #
    # Daemon threads are not joined at interpreter exit, so the process can die
    # immediately. cancel() additionally stops timers whose delay has not
    # elapsed yet -- without it a fresh worker's pollers would still fire out of
    # the dying process, doing pointless network I/O and DB writes.
    for i, poller in enumerate(pollers):
        _schedule(i * STARTUP_POLLER_STAGGER_SECONDS, poller)
    # Same reasoning for the catalog scan -- also establishes the "known
    # markets" baseline on a fresh DB right away rather than waiting up to
    # 24h for the first scheduled run (see scheduler.py::run_catalog_scan).
    _schedule(len(pollers) * STARTUP_POLLER_STAGGER_SECONDS, serialized(scheduler_module.run_catalog_scan))
    # Dead-market sanity check (see dead_market_sanity_check.py) -- extra
    # delay beyond the last poller's own stagger slot so its network calls
    # have actually had time to finish, not just start.
    _schedule((len(pollers) + 2) * STARTUP_POLLER_STAGGER_SECONDS, serialized(scheduler_module.run_sanity_check))
    yield
    # FIRST thing on the way out, before anything is torn down: raise the flag
    # that tells every self-HTTP loop to stop calling this server. Those loops
    # run on APScheduler pool threads, and concurrent.futures joins those
    # threads unconditionally at interpreter exit -- so a job still looping over
    # ~20 endpoints at a 90s timeout makes the worker literally unkillable and
    # the port stays held by a server that no longer answers. Full autopsy with
    # the py-spy stack in app/shutdown.py.
    shutdown.begin_shutdown()
    for timer in startup_timers:
        timer.cancel()
    scheduler_module.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Edge Finder", lifespan=lifespan)

    # ORDER MATTERS: Starlette applies the LAST-added middleware OUTERMOST.
    # ResponseCacheMiddleware is added FIRST (inner) so CORS wraps it -- a
    # cache HIT returns a bare Response (no CORS headers), and if the cache
    # were outer the browser would block every cached read as a CORS failure
    # ("could not reach backend"). With CORS outer, it stamps the
    # Access-Control-Allow-Origin header onto cached and live responses alike.
    app.add_middleware(ResponseCacheMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health(session: Session = Depends(get_session)):
        row = session.get(Setting, LAST_REFRESH_KEY)
        return {"status": "ok", "data_dir": settings.data_dir, "last_refresh_at": row.value if row else None}

    @app.post("/markets/refresh")
    def trigger_refresh():
        # Unwrapped for the same reason the startup pollers above are (see
        # lifespan()'s own comment) -- run_full_refresh's own sub-functions
        # already take db_write_lock() internally around just their writes;
        # wrapping the whole call here would re-block every OTHER sport's
        # scheduled refresh for this call's entire network I/O duration.
        threading.Thread(target=run_full_refresh, daemon=True).start()
        return {"status": "refresh triggered"}

    app.include_router(markets.router)
    app.include_router(nba_markets.router)
    app.include_router(wnba_markets.router)
    app.include_router(cfb_markets.router)
    app.include_router(mlb_markets.router)
    app.include_router(mma_markets.router)
    app.include_router(tennis_markets.router)
    app.include_router(soccer_markets.router)
    app.include_router(valorant_markets.router)
    app.include_router(cs2_markets.router)
    app.include_router(lol_markets.router)
    app.include_router(racing_markets.router)
    app.include_router(settings_router.router)
    app.include_router(catalog.router)
    app.include_router(placed_bets.router)
    app.include_router(backtests.router)
    app.include_router(health_router.router)

    return app


app = create_app()
