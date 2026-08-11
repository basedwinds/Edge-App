import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app import sports as app_sports
from app.db.database import SessionLocal
from app.ingestion.catalog_scan import scan_catalog, wake_dormant_sports
from app.ingestion.poller import run_full_refresh
from app.ingestion.poller_soccer import refresh_mls_playoff_sim
from app.models import observation_logger
from app.ingestion.poller_nba import run_full_refresh_nba
from app.ingestion.poller_mlb import run_full_refresh_mlb
from app.ingestion.poller_tennis import run_full_refresh_tennis
from app.ingestion.poller_mma import run_full_refresh_mma
from app.ingestion.poller_soccer import run_full_refresh_soccer
from app.ingestion.poller_valorant import run_full_refresh_valorant
from app.ingestion.poller_cod import run_full_refresh_cod
from app.ingestion.poller_cs2 import run_full_refresh_cs2
from app.ingestion.poller_lol import run_full_refresh_lol
from app.ingestion.poller_racing import run_full_refresh_racing
from app.ingestion.poller_cfb import run_full_refresh_cfb
from app.ingestion.poller_wnba import refresh_wnba_season_sim, run_full_refresh_wnba
from app.ingestion.poller_lock import serialized
from app.models.dead_market_sanity_check import run_dead_market_sanity_check
from app.models.snapshot_maintenance import prune_market_snapshots
from app.models.market_cleanup import close_stale_game_markets, reconcile_vanished_market_status
from app.models.tennis_surface_backfill import run_tennis_surface_backfill

log = logging.getLogger("scheduler")

scheduler = BackgroundScheduler()


def run_stuck_bet_check():
    """Reports pending bets whose event finished long ago -- the shared symptom
    of every settlement/timing bug found 2026-08-03, each of which was spotted by
    the user rather than the app. Only logs; never raises."""
    session = SessionLocal()
    try:
        from app.models.stuck_bet_check import report_stuck_bets
        report_stuck_bets(session)
    except Exception:
        log.exception("stuck-bet check crashed")
    finally:
        session.close()


def run_sanity_check():
    """Catches the "dead/decided market shown as live" bug class (see
    dead_market_sanity_check.py's own docstring) -- runs after the price
    pollers so it's checking freshly-refreshed data, not stale rows from
    before this tick. Only logs; never raises out to the scheduler."""
    try:
        run_dead_market_sanity_check()
    except Exception:
        log.exception("dead-market sanity check crashed")


def run_surface_backfill():
    """Real surface (tennisexplorer.com) doesn't change once a tournament
    starts, so once/day is plenty -- same cadence reasoning as
    run_catalog_scan, not the 5-minute price pollers. Only logs/commits;
    never raises out to the scheduler (see tennis_surface_backfill.py's own
    docstring for what this actually does and its known partial-coverage
    limitation)."""
    try:
        run_tennis_surface_backfill()
    except Exception:
        log.exception("tennis surface backfill crashed")


_WARM_PATHS = [
    # Every sport's markets endpoint, DERIVED from app.sports rather than listed
    # by hand -- this list and paper_logger's drifted apart once already.
    *app_sports.MARKETS_PATHS,
    "/markets/cross-platform-divergences",
    # Every sport's FUTURES endpoint, also DERIVED -- for the same reason as the
    # markets paths, and after the same failure. These run a real model (tennis
    # draw sim, esports tournament Monte Carlo, team-sport season sims, racing
    # championship sim), so warming them keeps a cold-model compute from being
    # cached and served stale -- the exact bug class the startup Elo warm and
    # this warmer both exist to kill.
    #
    # This used to be seven paths typed by hand and it was missing five:
    # /wnba, /cfb, /lol, /mma and /racing. Unwarmed, each computed live on the
    # user's request (racing measured 21.9s) and then cached whatever it built
    # -- including an all-unpriced payload, when the request landed before the
    # racing poller had warmed the championship cache. Racing futures could
    # therefore sit blank for the full TTL with nothing indicating why.
    *app_sports.FUTURES_PATHS,
]


