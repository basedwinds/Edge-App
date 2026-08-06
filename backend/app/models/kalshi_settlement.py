"""Settles pending bets from KALSHI'S OWN market result.

WHY THIS EXISTS. bet_settlement.py grades from the sport's result table
(TennisMatch.winner_key, MmaFight.winner_id, ...), which depends on a third-party
results scraper actually keeping up. When it lags, bets sit "pending" past their
start and the Bet Tracker shows them as "delayed?" indefinitely -- user-reported
2026-08-03, with the oldest stuck ~24h.

Kalshi already knows the answer. A finalized market carries result "yes"/"no",
which for a moneyline IS the winner. That is authoritative, needs no scraping,
and works for every sport at once.

WHY IT IS NARROW ON PURPOSE:

  * Only bets already PENDING whose scheduled start has passed -- so this is a
    handful of single-market lookups per run, not a crawl.
  * Only Kalshi bets with a stored ticker.
  * Only markets Kalshi reports as settled/finalized with a non-empty result.
    Checked live while building this: 4 of 6 stuck bets came back status=active
    with result='' -- those matches genuinely had not resolved, Kalshi's
    occurrence_datetime was simply an optimistic estimate it never revises
    (one sat 17h past its stated time, still active, with close_time two weeks
    out). Those are left alone rather than force-settled.
  * Only market types where "yes" maps unambiguously to the bet winning. A
    moneyline's yes-side IS bet.team winning. Ladder rungs and side-bearing
    markets are deliberately excluded -- the mapping there depends on
    line/side semantics per sport, and guessing would settle bets wrongly,
    which is far worse than leaving them pending.
"""
import logging

from sqlalchemy.orm import Session

from app.clients.kalshi_client import BASE as KALSHI_BASE
from app.db.models import Market, PlacedBet

log = logging.getLogger("kalshi_settlement")

# Market types whose Kalshi "yes" resolution means bet.team won outright.
_YES_MEANS_TEAM_WON = {"moneyline", "series_winner", "match_winner"}

_SETTLED_STATUSES = {"settled", "finalized", "determined"}


def _market_result(ticker: str) -> tuple[str | None, str | None]:
    """(status, result) straight from Kalshi, or (None, None) if unreachable."""
    import httpx

    try:
        resp = httpx.get(f"{KALSHI_BASE}/markets/{ticker}", timeout=20.0)
        if resp.status_code != 200:
            return None, None
        data = resp.json() or {}
    except Exception:
        log.debug("kalshi lookup failed for %s", ticker, exc_info=True)
        return None, None
    m = data.get("market") or {}
    # Reduce Kalshi's "scalar" result to yes/no/void here, so this path does not
    # strand the bets the batch settler stopped stranding. See
    # market_resolution_settlement.normalize_result.
    from app.ingestion.market_resolution_settlement import normalize_result

    return m.get("status"), (normalize_result(m) or None)


def settle_pending_from_kalshi(session: Session, bets: list[PlacedBet]) -> int:
    """Grade `bets` from Kalshi's own result. Returns how many were settled.

    Callers pass an already-filtered list (pending, past start) so this module
    never decides policy about which bets are worth a network call.
    """
    import datetime

    settled = 0
    for bet in bets:
        market = session.get(Market, bet.market_id) if bet.market_id else None
        if market is None or market.source != "kalshi" or not market.source_ticker:
            continue
        # The market's LIVE type, not the snapshot frozen on the bet at
        # placement -- a re-typed market otherwise keeps being judged against
        # the old type forever. See bet_settlement.effective_market_type for the
        # 499 bets that cost.
        if (market.market_type or bet.market_type) not in _YES_MEANS_TEAM_WON:
            continue
        status, result = _market_result(market.source_ticker)
        if status is None:
            continue
        if status not in _SETTLED_STATUSES or result not in ("yes", "no"):
            # Still trading, or settled without a usable result -- leave pending.
            continue
        bet.status = "won" if result == "yes" else "lost"
        bet.settled_at = datetime.datetime.utcnow()
        bet.settlement_note = f"auto-settled from Kalshi market result ({result})"
        # Mirror the outcome onto the market row so the UI stops treating a
        # resolved market as live even before the next full poll.
        market.status = "closed"
        settled += 1

    if settled:
        session.commit()
        log.info("kalshi settlement: settled %d pending bets", settled)
    return settled
