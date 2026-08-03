"""Settles pending bets from POLYMARKET'S own resolution.

Companion to kalshi_settlement.py, and the bigger half: Polymarket carries a
large share of this app's bets and had no authoritative settlement path at all,
so a bet there could only settle if a third-party results scraper happened to
catch the match. Johnny Speeds vs Metizport (user-reported 2026-08-03) sat
pending for exactly that reason.

WHY THIS IS STRONGER THAN THE KALSHI VERSION. Kalshi gives a yes/no on a whole
market, so that fallback had to be limited to market types where "yes" plainly
means the bet's team won. Polymarket instead publishes the outcome VECTOR:

    outcomes      = ["Metizport", "Johnny Speeds"]
    outcomePrices = ["1", "0"]

and this app already stores which outcome a bet is on -- Market.source_ticker is
"<conditionId>-<outcome name>". So the specific leg can be resolved directly,
which makes this safe for handicaps and other side-bearing markets that the
Kalshi path deliberately skips.

GATING. Only markets Polymarket reports closed AND umaResolutionStatus
"resolved", AND whose prices are decisive (one outcome at ~1, the rest ~0). A
market that is closed but still disputed, or priced mid-range, is left pending --
a late settlement is recoverable, a wrong one is not.
"""
import logging

from sqlalchemy.orm import Session

from app.db.models import Market, PlacedBet

log = logging.getLogger("polymarket_settlement")

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"

# How close a resolved outcome price must be to 1 (or 0) to be treated as final.
_DECISIVE = 0.99


def _split_ticker(ticker: str) -> tuple[str, str] | None:
    """"0xabc...-Johnny Speeds" -> ("0xabc...", "Johnny Speeds"). Outcome names
    can contain "-", so split once only."""
    if not ticker or "-" not in ticker:
        return None
    cid, outcome = ticker.split("-", 1)
    if not cid.startswith("0x") or not outcome:
        return None
    return cid, outcome


def _resolved_outcomes(condition_id: str) -> dict[str, float] | None:
    """{outcome name: resolved price} once the market is genuinely resolved."""
    import json

    import httpx

    try:
        # closed=true is required -- a resolved market drops off the default
        # (open) listing, which is what made these invisible in the first place.
        resp = httpx.get(GAMMA_MARKETS, params={"condition_ids": condition_id, "closed": "true"}, timeout=20.0)
        if resp.status_code != 200:
            return None
        rows = resp.json()
    except Exception:
        log.debug("polymarket lookup failed for %s", condition_id, exc_info=True)
        return None
    if not isinstance(rows, list) or not rows:
        return None
    m = rows[0]
    if not m.get("closed") or str(m.get("umaResolutionStatus") or "").lower() != "resolved":
        return None
    try:
        names = m["outcomes"] if isinstance(m["outcomes"], list) else json.loads(m["outcomes"])
        prices = m["outcomePrices"] if isinstance(m["outcomePrices"], list) else json.loads(m["outcomePrices"])
        vals = [float(p) for p in prices]
    except (KeyError, TypeError, ValueError):
        return None
    if len(names) != len(vals) or not any(v >= _DECISIVE for v in vals):
        return None  # not decisively resolved -- leave it alone
    return dict(zip(names, vals))


def settle_pending_from_polymarket(session: Session, bets: list[PlacedBet]) -> int:
    """Grade `bets` from Polymarket's resolution. Returns how many settled."""
    import datetime

    settled = 0
    for bet in bets:
        market = session.get(Market, bet.market_id) if bet.market_id else None
        if market is None or market.source != "polymarket" or not market.source_ticker:
            continue
        parts = _split_ticker(market.source_ticker)
        if parts is None:
            continue
        resolved = _resolved_outcomes(parts[0])
        if not resolved:
            continue
        price = resolved.get(parts[1])
        if price is None:
            # Outcome name drifted from what we stored -- do not guess which leg
            # this bet was on.
            log.warning("polymarket outcome %r not in %s", parts[1], sorted(resolved))
            continue
        if price >= _DECISIVE:
            bet.status = "won"
        elif price <= 1 - _DECISIVE:
            bet.status = "lost"
        else:
            continue
        bet.settled_at = datetime.datetime.utcnow()
        bet.settlement_note = f"auto-settled from Polymarket resolution ({parts[1]} @ {price:g})"
        market.status = "closed"
        settled += 1

    if settled:
        session.commit()
        log.info("polymarket settlement: settled %d pending bets", settled)
    return settled
