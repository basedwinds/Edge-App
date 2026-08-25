"""Stores each Kalshi market's own RESOLUTION TERMS alongside the market.

WHY THIS EXISTS. This app kept a market's identifier and title and threw its
rules text away, so questions about what a contract actually pays on could not
be answered from stored data at all. Two landed on the same day (2026-08-25) and
both were guesswork until the rules were read directly:

  * `wins_any` -- does "15+ wins" count playoff wins? A regular-season-only base
    rate said the model was ~4x too high; a playoff-inclusive one said it was too
    LOW. Opposite conclusions from the same data, decided entirely by a
    definition nobody had stored. (It is REGULAR SEASON: "If any Pro Football
    team wins 17 games in the 2026-27 Pro Football regular season, then the
    market resolves to Yes.")

  * `h2h_wins` -- how does Kalshi settle a TIE on win totals? The model splits
    ties 50/50, and whether that was right or a silent bug could not be checked.
    It is right, and the answer was in rules_secondary: "If New York G and
    Atlanta record an equal number of wins ... then all markets will resolve to
    50/50."

BOTH FIELDS ARE KEPT, and the tie case above is exactly why: rules_primary
states the Yes condition, but the edge cases -- ties, voids, early close -- live
in rules_secondary. Storing only the primary would have left the h2h question
unanswered.

Deliberately a SEPARATE backfill rather than threaded through every upsert path.
The catalog modules write markets from a dozen call sites per sport; adding a
field to each is a large, drift-prone change for data that never changes after a
market is created. This walks markets missing rules and fills them in batches of
100 tickers (the batching `get_markets_by_tickers` already does), so it is cheap
to run repeatedly and safe to interrupt.
"""
from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from app.clients.kalshi_client import get_markets_by_tickers
from app.db.models import Market

log = logging.getLogger(__name__)

# One pass fetches at most this many markets. Kalshi allows 100 tickers per
# call, so the default is 20 calls -- measured at ~0.3s each.
DEFAULT_LIMIT = 2000


def refresh_market_rules(session: Session, limit: int = DEFAULT_LIMIT) -> dict:
    """Fill in rules_primary/rules_secondary for Kalshi markets missing them.

    Only ever WRITES a market that has no rules stored, so a re-run costs one
    query and nothing else once the backlog is clear. Returns a small summary
    so a caller (or a health check) can see whether it is keeping up.
    """
    # ACTIVE MARKETS FIRST. There are ~99k Kalshi markets on file and most are
    # long settled, so an unordered scan spends days on history before reaching
    # anything bettable -- the first backfill run filled 12,000 rows without
    # touching either market the feature was built to answer. Sorting active to
    # the front means the rows that can still take money get their terms first.
    pending = (
        session.query(Market)
        .filter(Market.source == "kalshi")
        .filter(Market.source_ticker.isnot(None))
        .filter(Market.rules_primary.is_(None))
        .order_by((Market.status == "active").desc(), Market.id.desc())
        .limit(limit)
        .all()
    )
    if not pending:
        return {"pending": 0, "fetched": 0, "updated": 0, "missing": 0}

    by_ticker: dict[str, list[Market]] = {}
    for m in pending:
        by_ticker.setdefault(m.source_ticker, []).append(m)

    try:
        fetched = get_markets_by_tickers(list(by_ticker))
    except Exception:
        log.exception("market-rules fetch failed")
        return {"pending": len(pending), "fetched": 0, "updated": 0, "missing": 0}

    now = datetime.datetime.utcnow().isoformat()
    updated = 0
    seen: set[str] = set()
    for row in fetched:
        ticker = row.get("ticker")
        if not ticker:
            continue
        seen.add(ticker)
        primary = (row.get("rules_primary") or "").strip()
        if not primary:
            # Nothing to store. Left NULL on purpose so a later run retries it
            # rather than caching an empty answer as if it were the truth.
            continue
        secondary = (row.get("rules_secondary") or "").strip() or None
        for m in by_ticker.get(ticker, []):
            m.rules_primary = primary
            m.rules_secondary = secondary
            m.rules_fetched_at = now
            updated += 1
    session.commit()

    result = {
        "pending": len(pending),
        "fetched": len(fetched),
        "updated": updated,
        # Tickers we asked for and Kalshi did not return -- usually settled and
        # aged out of the API. Reported rather than retried forever.
        "missing": len([t for t in by_ticker if t not in seen]),
    }
    log.info("market rules: %s", result)
    return result
