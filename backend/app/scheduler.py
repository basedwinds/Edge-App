import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app import db_backup
from app import sports as app_sports
from app.db.database import SessionLocal
from app.ingestion import market_rules, tennis_best_of
from app.ingestion.catalog_scan import scan_catalog, wake_dormant_sports
from app.ingestion.poller import run_full_refresh
from app.ingestion.poller_soccer import refresh_mls_playoff_sim
from app.ingestion import soccer_xg_refresh
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

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor

log = logging.getLogger("scheduler")

# TWO EXECUTORS, and the second one is the whole point.
#
# BackgroundScheduler() with defaults gives ONE ThreadPoolExecutor of
# max_workers=10 and misfire_grace_time=1s. This app schedules 27 jobs, of which
# ~13 are minutes-long sport pollers. Sampled live 2026-08-15 with py-spy, all
# TEN slots were occupied by pollers simultaneously:
#
#   run_full_refresh (nfl)   run_full_refresh_nba   run_full_refresh_mlb
#   run_full_refresh_tennis  run_full_refresh_mma   run_full_refresh_soccer
#   run_full_refresh_valorant  run_full_refresh_cs2  run_full_refresh_lol
#   poller_lock.wrapper
#
# When cache_warm's turn came round with the pool full, APScheduler discarded
# the run as a MISFIRE -- the default grace is one second -- and said nothing.
# Observed directly: over 8 minutes past its due time, no pass had run and
# LAST_CACHE_WARM_PASS_SECONDS was still None.
#
# That is the dominant cause of the "Incomplete board" banner, and it is a
# bigger one than the pass-duration arithmetic in response_cache.py. Both are
# real: the pass (290s) genuinely does not fit the TTL (180s). But a pass that
# is merely too slow still refreshes every entry once per pass, whereas a pass
# that never RUNS refreshes nothing at all, for as long as the pollers stay
# busy. The serve-time start gate + STALE_SERVE_SECONDS handle the first; this
# handles the second.
#
# The warmer gets its own single-thread lane so poller saturation can never
# starve it again, and a generous misfire grace so a late fire still runs
# instead of being dropped. max_instances stays at the default 1, so passes
# still never overlap.
# THE WARMER WAS RESCUED FROM MISFIRES; THE POLLERS WERE NOT. The lane above
# fixed cache_warm, but every other job kept APScheduler's default
# misfire_grace_time of ONE SECOND -- so a poller that cannot grab a slot within
# a second of its due time is discarded silently, which is the same failure the
# comment above describes, on 26 other jobs.
#
# Measured live 2026-08-25 from /health's own MISSED_RUNS counter:
#     full_refresh_cod 30   full_refresh_cs2 26   full_refresh_racing 21
#     paper_log 18   tennis_best_of 16   wnba_season_sim 16   ... 27 jobs total
# On a 5-minute cadence, 26 misses is a large share of the day's runs.
#
# IT COSTS REAL DATA, not just tidiness. Liquipedia's CS2 page shows only
# recently-decided matches, so a dropped full_refresh_cs2 does not merely delay a
# result -- the match scrolls off the page and its map score is lost for good.
# CS2 map scores are missing on 93% of finished matches, which blinds
# series_handicap and series_total entirely.
#
# THIS DOES NOT RAISE CONCURRENCY. The pool is still capped at 10 and
# max_instances is still 1, so nothing new runs in parallel; a late job WAITS for
# a slot instead of being thrown away. Peak load is unchanged, utilisation is not.
#
# coalesce coalesces a backlog into ONE run rather than replaying every missed
# fire -- a poller reads current state, so running it five times in a row would
# just be five identical passes.
#
# 240s is deliberately under the 5-minute cadence of the busiest pollers: a run
# that cannot start within four minutes is better dropped in favour of the next
# fire than stacked behind it. The warmer keeps its own explicit 600 -- a per-job
# value overrides these defaults.
scheduler = BackgroundScheduler(
    executors={
        "default": ThreadPoolExecutor(max_workers=10),   # unchanged, explicit
        "warm": ThreadPoolExecutor(max_workers=1),
    },
    job_defaults={
        "misfire_grace_time": 240,
        "coalesce": True,
    },
)

