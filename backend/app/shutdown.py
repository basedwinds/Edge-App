"""A single process-wide "we are shutting down" flag.

WHY THIS EXISTS -- a real hang, diagnosed live 2026-08-06 with py-spy against a
wedged worker that had burned 1,467s of CPU and then sat at exactly 0% while
holding 1.8GB, answering nothing on port 8756:

    Thread 5644 (idle): "MainThread"
        _wait_for_tstate_lock (threading.py:1169)
        join (threading.py:1149)
        _python_exit (concurrent/futures/thread.py:31)
    Thread 21632 (idle): "ThreadPoolExecutor-0_8"
        read (httpcore/_backends/sync.py:128)
        ...
        run_cache_warm (app/scheduler.py:100)

The worker was trying to EXIT (uvicorn --reload had picked up an edit). The
scheduler's cache-warm job was mid-flight, and that job self-HTTPs this very
server -- which, being mid-shutdown, will never answer. concurrent.futures
registers _python_exit as an atexit hook that joins every live pool thread
UNCONDITIONALLY, so scheduler.shutdown(wait=False) does not help at all: the
interpreter cannot exit until the job returns. Result: the old worker never
dies, the port stays bound by a server that no longer serves, and no new worker
can take over. The app looks frozen and a restart appears to do nothing.

That is almost certainly the same symptom reported earlier as "the app hasn't
updated in 1h despite restarting the front and back end" -- a restart cannot fix
it, because the process being restarted is the one that refuses to die.

The self-HTTP loops each iterate ~10-20 endpoints at a 90s timeout, so the
worst case was ~20 x 90s = half an hour of unkillable shutdown. Checking this
flag between requests bounds it to whatever single request is already in the
air, and skips the rest.

Deliberately dependency-free so the models/ modules that need it can import it
without creating a cycle back through scheduler.py.
"""
import threading

_shutting_down = threading.Event()


def begin_shutdown() -> None:
    """Called from the FastAPI lifespan's shutdown leg, BEFORE the scheduler is
    told to stop, so any job already running sees the flag on its next check."""
    _shutting_down.set()


def is_shutting_down() -> bool:
    return _shutting_down.is_set()


def reset_for_tests() -> None:
    _shutting_down.clear()
