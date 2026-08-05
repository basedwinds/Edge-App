"""Closes stale game markets that Kalshi dropped from its open list but that
linger as `active` in our DB forever (the poller only UPDATES markets it still
sees; it never marks the vanished ones closed). These pile up -- 6k+ observed --
inflating active counts and tripping the health check's unlinked-markets warning.

Safe because it keys ONLY on the date encoded in the Kalshi ticker (e.g.
`KXMLBGAME-26JUL17...` -> 2026-07-17): a game/match/fight market whose date is
comfortably in the past is definitively resolved. FUTURES tickers use a season
year (`KXNFLAFCWEST-27-DEN`) not a date, so they never match and are never
touched (verified: 0 futures-type markets caught on a full dry-run). Racing
event tickers (`HUNGP26`) don't match the date shape either.
"""
import datetime
import logging
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.clients.kalshi_client import get_markets_by_tickers
from app.db.models import Market, MarketSnapshot

log = logging.getLogger("market_cleanup")

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
# YYMONDD as it appears in a Kalshi game ticker, e.g. "26JUL17".
_DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})")


def _ticker_date(ticker: str | None) -> datetime.date | None:
    if not ticker:
        return None
    m = _DATE_RE.search(ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    if mon not in _MONTHS:
        return None
    try:
        return datetime.date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None


def close_stale_game_markets(session: Session, days: int = 2, commit: bool = True) -> int:
    """Marks active Kalshi markets 'closed' when their ticker-encoded date is
    more than `days` days in the past. Returns the count closed."""
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    rows = (
        session.query(Market)
        .filter(Market.status == "active", Market.source == "kalshi")
        .all()
    )
    closed = 0
    for m in rows:
        d = _ticker_date(m.source_ticker)
        if d is not None and d < cutoff:
            m.status = "closed"
            closed += 1
    if commit:
        session.commit()
    log.info("market cleanup: closed %d stale game markets (ticker date < %s)", closed, cutoff)
    return closed


# Statuses Kalshi reports for a market that is no longer tradeable.
_DEAD = {"closed", "determined", "finalized", "settled"}


def reconcile_vanished_market_status(
    session: Session, stale_hours: int = 24, limit: int = 4000, commit: bool = True
) -> dict[str, int]:
    """Asks Kalshi the true status of active markets that stopped updating.

    close_stale_game_markets above reads the DATE out of the ticker, which only
    works for per-game tickers (`KXMLBGAME-26JUL17...`). Futures tickers carry a
    season or an event code instead (`KXCS2-BBS2F26-FAL`, `KXNFLAFCWEST-27-DEN`),
    so that function deliberately never touches them -- and nothing else did
    either. A finished tournament therefore stayed `active` forever: BLAST Bounty
    Season 2 Finals settled on 2026-07-25 and was still being recommended as a
    bet, with a Mark placed button, eleven days later.

    Scale of the gap when this was written: 27,755 of 54,575 markets marked
    active (51%) had not received a snapshot in 24 hours. A 100-ticker sample
    came back 100/100 `finalized`.

    Rather than infer death from silence, this ASKS. Silence only selects which
    markets to ask about; the status written is Kalshi's own. That distinction
    matters -- a market can go quiet because a poller crashed or a series was
    temporarily dropped, and guessing "closed" from silence alone would retire
    live markets. A market Kalshi still calls open is explicitly left alone.

    `limit` bounds one run (4,000 markets = 40 requests) so the daily job stays
    polite; the backlog drains over a few days and the steady state is small.
    """
    latest = (
        session.query(MarketSnapshot.market_id, func.max(MarketSnapshot.ts).label("mx"))
        .group_by(MarketSnapshot.market_id).subquery()
    )
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=stale_hours)
    rows = (
        session.query(Market)
        .join(latest, latest.c.market_id == Market.id)
        .filter(Market.status == "active", Market.source == "kalshi",
                Market.source_ticker.isnot(None), latest.c.mx < cutoff)
        .order_by(latest.c.mx)          # oldest silence first
        .limit(limit).all()
    )
    if not rows:
        return {"checked": 0, "closed": 0, "still_open": 0, "unknown": 0}

    by_ticker = {m.source_ticker: m for m in rows}
    try:
        live = get_markets_by_tickers(list(by_ticker))
    except Exception:
        log.exception("status reconcile: Kalshi lookup failed -- nothing changed")
        return {"checked": 0, "closed": 0, "still_open": 0, "unknown": 0}

    closed = still_open = 0
    seen = set()
    for m in live:
        row = by_ticker.get(m.get("ticker"))
        if row is None:
            continue
        seen.add(m["ticker"])
        status = (m.get("status") or "").lower()
        if status in _DEAD:
            row.status = status
            closed += 1
        else:
            still_open += 1
    # A ticker Kalshi did not return is NOT assumed dead: an unrecognised or
    # delisted ticker looks identical to a transient API omission from here.
    unknown = len(by_ticker) - len(seen)
    if commit:
        session.commit()
    log.info("status reconcile: checked %d, closed %d, still open %d, no answer %d",
             len(by_ticker), closed, still_open, unknown)
    return {"checked": len(by_ticker), "closed": closed,
            "still_open": still_open, "unknown": unknown}