# Missed runs, per job id. APScheduler drops a misfire SILENTLY, which is how a
# starved warmer went unnoticed; counting them makes it observable on /health.
MISSED_RUNS: dict[str, int] = {}


def _on_missed(event) -> None:
    MISSED_RUNS[event.job_id] = MISSED_RUNS.get(event.job_id, 0) + 1
    log.warning("scheduler: job %s MISSED its run (executor saturated or misfire "
                "grace exceeded) -- %d total since start", event.job_id, MISSED_RUNS[event.job_id])


scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)


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


# Duration of the most recent completed cache-warm pass, or None before the
# first one finishes. Read by /health; see run_cache_warm.
LAST_CACHE_WARM_PASS_SECONDS: float | None = None


def run_cache_warm():
    """Keeps the response cache (response_cache.py) warm by recomputing each
    heavy list endpoint off the request path, so real user requests -- the
    combined /all page fires ~10 at once -- always hit a fresh cached copy
    instead of a 5-31s recompute that contends with the pollers. Self-HTTPs
    the running server with the refresh header so even a still-fresh entry gets
    replaced (the cache never ages out under a user). Only logs; never raises.

    LOGS ITS OWN PASS DURATION, and warns when the pass no longer fits inside
    CACHE_TTL_SECONDS. This is the whole point: the sizing invariant
    (response_cache.CACHE_TTL_SECONDS > one full pass) was set against a
    MEASURED 61.7s pass, then quietly drifted to 548s as sports were added --
    9x over -- and nothing anywhere said so. Three paths
    (/markets/cross-platform-divergences, /soccer/futures, /tennis/futures) had
    also grown past the old 90s client timeout, so they were abandoned mid-
    compute and NEVER got a cached entry at all; every user request for them
    computed live. A number nobody measures is a number that rots.

    Note the effective refresh period is the PASS duration, not the job
    interval: APScheduler runs one instance at a time, so while a pass is longer
    than the interval, passes simply run back-to-back and each entry is
    refreshed once per pass."""
    import time

    import httpx

    from app.api.response_cache import CACHE_TTL_SECONDS, REFRESH_HEADER
    from app.shutdown import is_shutting_down

    started = time.monotonic()
    slowest: list[tuple[float, str]] = []
    try:
        # Was 90s, which was SHORTER than three of the endpoints it warms. A
        # timeout here doesn't just skip that path, it wastes the compute the
        # server already did, so it must sit above the slowest endpoint rather
        # than act as a throttle. The shutdown check below is what keeps a long
        # timeout from holding the interpreter open.
        with httpx.Client(timeout=240.0) as client:
            for path in _WARM_PATHS:
                # Stop issuing self-requests the moment shutdown starts -- the
                # server we are calling is the one going away, so every
                # remaining path would block for the full timeout and hold the
                # interpreter open. See app/shutdown.py for the full autopsy.
                if is_shutting_down():
                    log.info("cache warm: stopping early, shutdown in progress")
                    return
                t0 = time.monotonic()
                try:
                    client.get(f"http://127.0.0.1:8756{path}", headers={REFRESH_HEADER: "1"})
                except Exception:
                    log.warning("cache warm: %s did not complete -- it will have no cached entry", path)
                slowest.append((time.monotonic() - t0, path))
    except Exception:
        log.exception("cache warm failed")

    elapsed = time.monotonic() - started
    # Published so /health -- and through it board_artifact_scan's cache
    # freshness check -- can see whether the pass still fits inside
    # CACHE_TTL_SECONDS + STALE_SERVE_SECONDS. The sizing drifted unnoticed
    # twice (61.7s -> 548s caught by #159, then -> 290s caught by the banner);
    # a number nobody measures is a number that rots, so it is now reported
    # rather than left in a log line.
    global LAST_CACHE_WARM_PASS_SECONDS
    LAST_CACHE_WARM_PASS_SECONDS = elapsed
    slowest.sort(reverse=True)
    worst = ", ".join(f"{p} {s:.0f}s" for s, p in slowest[:5])
    if elapsed > CACHE_TTL_SECONDS:
        log.warning(
            "cache warm pass took %.0fs, LONGER than the %ds cache TTL -- entries expire "
            "before they are refreshed, so users compute live. Slowest: %s",
            elapsed, CACHE_TTL_SECONDS, worst)
    else:
        log.info("cache warm pass %.0fs (TTL %ds). Slowest: %s", elapsed, CACHE_TTL_SECONDS, worst)


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
        # Lock per BATCH, not for the whole run -- see snapshot_maintenance
        # .DEFAULT_BATCH_SIZE for why a single giant DELETE never finished.
        from app.ingestion.poller_lock import db_write_lock
        prune_market_snapshots(session, lock=db_write_lock)
    except Exception:
        log.exception("snapshot prune failed")
    finally:
        session.close()


