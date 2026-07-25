"""Settles placed bets straight from the Kalshi market's OWN resolution -- the
authoritative, 100%-coverage settlement path.

Every Market row is one Kalshi yes/no ticker, and the app only ever bets that
market's priced YES side (see staking.py), so once Kalshi marks the market
`finalized`, its `result` grades the bet directly: yes->won, no->lost, void/''
->void. This needs NO external result scraping and NO team-name matching, so it
covers what the per-sport graders can't -- lower-tier matches missing from the
result feeds, map_winner (no per-map data stored), and even season futures once
they resolve. The per-sport graders + result scrapers stay for POLYMARKET bets
(no Kalshi ticker) and as an immediate fallback.

Network (batch ticker fetch) happens BEFORE the write lock; only the grade+commit
takes it.
"""
import datetime
import logging

from app.clients.base import get_json
from app.db.database import SessionLocal
from app.db.models import Market, PlacedBet
from app.ingestion.poller_lock import db_write_lock

log = logging.getLogger("market_resolution_settlement")

_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
_BATCH = 100


def _fetch_resolutions(tickers: list[str]) -> dict:
    """{ticker: 'yes'|'no'|'void'|''} for the FINALIZED ones among `tickers`."""
    out: dict[str, str] = {}
    for i in range(0, len(tickers), _BATCH):
        chunk = tickers[i : i + _BATCH]
        try:
            d = get_json(f"{_MARKETS_URL}?tickers={','.join(chunk)}&limit={_BATCH}")
        except Exception:
            log.exception("kalshi batch resolution fetch failed for a chunk")
            continue
        for m in d.get("markets", []):
            if m.get("status") in ("finalized", "settled"):
                out[m.get("ticker")] = (m.get("result") or "")
    return out


def settle_from_kalshi_resolution() -> int:
    """Grade every pending bet whose Kalshi market has finalized. Returns count."""
    # 1) read pending Kalshi bets + their tickers (no lock)
    session = SessionLocal()
    try:
        rows = (
            session.query(PlacedBet.id, Market.source_ticker)
            .join(Market, PlacedBet.market_id == Market.id)
            .filter(PlacedBet.status == "pending", Market.source == "kalshi", Market.source_ticker.isnot(None))
            .all()
        )
    finally:
        session.close()
    if not rows:
        return 0
    tickers = sorted({t for _bid, t in rows if t})

    # 2) batch-fetch resolutions (no lock)
    resolution = _fetch_resolutions(tickers)
    if not resolution:
        return 0

    # 3) grade + commit (under lock)
    now = datetime.datetime.utcnow()
    settled = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for bid, ticker in rows:
                r = resolution.get(ticker)
                if r is None:
                    continue  # market not finalized yet
                status = "won" if r == "yes" else "lost" if r == "no" else "void" if r in ("void", "") else None
                if status is None:
                    continue
                bet = session.get(PlacedBet, bid)
                if bet is None or bet.status != "pending":
                    continue
                bet.status = status
                bet.settled_at = now
                bet.settlement_note = f"auto-settled from Kalshi market resolution (result={r or 'void'})"
                settled += 1
            if settled:
                session.commit()
                log.info("settled %d bets from Kalshi market resolution", settled)
        finally:
            session.close()
    return settled
