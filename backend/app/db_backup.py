"""Weekly snapshot of the SQLite database, with rotation.

WHY THIS EXISTS. app.db holds the entire bet tracker -- the only record of what
this app has actually predicted and how it turned out, and the yardstick every
model decision is judged against. It is deliberately gitignored (the repo is
public), so GitHub is NOT a backup. Before this job the only snapshot in
existence was a one-off taken by hand before a fixture merge, and by the time
anyone looked at it 1,600 bets had accrued past it -- meaning it had quietly
stopped being restorable without discarding a day of results.

USES THE SQLITE BACKUP API, NOT A FILE COPY. The server writes continuously and
runs in WAL mode with a write-ahead log that has been observed at ~300MB. `cp`
of a live database in that state can capture a torn page set: the .db file and
the -wal disagree, and nothing tells you until a restore fails. Connection
.backup() coordinates with the writer and produces a consistent point-in-time
snapshot. Cost is the same (~25s for 6.9GB) so there is no reason to do it the
unsafe way.

VERIFIES BEFORE IT ROTATES, and that ordering is the whole safety argument. A
backup is checked (integrity + a real row count) BEFORE any older one is
deleted; if the new snapshot fails its check it is removed and the existing
backups are left alone. The failure mode this prevents is the one that matters:
a corrupt write silently replacing every good copy you had.

WHAT A CLEAN RUN LOOKS LIKE. Row counts will differ slightly from the live
database by the time you compare them -- the server settles bets while the
snapshot is being taken. That is expected. What must hold is that TOTALS
reconcile: a bet moving pending -> lost is fine, a bet disappearing is not.
"""
from __future__ import annotations

import datetime
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from app.config import settings

log = logging.getLogger("db_backup")

# How many snapshots to keep. Two means you always have a fallback if the most
# recent one turns out to have caught a bad moment -- one is not a backup
# strategy, it is a single point of failure with extra steps.
KEEP = 2
_PREFIX = ".backup-"
# Refuse to write when the result would leave the disk uncomfortably tight.
# A backup that fills the volume takes the LIVE database down with it, which is
# the opposite of the job's purpose.
MIN_FREE_MULTIPLE = 1.5


def _snapshots(db: Path) -> list[Path]:
    """Existing snapshots, oldest first.

    Excludes the -wal/-shm siblings SQLite creates next to a snapshot. Leaving
    them in is not cosmetic: a plain glob sorts "<name>-wal" AFTER "<name>", so
    "the newest snapshot" resolves to a write-ahead log with no tables in it --
    which is exactly how a verification step ends up confidently checking an
    empty file (hit while building this, caught only because the error was loud).
    """
    return sorted(
        (p for p in db.parent.glob(db.name + _PREFIX + "*")
         if not p.name.endswith(("-wal", "-shm"))),
        key=lambda p: p.name,
    )


def _verify(path: Path) -> tuple[bool, str]:
    """Integrity check plus a real read. quick_check alone can pass on a file
    whose tables are missing, so this also counts a table the app cannot run
    without.

    FULL integrity_check, NOT quick_check -- and this distinction cost a
    database on 2026-08-21.

    quick_check skips most page-level validation. The snapshot taken that
    morning was certified "integrity ok, 655 bets" by this function and was
    ACTUALLY CORRUPT: a full check on it reported dozens of
    "btreeInitPage() returns error code 11" failures across trees 6, 7 and 10.
    Both of this function's old tests passed anyway, because the damage was in
    the market_snapshots b-trees while `placed_bets` -- the very table counted
    here -- read perfectly.

    A backup verifier that can certify a corrupt backup is worse than no
    verifier, because it is trusted precisely at the moment it matters. The
    whole point of the surrounding code is "never rotate on an unverified
    backup"; that guarantee is only as good as this check.

    COST, measured on the live 7.78GB DB: quick_check ~1s, integrity_check
    ~240s. That is the right trade for a job that runs daily and exists solely
    so a restore can be trusted -- and it is a strong argument for keeping the
    database small enough that verifying it is cheap.
    """
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            c.execute("pragma cache_size=-262144")   # else the check crawls
            status = c.execute("pragma integrity_check(20)").fetchone()[0]
            bets = c.execute("select count(*) from placed_bets").fetchone()[0]
        finally:
            c.close()
    except Exception as exc:                       # noqa: BLE001 -- report anything
        return False, f"unreadable: {exc}"
    if status != "ok":
        return False, f"integrity {status}"
    if bets <= 0:
        return False, "placed_bets is empty"
    return True, f"integrity ok, {bets} bets"


def run_backup() -> dict:
    """Take one verified snapshot, then rotate. Returns a summary dict."""
    db = Path(settings.sqlite_url().replace("sqlite:///", ""))
    if not db.exists():
        log.error("db backup: no database at %s", db)
        return {"ok": False, "reason": "missing db"}

    size = db.stat().st_size
    free = shutil.disk_usage(db.parent).free
    if free < size * MIN_FREE_MULTIPLE:
        # LOUD and skipped, never a partial write.
        log.error("db backup SKIPPED: %.1fGB free, want %.1fGB (db is %.1fGB)",
                  free / 1e9, size * MIN_FREE_MULTIPLE / 1e9, size / 1e9)
        return {"ok": False, "reason": "low disk"}

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db.with_name(db.name + _PREFIX + stamp)
    started = datetime.datetime.now()
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except Exception as exc:                       # noqa: BLE001
        log.exception("db backup FAILED writing %s", target)
        for junk in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            junk.unlink(missing_ok=True)
        return {"ok": False, "reason": f"write failed: {exc}"}

    ok, detail = _verify(target)
    if not ok:
        # Remove the bad snapshot and KEEP every existing one. Rotating on an
        # unverified backup is how a good copy gets replaced by a broken one.
        log.error("db backup VERIFY FAILED (%s) -- discarded, older backups kept", detail)
        for junk in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            junk.unlink(missing_ok=True)
        return {"ok": False, "reason": f"verify failed: {detail}"}

    secs = (datetime.datetime.now() - started).total_seconds()
    log.info("db backup ok: %s (%.2fGB in %.0fs, %s)",
             target.name, target.stat().st_size / 1e9, secs, detail)

    removed = []
    snaps = _snapshots(db)
    for old in snaps[:-KEEP] if len(snaps) > KEEP else []:
        for p in (old, Path(str(old) + "-wal"), Path(str(old) + "-shm")):
            p.unlink(missing_ok=True)
        removed.append(old.name)
        log.info("db backup: rotated out %s", old.name)
    return {"ok": True, "path": str(target), "seconds": round(secs, 1),
            "kept": [p.name for p in _snapshots(db)], "removed": removed}