def run_wal_checkpoint():
    """Force a WAL checkpoint. Only logs, never raises.

    WHY THIS EXISTS -- it is the root cause of the 2026-08-21 incident.

    A WAL checkpoint needs a moment with NO ACTIVE READERS. This app has nine
    sport pollers, a cache warmer and a frontend all reading more or less
    continuously, so SQLite's automatic checkpoint (wal_autocheckpoint=1000
    pages = 4.1MB) is perpetually deferred and the WAL simply grows until the
    process exits. Measured 2026-08-21: 59MB after fifteen minutes, 88MB after
    twenty-five -- roughly 20x past the level SQLite is trying to hold.

    That is the first link in the chain that destroyed the database:

        WAL grows unbounded (reached 297MB)
          -> process is hard-killed
          -> 297MB of unrecovered WAL has to be replayed
          -> replay damages page 1, SQLite then believes the DB is 5914 pages
          -> it writes a COHERENT 24MB file over a 7.78GB one
          -> the tracker reads empty and 655 real bets are gone

    Every link needs the one before it. Bound the WAL and a hard kill costs a
    few seconds of snapshots instead of the database.

    TRUNCATE, not PASSIVE. PASSIVE never blocks but also does nothing when a
    reader is attached, which is exactly the situation that created this. But
    TRUNCATE CANNOT BE FORCED EITHER -- it returns busy=1 rather than waiting
    forever, and that is fine: this runs on a timer, so a blocked attempt just
    means the next one tries again. What matters is that it REPORTS the busy
    case instead of logging a success it did not achieve.

    Takes the write lock so it is not competing with a poller mid-commit, but
    note the lock does not evict READERS -- it cannot, and that is why busy is
    an expected outcome rather than an error.
    """
    from pathlib import Path
    from app.config import settings
    from app.ingestion.poller_lock import db_write_lock
    from app.db.database import engine

    wal = Path(settings.sqlite_url().replace("sqlite:///", "") + "-wal")
    before = wal.stat().st_size if wal.exists() else 0
    try:
        with db_write_lock():
            with engine.connect() as conn:
                row = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        after = wal.stat().st_size if wal.exists() else 0
        busy = row[0] if row else None
        if busy:
            log.warning("wal checkpoint BUSY (readers attached) -- WAL still %.1fMB, will retry",
                        after / 1e6)
        else:
            log.info("wal checkpoint ok: %.1fMB -> %.1fMB (%d pages)",
                     before / 1e6, after / 1e6, (row[1] if row and row[1] is not None else -1))
    except Exception:
        log.exception("wal checkpoint failed")


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
        from app.models.catalog_resolution import auto_close_ingested, auto_resolve_flagged
        summary = auto_resolve_flagged(session)
        if summary["resolved"]:
            log.info("catalog auto-resolve closed: %s", "; ".join(summary["resolved"]))
        # And close the UNTRIAGED entries whose series is already ingesting.
        # Without this the New Markets queue only grows -- 167 -> 259 in a single
        # day on 2026-08-13, with today's 75 CFB win totals still queued while
        # 420 of their markets were live. Same DB-only, no-self-HTTP contract as
        # the call above.
        closed = auto_close_ingested(session)
        if closed["closed"]:
            log.info("catalog auto-close: %d untriaged entries already ingested (%d -> %d open)",
                     len(closed["closed"]), closed["open_before"], closed["open_after"])
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




