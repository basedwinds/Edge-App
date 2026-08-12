"""Bounds MarketSnapshot table growth. The price pollers append one snapshot
per market per cycle and never delete, so the table grows without limit (5.5M
rows observed 2026-07-22) -- which slowly drags every endpoint's
`_batch_latest_snapshots` (GROUP BY MAX(ts)) and the CLV closing-price lookups.

Snapshots are only ever read two ways:
  1. the LATEST per market (current price) -- routers, divergence scanner;
  2. the last snapshot BEFORE a game's kickoff (closing line) -- CLV, only for
     markets that have a placed bet.
So we keep everything from the last `keep_days` (covers recent games' closing
lines for forward CLV) PLUS the single most-recent snapshot for every market
(so current price is never lost, even for a market not updated in a while), and
delete the rest. Conservative on the CLV side -- keep_days is generous.
"""
import datetime
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import MarketSnapshot

log = logging.getLogger("snapshot_maintenance")

# 14 days safely covers every WEEKLY (game-tied) bet's closing-line lookup --
# those games settle within days of placement -- while capping the table (the
# pollers write ~260k snapshots/day). Futures bets' CLV is "current vs
# placement" off the always-preserved latest snapshot, so they need no history.
DEFAULT_KEEP_DAYS = 14


# Rows deleted per transaction. The prune runs under the SAME app-wide lock the
# nine sports' DB writes use (poller_lock), so a single giant DELETE is not just
# slow -- it starves every poller for its whole duration and the job gets skipped
# with "maximum number of running instances reached" before it can finish. That
# is why 26 days of snapshots were still present on 2026-08-11 under a 14-day
# policy: the job was firing (8 min after each startup) and never completing.
#
# Batching lets the caller take and release the lock per batch so pollers
# interleave, and makes progress durable -- an interrupted run keeps whatever it
# already committed instead of rolling back hours of work.
DEFAULT_BATCH_SIZE = 25_000


def prune_market_snapshots(session: Session, keep_days: int = DEFAULT_KEEP_DAYS,
                           batch_size: int = DEFAULT_BATCH_SIZE,
                           lock=None) -> int:
    """Deletes snapshots older than `keep_days` that are NOT the latest for
    their market, in committed batches. Returns the number deleted.

    `lock` is an optional context-manager factory (poller_lock.db_write_lock)
    taken around EACH BATCH rather than the whole run -- see DEFAULT_BATCH_SIZE.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=keep_days)
    # the most-recent snapshot id for each market (id is autoincrement, so the
    # max id per market_id is its latest row) -- these are always preserved.
    #
    # MATERIALISED ONCE, not used as a correlated subquery per batch. Measured
    # 2026-08-11: re-running `NOT IN (SELECT MAX(id) ... GROUP BY market_id)` on
    # every batch managed only ~3.5k deletes/sec, because that subquery scans the
    # whole table each time. Pulling the ~133k keep-ids into a set once and
    # filtering candidates in Python is the same result, far cheaper.
    latest_ids = {r[0] for r in
                  session.query(func.max(MarketSnapshot.id))
                  .group_by(MarketSnapshot.market_id).all()}
    total = 0
    while True:
        candidates = [r[0] for r in session.query(MarketSnapshot.id)
                      .filter(MarketSnapshot.ts < cutoff)
                      .limit(batch_size * 2).all()]
        if not candidates:
            break
        ids = [i for i in candidates if i not in latest_ids][:batch_size]
        if not ids:
            # Every remaining old row is a market's latest and must be kept.
            break
        if lock is not None:
            with lock():
                session.query(MarketSnapshot).filter(MarketSnapshot.id.in_(ids)).delete(
                    synchronize_session=False)
                session.commit()
        else:
            session.query(MarketSnapshot).filter(MarketSnapshot.id.in_(ids)).delete(
                synchronize_session=False)
            session.commit()
        total += len(ids)
        log.info("snapshot prune: %d deleted so far", total)
    log.info("pruned %d market snapshots older than %d days", total, keep_days)
    return total
