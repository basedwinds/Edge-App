"""Reconciles Polymarket market status the way market_resolution_settlement.py
does for Kalshi -- the other half of a defect that was only ever half-fixed.

THE BUG. Every per-sport Polymarket refresh fetches with `closed=false`, exactly
as the Kalshi refreshes fetch only OPEN markets. So the moment a Polymarket
market resolves it stops being returned, and its stored status is frozen at
whatever it last was: "active", forever. Nothing walks back over it.

That is the same bug reconcile_kalshi_market_status() was written for, and the
Polymarket side is far worse. Measured 2026-08-06 on a random 100-conditionId
sample of the 30,573 distinct conditions this app called active: 80 were already
CLOSED on Polymarket. Kalshi's equivalent rate was 21%.

Why it matters beyond cosmetics: routers filter on status == "active", so a
stale-active row is still priceable and recommendable, and a resolved market's
last price sits at 0 or 1 -- precisely the shape that manufactures an enormous
fake edge against any confident model.

HOW GAMMA REPORTS IT. `GET /markets?condition_ids=...` silently filters to
NON-closed markets -- a resolved condition simply is not in the response, with no
error and no flag. Adding `&closed=true` inverts that: the same request returns
the condition if and only if Polymarket considers it closed. That is a positive,
authoritative report, so this queries the closed side and treats a HIT as the
signal. Absence is never evidence here -- a condition can be missing because a
chunk failed or because it was delisted -- which keeps this in the same
conservative direction as the Kalshi reconciler: only ever write a status the
exchange actually reported.

Do NOT use the `active` field for this. Closed Polymarket markets keep
`active: true` (verified on live resolved markets); `closed` is the field that
moves. `endDate` is no good either -- resolved markets were observed carrying
endDates days in the future.

Network happens BEFORE the write lock; only the status write takes it, per
poller_lock.
"""
import logging
import re

from app.clients.base import get_json
from app.db.database import SessionLocal
from app.db.models import Market
from app.ingestion.poller_lock import db_write_lock

log = logging.getLogger("polymarket_resolution")

_GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
# 100 condition_ids per request is the measured ceiling: 100 returns fine, 200
# gets a 422 from Gamma. Same batch size the Kalshi path uses, coincidentally.
_BATCH = 100

# source_ticker is "{conditionId}" or "{conditionId}-{outcome}", and the outcome
# is a team or player name that can itself contain "-", so the conditionId is
# taken by its fixed shape (0x + 64 hex) rather than by splitting on "-".
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}")


def condition_id(source_ticker: str | None) -> str | None:
    """The conditionId embedded in a Polymarket source_ticker, or None."""
    if not source_ticker:
        return None
    m = _CONDITION_ID.match(source_ticker)
    return m.group(0) if m else None


def _fetch_closed(condition_ids: list[str]) -> set[str]:
    """The subset of `condition_ids` Polymarket currently reports as closed.

    A chunk that errors is logged and skipped, contributing nothing -- those
    conditions are simply left alone this cycle rather than guessed at.
    """
    closed: set[str] = set()
    for i in range(0, len(condition_ids), _BATCH):
        chunk = condition_ids[i : i + _BATCH]
        query = "&".join(f"condition_ids={c}" for c in chunk)
        try:
            rows = get_json(f"{_GAMMA_MARKETS}?{query}&closed=true&limit={_BATCH * 5}")
        except Exception:
            log.exception("polymarket closed-status fetch failed for a chunk")
            continue
        for m in rows or []:
            # Belt and braces: trust the row's own flag, not just the filter.
            if m.get("closed") and m.get("conditionId"):
                closed.add(m["conditionId"])
    return closed


def reconcile_polymarket_market_status() -> int:
    """Move markets Polymarket has closed off "active". Returns the count.

    Only ever writes in one direction -- active -> closed -- because that is the
    only transition a `closed=true` hit is evidence for. Nothing here reopens a
    market or settles a bet.
    """
    session = SessionLocal()
    try:
        rows = (
            session.query(Market.id, Market.source_ticker)
            .filter(Market.source == "polymarket", Market.status == "active",
                    Market.source_ticker.isnot(None))
            .all()
        )
    finally:
        session.close()
    if not rows:
        return 0

    by_market = {mid: condition_id(t) for mid, t in rows}
    wanted = sorted({c for c in by_market.values() if c})
    if not wanted:
        return 0

    closed = _fetch_closed(wanted)
    if not closed:
        return 0

    changed = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for mid, cid in by_market.items():
                if not cid or cid not in closed:
                    continue
                m = session.get(Market, mid)
                if m is None or m.status != "active":
                    continue
                m.status = "closed"
                changed += 1
            if changed:
                session.commit()
                log.info("reconciled %d polymarket market statuses away from 'active'", changed)
        finally:
            session.close()
    return changed