def run_cache_warm():
    """Keeps the response cache (response_cache.py) warm by recomputing each
    heavy list endpoint off the request path, so real user requests -- the
    combined /all page fires ~10 at once -- always hit a fresh cached copy
    instead of a 5-31s recompute that contends with the pollers. Self-HTTPs
    the running server with the refresh header so even a still-fresh entry gets
    replaced (the cache never ages out under a user). Only logs; never raises."""
    import httpx

    from app.api.response_cache import REFRESH_HEADER
    from app.shutdown import is_shutting_down

    try:
        with httpx.Client(timeout=90.0) as client:
            for path in _WARM_PATHS:
                # Stop issuing self-requests the moment shutdown starts -- the
                # server we are calling is the one going away, so every
                # remaining path would block for the full 90s and hold the
                # interpreter open. See app/shutdown.py for the full autopsy.
                if is_shutting_down():
                    log.info("cache warm: stopping early, shutdown in progress")
                    return
                try:
                    client.get(f"http://127.0.0.1:8756{path}", headers={REFRESH_HEADER: "1"})
                except Exception:
                    pass  # a slow/failing endpoint shouldn't stop warming the rest
    except Exception:
        log.exception("cache warm failed")


def run_paper_log_job():
    """Auto-log the app's current recommendations as paper bets so forward CLV
    accrues (see paper_logger.py). Only logs; never raises out to the
    scheduler. Self-HTTP + a quick DB write, like the cache warmer."""
    try:
        from app.models.paper_logger import run_paper_log
        run_paper_log()
    except Exception:
        log.exception("paper log job crashed")


def run_futures_history():
    """Hourly -- samples each futures leg's MODEL probability so the UI can show
    how the model and the market moved against each other (see
    models/futures_history.py). Self-HTTP + a small write; only logs."""
    try:
        from app.models.futures_history import record_futures_probs
        session = SessionLocal()
        try:
            record_futures_probs(session)
        finally:
            session.close()
    except Exception:
        log.exception("futures history job crashed")


def run_snapshot_prune():
    """Daily -- caps MarketSnapshot growth (see snapshot_maintenance.py). Keeps
    the last 14 days + each market's latest; only logs, never raises."""
    session = SessionLocal()
    try:
        prune_market_snapshots(session)
    except Exception:
        log.exception("snapshot prune failed")
    finally:
        session.close()


def run_market_cleanup():
    """Daily -- marks past-date game markets 'closed' so Kalshi markets that
    dropped off the open list don't linger as 'active' forever (see
    market_cleanup.py), then asks Kalshi directly about the ones whose ticker
    carries no date. Only logs, never raises.

    The second pass exists because the first cannot see futures: their tickers
    hold a season or event code rather than a date, so a settled tournament
    stayed 'active' indefinitely and kept being offered as a bet."""
    session = SessionLocal()
    try:
        close_stale_game_markets(session)
    except Exception:
        log.exception("market cleanup failed")
    try:
        reconcile_vanished_market_status(session)
    except Exception:
        log.exception("market status reconcile failed")
    finally:
        session.close()



def refresh_exposure_snapshot():
    """Rebuild the bankroll-capacity snapshot that the sizing caps read.

    It was previously rebuilt ONLY when the Settings page was saved. After a
    restart the snapshot is empty, and an empty snapshot makes
    remaining_for_unit_scale return None -- which every caller treats as
    UNCAPPED. So the exposure ceilings did nothing at all until someone happened
    to open Settings and press save. Refreshed on the poll cycle instead, so
    they hold from startup.
    """
    from app.models import exposure
    from app.api.routers.settings import (
        BANKROLL_KEY, DEFAULT_BANKROLL, FUTURES_EXPOSURE_CAP_PCT_KEY,
        GAME_EXPOSURE_CAP_PCT_KEY, _get_float,
    )
    session = SessionLocal()
    try:
        exposure.refresh_snapshot(
            session,
            _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL),
            _get_float(session, FUTURES_EXPOSURE_CAP_PCT_KEY, exposure.DEFAULT_FUTURES_EXPOSURE_CAP_PCT),
            _get_float(session, GAME_EXPOSURE_CAP_PCT_KEY, exposure.DEFAULT_GAME_EXPOSURE_CAP_PCT),
        )
    except Exception:  # a cap must never be able to break pricing
        log.exception("exposure snapshot refresh failed; sizing continues uncapped")
    finally:
        session.close()


