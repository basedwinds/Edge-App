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


def _fetch_statuses(tickers: list[str]) -> dict:
    """{ticker: status} for whatever Kalshi currently says, not just finalized."""
    out: dict[str, str] = {}
    for i in range(0, len(tickers), _BATCH):
        chunk = tickers[i : i + _BATCH]
        try:
            d = get_json(f"{_MARKETS_URL}?tickers={','.join(chunk)}&limit={_BATCH}")
        except Exception:
            log.exception("kalshi batch status fetch failed for a chunk")
            continue
        for m in d.get("markets", []):
            st = m.get("status")
            if st:
                out[m.get("ticker")] = st
    return out


def reconcile_kalshi_market_status() -> int:
    """Refresh Market.status for rows we still believe are active.

    REAL BUG this fixes (user-reported 2026-08-06: a finished CS2 series, "33 vs
    SPARTA", still showing as a live market at 100%). Every per-sport Kalshi
    refresh fetches only OPEN markets, so the moment a market resolves it stops
    being returned and its stored status is frozen at whatever it last was --
    "active", forever. Nothing ever walks back over a resolved market to correct
    it.

    Measured on a random 180-ticker sample of the 10,586 Kalshi markets this app
    called active: 38 of 180 (21%) were already FINALIZED on Kalshi, spread
    across mlb/tennis/cs2/wnba/lol/valorant. That is roughly 2,200 resolved
    markets being served as live ones.

    This matters beyond cosmetics: routers filter on status == "active", so a
    stale-active row is still eligible to be priced and recommended, and its
    last traded price sits at 0 or 1 -- which is exactly the shape that
    manufactures a huge fake edge against any confident model.

    Network first, then a single locked write, per poller_lock.
    """
    session = SessionLocal()
    try:
        rows = (
            session.query(Market.id, Market.source_ticker)
            .filter(Market.source == "kalshi", Market.status == "active", Market.source_ticker.isnot(None))
            .all()
        )
    finally:
        session.close()
    if not rows:
        return 0

    statuses = _fetch_statuses(sorted({t for _mid, t in rows if t}))
    if not statuses:
        return 0

    changed = 0
    with db_write_lock():
        session = SessionLocal()
        try:
            for mid, ticker in rows:
                real = statuses.get(ticker)
                # Only ever write a status Kalshi actually reported, and only
                # when it differs -- a missing ticker (delisted, or dropped from
                # a failed chunk) must not be guessed at.
                if not real or real == "active":
                    continue
                m = session.get(Market, mid)
                if m is None or m.status == real:
                    continue
                m.status = real
                changed += 1
            if changed:
                session.commit()
                log.info("reconciled %d kalshi market statuses away from 'active'", changed)
        finally:
            session.close()
    return changed


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
