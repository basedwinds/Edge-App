"""Serializes DB-write access across this app's 9 sports' pollers.

ORIGINAL DESIGN (2026-07-19): `serialized()` wrapped each sport's ENTIRE
`run_full_refresh_<sport>()` call, at the call site only (main.py's startup
timers, scheduler.py's recurring jobs) -- fixed a real bug where 9 concurrent
pollers, each holding a SQLAlchemy session open across BOTH slow network I/O
(fetching Kalshi/Polymarket/free-data-source pages) AND the DB writes derived
from it, caused real `sqlite3.OperationalError: database is locked` errors
(confirmed live: even a trivial `/health` read hung 90+ seconds on a fresh
boot). Serializing whole poller RUNS eliminated that contention at the cost
of a longer, fully-sequential fresh-boot catch-up.

REAL PROBLEMS this original design caused, confirmed live 2026-07-20 (this
module's SECOND real incident, not counting the original bug it fixed):
  1. LoL's own `refresh_lol_matches()` can burn 100+ real seconds retrying
     Leaguepedia's rate limit -- while `run_full_refresh_lol()` ran (the
     WHOLE function, network I/O included, under the OLD wrapping), it held
     this lock the entire time, causing OTHER sports' own scheduled 5-minute
     refreshes to be skipped outright ("maximum number of running instances
     reached") purely because LoL was unlucky with an unrelated external API.
  2. Adding 3 new roster-change refresh functions (one per esports title)
     that ALSO held a session open across their own network fetch was enough
     EXTRA accumulated hold-time to exhaust the app's entire SQLAlchemy
     QueuePool outright (confirmed live: `sqlalchemy.exc.TimeoutError`,
     making even `/health` time out).

FIX (2026-07-20): moved the lock from wrapping each sport's WHOLE
`run_full_refresh_<sport>()` (removed from every call site in main.py/
scheduler.py) down to wrapping ONLY each individual `refresh_*()`
function's own DB-write block (open session -> write -> commit -> close),
via `db_write_lock()` below -- AFTER restructuring every `refresh_*()`
function across all 9 poller modules to do 100% of its own network I/O
FIRST, with no session open at all during that fetch. This preserves the
exact same original guarantee (only one sport's DB write is ever in flight
at a time, so SQLite write contention still can't happen) while letting
every sport's slow network I/O run fully concurrently -- the actual
correct fix, same one this module's own PRE-2026-07-20 docstring already
named as "the correct long-term fix... a real, larger refactor... not
attempted here" before it was finally done.

`serialized()` (the original decorator) is KEPT, now used only for the
non-per-sport background jobs (`run_catalog_scan`/`run_sanity_check`/
`run_surface_backfill`) that were never part of the per-sport contention
problem and don't need the finer-grained treatment."""
import functools
import threading

_POLLER_LOCK = threading.Lock()


def serialized(fn):
    """Wraps a callable so it acquires the shared app-wide poller lock
    before running and releases it after -- used at every call site
    (main.py's startup timers, scheduler.py's recurring jobs) for the
    non-per-sport background jobs. No longer used for the 9 sports' own
    `run_full_refresh_<sport>` -- see this module's own docstring for why."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _POLLER_LOCK:
            return fn(*args, **kwargs)

    return wrapper


def db_write_lock():
    """Returns the SAME shared lock `serialized()` uses, for direct `with
    poller_lock.db_write_lock():` use around just a `refresh_*()`
    function's own DB-write block -- see this module's own docstring for
    the full real-bug story on why this replaced the old whole-function
    wrapping. Callers MUST have already finished every network call before
    entering this block; the lock is non-reentrant (plain threading.Lock),
    so nesting it inside an already-locked scope would deadlock."""
    return _POLLER_LOCK
