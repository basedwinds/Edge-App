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

from sqlalchemy.orm import Session

from app.db.models import Market

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