def run_catalog_scan():
    """Daily, not every-5-min like run_full_refresh -- this hits Kalshi's
    full /series?category=Sports and Polymarket's full NFL event list
    (hundreds of items), unlike the targeted per-series calls the 5-minute
    price refresh makes, so it doesn't need to run nearly as often."""
    session = SessionLocal()
    try:
        scan_catalog(session)
        # A sport that was dismissed while between seasons must not stay
        # dismissed once it relists -- see wake_dormant_sports.
        wake_dormant_sports(session)
        session.commit()
    except Exception:
        log.exception("catalog scan failed")
        # ROLL BACK BEFORE REUSING THE SESSION. A failed commit leaves the
        # transaction in a broken state, and every later query on it raises
        # PendingRollbackError rather than the original error -- so the scan's
        # IntegrityError silently took auto_resolve_flagged down with it, and
        # the second traceback pointed at an innocent query. Two jobs dead from
        # one fault, and the second failure actively misleading.
        try:
            session.rollback()
        except Exception:
            log.exception("catalog scan rollback failed")
    try:
        # Close flagged entries whose build has since shipped. Runs with the
        # scan because that is when the catalog picture is freshest, and it is
        # DB-only (no self-HTTP -- see app/shutdown.py). Without it the backlog
        # rots: 8 of 48 entries were describing finished work by 2026-08-07.
        from app.models.catalog_resolution import auto_resolve_flagged
        summary = auto_resolve_flagged(session)
        if summary["resolved"]:
            log.info("catalog auto-resolve closed: %s", "; ".join(summary["resolved"]))
    except Exception:
        log.exception("catalog auto-resolve failed")
    finally:
        session.close()


#  Same next_run_time for every 5-minute job meant all five pollers (plus
# run_catalog_scan's own daily tick, whenever it happens to land near one)
# hit the same SQLite file at once on every recurring tick, not just at
# startup -- reproduced "database is locked" errors that killed poller
# threads mid-refresh even with WAL mode + a busy_timeout configured
# (2026-07-18). IntervalTrigger just adds the interval to the previous
# run time, so staggering the *first* next_run_time keeps every later tick
# staggered too, not just the first one. Kept even after the 2026-07-20
# poller_lock.py fix (whole-function serialized() removed from the 9
# sports' own jobs below) purely to avoid every sport hammering its own
# external API at the exact same instant -- no longer needed to prevent DB
# contention, that's poller_lock.py::db_write_lock's job now.
JOB_STAGGER_SECONDS = 20