def _refresh_tennis_best_of_job():
    """Session wrapper for tennis_best_of.refresh_best_of."""
    session = SessionLocal()
    try:
        tennis_best_of.refresh_best_of(session)
    except Exception:
        log.exception("tennis_best_of job failed")
    finally:
        session.close()


def _refresh_market_rules_job():
    """Session wrapper for market_rules.refresh_market_rules."""
    session = SessionLocal()
    try:
        market_rules.refresh_market_rules(session)
    except Exception:
        log.exception("market_rules job failed")
    finally:
        session.close()


def start():
    # next_run_time is set to now+interval, not None: passing None tells APScheduler to
    # add the job paused (per its add_job docstring), which means it never fires again on
    # its own. The explicit startup thread in main.py handles the first run of each poller.
    base_tick = datetime.now() + timedelta(minutes=5)
    # MLS playoff Monte Carlo -- 10,000 sims + ~10 live ESPN calls per run, for
    # a league table that moves at most daily. Its own slow job precisely
    # because it used to ride the 5-minute soccer refresh and pinned a core.
    # UNDERSTAT xG REFRESH (#203). The soccer ratings blend xG into the
    # attack/defence residual for E0/SP1/D1/I1/F1 (w=0.50, see
    # baseline/soccer_xg.py). Its cache was a one-off crawl, and ratings update
    # after every match -- so WITHOUT this the blend degrades to pure goals for
    # each new fixture and the improvement decays SILENTLY through a season.
    # Silent decay is the reason this is automated; the gain itself is 0.30%
    # logloss on 5 of 33 leagues.
    #
    # WEEKLY IS PLENTY. Understat publishes a season-long rate, not a live feed,
    # and a refresh is five requests. It also rebuilds the alias map, which is
    # the part that would rot quietly -- promoted clubs arrive each August with
    # names the map has never seen, and an unmapped club falls back to pure
    # goals for its whole season with no error anywhere.
    # WEEKLY DATABASE SNAPSHOT (app/db_backup.py). app.db is the only record of
    # what this app predicted and how it resolved, it is gitignored because the
    # repo is public, so GitHub is not a backup. The one snapshot that existed
    # before this job was taken by hand and had 1,600 bets accrue past it,
    # which quietly made it unrestorable without discarding a day of results.
    #
    # LAST IN THE STAGGER, deliberately: it reads ~7GB and takes ~25s, so it
    # runs after every poller has had its slot rather than alongside them.
    # Verifies before it rotates and keeps 2 -- see the module docstring.
    # MARKET RESOLUTION TERMS (app/ingestion/market_rules.py). Backfills Kalshi's
    # rules_primary/rules_secondary onto markets that have none, so questions
    # about what a contract actually PAYS ON are answerable from stored data.
    # Two such questions in one day were pure guesswork without it, and one of
    # them ("does 15+ wins include playoffs?") flips a model verdict depending
    # on the answer.
    #
    # HOURLY AND CHEAP. It only touches markets with no rules stored, in batches
    # of 100 tickers, so once the backlog clears a run is a single query. New
    # markets arrive continuously, which is why it repeats rather than being a
    # one-off backfill.
    # TENNIS FORMAT (app/ingestion/tennis_best_of.py). Derives best-of-3 vs
    # best-of-5 from the BOOK'S OWN inventory, because no metadata field carries
    # it -- Kalshi labels a Slam qualifying match identically to the main draw.
    # Guessing it from the tournament name flagged 80 qualifiers as Bo5 and moved
    # expected total games from ~22 to ~38, which manufactured +48 to +72pp of
    # edge and staked 36 bets against liquid books.
    #
    # EVERY 15 MINUTES, not hourly. The inventory arrives with the markets, and
    # until it does a match keeps the Bo3 default -- which is right for the vast
    # majority but WRONG for a Grand Slam main draw, where it would under-state
    # expected games by ~16 and tilt the model toward false UNDERs. Kalshi lists
    # these markets days ahead so the window is small in practice, but the job is
    # a single query pass when nothing has changed, so a tighter interval is
    # nearly free insurance.
    scheduler.add_job(
        _refresh_tennis_best_of_job,
        "interval",
        minutes=15,
        id="tennis_best_of",
        next_run_time=base_tick + timedelta(seconds=14 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        _refresh_market_rules_job,
        "interval",
        hours=1,
        id="market_rules",
        next_run_time=base_tick + timedelta(seconds=13 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        db_backup.run_backup,
        "interval",
        days=7,
        id="db_backup",
        next_run_time=base_tick + timedelta(seconds=12 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
    scheduler.add_job(
        soccer_xg_refresh.refresh,
        "interval",
        days=7,
        id="soccer_xg_refresh",
        next_run_time=base_tick + timedelta(seconds=9 * JOB_STAGGER_SECONDS),
        replace_existing=True,
    )
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
    # Kalshi resolution for the forward log, on its OWN cadence. settle() above
    # is daily because its local pass walks every pending observation; this one
    # is cheap (~0.5s per 100-ticker batch) and time-sensitive, because a market
    # resolves when its event ends and until this runs the forward log
    # understates coverage.
    #
    # Four-hourly rather than daily is what actually drains the backlog: at one
    # run a day the 23k pending rows would have taken six days. First run is
    # early (8 min) so a restart starts closing the gap rather than waiting.
    scheduler.add_job(
        observation_logger.settle_from_kalshi,
        "interval",
        hours=4,
        id="observation_settle_kalshi",
        next_run_time=base_tick + timedelta(minutes=8),
        replace_existing=True,
    )
    # Polymarket half of the same gap. Staggered 4 minutes off the Kalshi one so
    # the two never contend for the same worker in a thread pool that is already
    # the app's tightest resource.
    # WHO HOLDS THE WRITE LOCK. Four rounds of poller tuning found nothing
    # because the pollers are not the cost -- soccer does 3-8s of work and
    # queues up to ten minutes. This logs total hold time per caller so the
    # actual holder names itself instead of being guessed at one module at a
    # time. Cheap: it reads an in-memory dict.
    def _log_lock_report():
        try:
            from app.ingestion.poller_lock import lock_report
            log.info("%s", lock_report())
        except Exception:
            log.exception("lock report failed")

    scheduler.add_job(
        _log_lock_report,
        "interval",
        minutes=10,
        id="lock_report",
        next_run_time=base_tick + timedelta(minutes=6),
        replace_existing=True,
    )
    scheduler.add_job(
        observation_logger.settle_from_polymarket,
        "interval",
        hours=4,
        id="observation_settle_polymarket",
        next_run_time=base_tick + timedelta(minutes=12),
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
    # run_wal_checkpoint IS DELIBERATELY NOT REGISTERED (2026-08-21).
    #
    # It was added the same day to bound WAL growth, ran for ~3 hours on a
    # 5-minute interval, and the database was found corrupt afterwards -- damage
    # in the market_snapshots b-trees, on a file that had passed a FULL
    # integrity_check at 11:58 before the job went live. That is not proof:
    # TRUNCATE is a standard operation and the app was also crash-looping in the
    # same window, which is the hazard that produced the original 297MB WAL. But
    # a change went in, damage followed, and the app ran for weeks without it.
    #
    # The function is kept, unregistered, so the reasoning is not lost. Before
    # re-enabling it, shrink the database first (68.5M market_snapshots rows is
    # the real fragility) and re-test on a DB small enough to verify quickly.
    scheduler.add_job(
        run_snapshot_prune,   # takes the lock per batch itself, must NOT be serialized()
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
        # Its OWN executor -- see the scheduler construction above. On the
        # shared pool this job was silently starved by the 13 sport pollers.
        executor="warm",
        # The default grace is ONE SECOND, which is what turned a busy moment
        # into a dropped pass. 600s means a late fire still runs; coalesce
        # (default True) keeps a backlog from becoming a burst of passes.
        misfire_grace_time=600,
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
