"""Drop market snapshots that record nothing new, at the point of flush.

## Why

`market_snapshots` reached **95,617,027 rows over 41 days -- 2,332,123 a day**,
most of a 10 GB database, against the ~30M steady state `snapshot_maintenance`
assumes. Sampling 250 active markets and 252,258 of their snapshots:

    66.2%  identical to the previous row AND written within 30 min
    22.9%  identical but older than 30 min
    10.8%  an actual price change
     0.1%  the market's first snapshot

**Only 10.8% carry new information.** Skipping the first bucket removes ~1.54M
rows a day and lands the table near 32M -- the size it was always meant to be.

That matters beyond disk: every one of those writes happens inside the app-wide
write lock, and a 10 GB file with a 2 GB mmap over it is the configuration the
2026-08-27 corruption happened on.

## Why a flush hook rather than 97 edits

There are 97 `session.add(MarketSnapshot(...))` sites across 13 catalog modules.
Editing each is 97 chances to get one wrong, and a missed site fails SILENTLY --
it just keeps writing. One hook covers every present and future site, and there
is a single place to read the rule.

## THE HEARTBEAT IS NOT OPTIONAL

`clv.py` refuses a closing line more than `MAX_CLOSING_STALENESS` (6 hours)
older than kickoff, deliberately, so an app outage cannot fake one. Pure dedupe
would make a genuinely STABLE price indistinguishable from an outage and delete
CLV coverage wholesale -- the exact forward evidence the settlement work exists
to build.

So an unchanged price is still written every `HEARTBEAT_SECONDS`. 30 minutes is
twelve times inside that 6h limit, and the measurement above says it costs 22.9%
of writes to keep -- a cheap price for not breaking the app's only forward
validation.

## What it deliberately does NOT do

* No DB read. The comparison is against an in-process memo, so this adds no
  query -- a per-row lookup is what made the pollers slow in the first place.
* Empty memo after a restart means the first poll writes for every market. That
  is correct: it re-establishes a baseline rather than trusting state that did
  not survive.
* A rolled-back transaction leaves the memo claiming a write that never landed,
  so the next identical value is skipped. The heartbeat bounds that to 30
  minutes, which is why it is a bound and not just a staleness guard.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import event
from sqlalchemy.orm import Session as _Session

log = logging.getLogger("snapshot_dedupe")

# An unchanged price is still recorded this often. See the docstring: this is
# what keeps clv.py's 6h closing-line window satisfied for a stable market.
HEARTBEAT_SECONDS = 30 * 60

# market_id -> (values, epoch_seconds) of the last snapshot we let through.
_last: dict[int, tuple[tuple, float]] = {}
_lock = threading.Lock()

# Observability, so the effect is visible in production rather than assumed.
STATS = {"written": 0, "skipped": 0}
_last_report = [0.0]
_REPORT_EVERY = 15 * 60


def _values(obj) -> tuple:
    return (obj.yes_bid, obj.yes_ask, obj.last_price, obj.volume)


def should_write(market_id, values, now: float) -> bool:
    """True if this snapshot carries new information, or the heartbeat is due."""
    if market_id is None:
        return True  # not flushed yet -- cannot key a memo on it, so never skip
    prev = _last.get(market_id)
    if prev is None:
        return True
    prev_values, prev_ts = prev
    if prev_values != values:
        return True
    return (now - prev_ts) >= HEARTBEAT_SECONDS


def note_written(market_id, values, now: float) -> None:
    if market_id is not None:
        _last[market_id] = (values, now)


def _maybe_report(now: float) -> None:
    if now - _last_report[0] < _REPORT_EVERY:
        return
    _last_report[0] = now
    w, s = STATS["written"], STATS["skipped"]
    if w + s:
        log.info("snapshot dedupe: %d written, %d skipped (%.1f%% of writes removed), "
                 "%d markets tracked", w, s, 100 * s / (w + s), len(_last))


@event.listens_for(_Session, "before_flush")
def _drop_unchanged_snapshots(session, flush_context, instances):
    """Expunge pending MarketSnapshot rows that duplicate the last one written.

    Imported here rather than at module scope so this file can be imported from
    app.db.database without a circular import back through models.
    """
    from app.db.models import MarketSnapshot

    pending = [o for o in session.new if isinstance(o, MarketSnapshot)]
    if not pending:
        return
    now = time.time()
    with _lock:
        for obj in pending:
            vals = _values(obj)
            if should_write(obj.market_id, vals, now):
                note_written(obj.market_id, vals, now)
                STATS["written"] += 1
            else:
                # Pending and not yet flushed, so expunging fully removes it.
                session.expunge(obj)
                STATS["skipped"] += 1
        _maybe_report(now)