def start():
    # next_run_time is set to now+interval, not None: passing None tells APScheduler to
    # add the job paused (per its add_job docstring), which means it never fires again on
    # its own. The explicit startup thread in main.py handles the first run of each poller.
    base_tick = datetime.now() + timedelta(minutes=5)
    # MLS playoff Monte Carlo -- 10,000 sims + ~10 live ESPN calls per run, for
    # a league table that moves at most daily. Its own slow job precisely
    # because it used to ride the 5-minute soccer refresh and pinned a core.
    scheduler.add_job(
        refresh_mls_playoff_sim,
        "interval",
        hours=6,
        id="mls_playoff_sim",
        next_run_time=base_tick + timedelta(minutes=3),
        replace_existing=True,
    )

    # Forward observation log: one row per priced market, scored later on
    # OUTCOMES. Runs ONCE DAILY and deliberately LAST in the tick order --
    # it reads every sport's market route, and those routes price off model
    # caches the pollers fill. Run cold, soccer/mma/cfb/tennis return every
    # market with model_prob=None and the log silently covers a third of the
    # app (measured while building it: 8 of 12 sports logged nothing). Daily
    # rather than 5-minutely because it re-prices all 12 sports, and a
    # once-a-day pre-event view is what the scoring needs anyway.
    # HOURLY, not daily. REAL BUG this fixes (2026-08-08): at 24h this fired
    # once, in the evening, so any event starting earlier in the day was
    # captured AFTER it had already happened. For racing especially -- cards run
    # afternoons -- that meant the "forward" log was recording post-hoc prices
    # for most of the field, which is worthless for the one question it exists
    # to answer. A user's 5pm NASCAR picks had no pre-race record at all.
    #
    # refresh() is idempotent (upsert per market_id) and already refuses to
    # touch a row once event_start <= now, so extra runs cannot overwrite a
    # pre-event view with a mid-event one -- they can only ADD coverage for
    # events that had not started yet. That freeze is what makes a higher
    # frequency safe rather than merely tolerable.
    #
    # Hourly bounds worst-case staleness at 1h before kickoff instead of 24h.
    # Cost is bounded too: it reads each sport's market route, and those are
    # served from the 180s response cache that the cache warmer keeps hot, so
    # this is mostly cache reads rather than 12 full recomputes.
    scheduler.add_job(
        refresh_exposure_snapshot,
        "interval",
        minutes=5,
        id="exposure_snapshot",
        next_run_time=base_tick + timedelta(seconds=30),
        replace_existing=True,
    )
    scheduler.add_job(
        observation_logger.refresh,
        "interval",
        hours=1,
        id="observation_log",
        next_run_time=base_tick + timedelta(minutes=20),
        replace_existing=True,
    )
    scheduler.add_job(
        observation_logger.settle,
        "interval",
        hours=24,
        id="observation_settle",
        next_run_time=base_tick + timedelta(minutes=35),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh,
        "interval",
        minutes=5,
        id="full_refresh",
        next_run_time=base_tick,
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_nba,
        "interval",
        minutes=5,
        id="full_refresh_nba",
        next_run_time=base_tick + timedelta(seconds=JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_mlb,
        "interval",
        minutes=5,
        id="full_refresh_mlb",
        next_run_time=base_tick + timedelta(seconds=2 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_tennis,
        "interval",
        minutes=5,
        id="full_refresh_tennis",
        next_run_time=base_tick + timedelta(seconds=3 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_mma,
        "interval",
        minutes=5,
        id="full_refresh_mma",
        next_run_time=base_tick + timedelta(seconds=4 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_soccer,
        "interval",
        minutes=5,
        id="full_refresh_soccer",
        next_run_time=base_tick + timedelta(seconds=5 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_valorant,
        "interval",
        minutes=5,
        id="full_refresh_valorant",
        next_run_time=base_tick + timedelta(seconds=6 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_cs2,
        "interval",
        minutes=5,
        id="full_refresh_cs2",
        next_run_time=base_tick + timedelta(seconds=7 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    # Call of Duty. Registered HERE and not only in main.py's startup timer
    # list -- those timers fire once per process, so without this CoD would
    # refresh at boot and then never again, which is the "dead scheduler job"
    # shape this app has already shipped once.
    #
    # next_run_time is set explicitly for the same reason: a job added without
    # one does not fire at all here.
    #
    # Slot 15 because 0-14 are taken. breakingpoint.gg is a small site and this
    # is a 5-minute cadence, so cod_data paces itself internally too -- the
    # Liquipedia ban earlier today is the standing reminder of what a shared
    # host costs when a poller is impolite.
    scheduler.add_job(
        run_full_refresh_cod,
        "interval",
        minutes=5,
        id="full_refresh_cod",
        next_run_time=base_tick + timedelta(seconds=15 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    # LoL's own Leaguepedia Cargo API applies a real rate limit to anonymous
    # requests (confirmed live 2026-07-19 -- see lol_data.py's own
    # docstring), stricter than every other sport's free data source here.
    # Sharing the same 5-minute cadence as everything else risks tripping
    # that limit on every single tick -- given 5 was already the standing
    # default for every sport in this app and no live evidence yet shows
    # whether 5 minutes alone is too fast, this stays at 5 for now with the
    # rate limit's own graceful failure handling (poller_lol.py::
    # refresh_lol_matches) as the real safety net; revisit with a longer
    # LoL-specific interval if production polling shows it tripping often.
    scheduler.add_job(
        run_full_refresh_lol,
        "interval",
        # 30, not 5. lol_data.py's own docstring already prescribed this: "the fix
        # is a longer poller interval for LoL specifically, not a workaround around
        # the limit". At 5 minutes this hit Leaguepedia's cargoquery ~288x/day and
        # the limit is now permanently tripped -- refresh_lol_results raises every
        # cycle, so 0 of 223 played LoL matches have a result and 353 bets cannot
        # settle. It also stalls OTHER sports: run_full_refresh_lol holds the shared
        # cross-sport poller lock while burning 100+ seconds on 20/30/45/68s retries.
        # Kalshi/Polymarket prices for LoL come from the same job, so this trades
        # some price freshness for having results at all -- results are the harder
        # half, and without them nothing settles and no CLV accrues.
        minutes=30,
        id="full_refresh_lol",
        next_run_time=base_tick + timedelta(seconds=8 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        run_full_refresh_racing,
        "interval",
        minutes=5,
        id="full_refresh_racing",
        next_run_time=base_tick + timedelta(seconds=9 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    # CFB on a 15min interval, not the 5min the other sports use. The schedule
    # fetch is one ESPN call PER DAY across a 90-day window (a date-RANGE query
    # silently returns a subset -- see espn_cfb_client), so a 5min cadence would
    # be ~90 calls every 5 minutes for a schedule that changes weekly. Market
    # prices still refresh inside each run.
    scheduler.add_job(
        run_full_refresh_cfb,
        "interval",
        minutes=15,
        id="full_refresh_cfb",
        next_run_time=base_tick + timedelta(seconds=12 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    # WNBA had NO recurring job at all until 2026-08-03 -- every other sport has
    # one, and this was simply never added when WNBA was integrated. The sport
    # refreshed exactly ONCE, from main.py's startup timer, and then never again:
    # market prices went stale immediately (so no closing-line capture, hence no
    # CLV), placed bets never auto-settled, and the season sim was attempted a
    # single time. That last one is how this was found -- the win-total rows sat
    # on "Season simulation not warm yet" indefinitely, and the retry added to
    # season_sim_wnba.warm() could never fire because nothing ever called warm()
    # a second time.
    #
    # 15min rather than 5min for the same reason as CFB above: refresh_wnba_games
    # is ~124 sequential ESPN calls (one per day of the season window), which is
    # far too heavy to repeat every 5 minutes for a schedule that barely changes.
    scheduler.add_job(
        run_full_refresh_wnba,
        "interval",
        minutes=15,
        id="full_refresh_wnba",
        next_run_time=base_tick + timedelta(seconds=14 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    # The WNBA season sim gets its OWN job rather than living inside
    # run_full_refresh_wnba. It only needs Elo plus its own ESPN season fetch,
    # and inside the chain it sat behind refresh_wnba_games -- ~124 sequential
    # ESPN calls that py-spy caught still running nine minutes into an
    # undisturbed boot, which left all 45 win-total rows unpriced indefinitely.
    # Decoupled, a slow games fetch can no longer block pricing.
    #
    # 20min against the sim's own 1h cache TTL, so a failed run (which expires
    # after _FAILURE_TTL) gets retried well before the hour is up.
    scheduler.add_job(
        refresh_wnba_season_sim,
        "interval",
        minutes=20,
        id="wnba_season_sim",
        next_run_time=datetime.now() + timedelta(seconds=90),
        replace_existing=True,
    )
    # Hourly: cheap DB-only scan, no network, no per-sport knowledge.
    scheduler.add_job(
        run_stuck_bet_check,
        "interval",
        hours=1,
        id="stuck_bet_check",
        next_run_time=datetime.now() + timedelta(seconds=150),
        replace_existing=True,
    )
    # REAL BUG these next_run_times fix (found 2026-08-04 chasing "tennis keeps
    # vanishing from Recommended"). Registered without next_run_time, an interval
    # job's first fire is one FULL interval after start() -- so a 6h/24h
    # housekeeping job only ever runs if the process stays up that long. This
    # backend is restarted far more often than that, so all three had effectively
    # NEVER run. market_cleanup is the one that bit: it marks past-date game
    # markets 'closed', and without it 25,071 of tennis's 26,306 "active" markets
    # were finished matches. That pushed tennis past 32,766 total markets --
    # SQLite's host-variable cap -- so /tennis/markets raised "too many SQL
    # variables" and 500'd, and the frontend's guard() turned that into an empty
    # tennis list. The pollers dodge this only because main.py runs them once
    # explicitly at startup; these have no such thread, so they need the first
    # run scheduled here. Staggered, and after the initial poll burst, so
    # housekeeping never competes with the first price refresh for the write lock.
    housekeeping_tick = datetime.now() + timedelta(minutes=8)
    scheduler.add_job(
        serialized(run_futures_history),
        "interval",
        hours=1,
        id="futures_history",
        next_run_time=housekeeping_tick + timedelta(seconds=3 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        serialized(run_catalog_scan),
        "interval",
        hours=24,
        id="catalog_scan",
        next_run_time=housekeeping_tick + timedelta(seconds=2 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        serialized(run_snapshot_prune),
        "interval",
        hours=24,
        id="snapshot_prune",
        next_run_time=housekeeping_tick + timedelta(seconds=JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        serialized(run_market_cleanup),
        "interval",
        hours=6,
        id="market_cleanup",
        next_run_time=housekeeping_tick,
        replace_existing=True,
    )
    # Keep the response cache warm (see run_cache_warm). Every 200s < the 300s
    # cache TTL, so entries never expire under a user. NOT serialized -- it's
    # read-only self-HTTP, no DB write lock needed -- and staggered to start
    # after the first poll cycle has populated markets.
    scheduler.add_job(
        run_cache_warm,
        "interval",
        # Paired with response_cache.CACHE_TTL_SECONDS (180) and a MEASURED
        # 61.7s full pass: 90 + 62 < 180, so an entry is always refreshed before
        # it can expire. At the old 200s against a 60s TTL the cache sat empty
        # ~140s of every cycle, which is what made tennis flicker in and out of
        # Recommended. Change these two together.
        seconds=90,
        id="cache_warm",
        next_run_time=base_tick + timedelta(seconds=11 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    # Auto paper-trading logger (see paper_logger.py) -- snapshots the current
    # recommendations as paper bets so forward CLV accrues, AND fires the Discord
    # new-bet alerts.
    #
    # Every 5min, matched to the price pollers above. This was 30min, which meant
    # a qualifying bet could sit up to half an hour before pinging -- reported
    # live: bets settle, replacements surface with their event starting soon, and
    # the alert arrived too late to act on. 5min is the useful FLOOR, not a
    # throttle: the underlying markets only refresh every 5min, so alerting more
    # often than that cannot surface anything newer.
    #
    # Safe to run this often because the dedupe is by persisted state, not by
    # timing: open_ids skips any market already logged and alerted_keys skips any
    # cross-platform sibling already announced, both rebuilt from open paper bets
    # every run, so they survive restarts. More frequent runs therefore catch new
    # bets SOONER and never re-announce one.
    #
    # Staggered well after startup so pricing + Elo are warm first. Not
    # serialized() -- it takes db_write_lock() itself only around its quick
    # write, after the (network) self-HTTP, same as the pollers.
    scheduler.add_job(
        run_paper_log_job,
        "interval",
        minutes=5,
        id="paper_log",
        next_run_time=base_tick + timedelta(seconds=13 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    # Runs every 10 minutes (not every 5, like the price pollers -- this is a
    # detection aid, not something that needs to react within one poll cycle)
    # staggered to start after every sport's first refresh has had a chance
    # to land, so the very first run isn't checking empty/half-refreshed data.
    scheduler.add_job(
        serialized(run_sanity_check),
        "interval",
        minutes=10,
        id="dead_market_sanity_check",
        next_run_time=base_tick + timedelta(seconds=9 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        serialized(run_surface_backfill),
        "interval",
        hours=24,
        id="tennis_surface_backfill",
        next_run_time=base_tick + timedelta(seconds=10 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.start()


def stop():
    scheduler.shutdown(wait=False)
