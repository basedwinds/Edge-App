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


def prune_market_snapshots(session: Session, keep_days: int = DEFAULT_KEEP_DAYS) -> int:
    """Deletes snapshots older than `keep_days` that are NOT the latest for
    their market. Returns the number deleted."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=keep_days)
    # the most-recent snapshot id for each market (id is autoincrement, so the
    # max id per market_id is its latest row) -- these are always preserved.
    latest_ids = session.query(func.max(MarketSnapshot.id)).group_by(MarketSnapshot.market_id)
    deleted = (
        session.query(MarketSnapshot)
        .filter(MarketSnapshot.ts < cutoff, ~MarketSnapshot.id.in_(latest_ids))
        .delete(synchronize_session=False)
    )
    session.commit()
    log.info("pruned %d market snapshots older than %d days", deleted, keep_days)
    return deleted
