"""Samples each futures leg's MODEL probability so it can be charted over time.

The market side of "how has this moved?" was always there -- MarketSnapshot
records every poll. The model side was not recorded anywhere: every futures
router prices its rows on the READ path and discards the result, so there was no
way to tell whether the model changed its mind or only the market did. That is
exactly the comparison a futures position needs, since these settle months out
and the only thing that moves in between is opinion.

Sampled by SELF-HTTP off the already-priced endpoints rather than re-running the
models here. Those prices go through the full stack -- pool routing, blends,
sim, caching -- and reproducing any of that in a second place is how two numbers
start disagreeing. The cache warmer keeps these endpoints hot anyway, so the
call is usually free.

HOURLY, deliberately. A futures price moves on news and results, not minute to
minute; a season-long market sampled every few minutes would be almost entirely
storage for a flat line. One row per market per hour also means the chart can be
read straight out of the table with no downsampling.
"""
from __future__ import annotations

import datetime
import logging

log = logging.getLogger("futures_history")

BASE = "http://127.0.0.1:8756"

# Every futures endpoint that returns a priced model_prob.
FUTURES_PATHS = [
    "/markets/futures", "/nba/futures", "/mlb/futures", "/tennis/futures",
    "/soccer/futures", "/valorant/futures", "/cs2/futures", "/lol/futures",
]

# Don't write a second row for the same market inside this window.
MIN_INTERVAL = datetime.timedelta(minutes=50)


def _fetch_rows() -> list[dict]:
    import httpx

    from app.shutdown import is_shutting_down

    rows: list[dict] = []
    with httpx.Client(timeout=90.0) as client:
        for path in FUTURES_PATHS:
            if is_shutting_down():  # see app/shutdown.py -- unkillable worker
                break
            try:
                resp = client.get(f"{BASE}{path}")
                if resp.status_code == 200:
                    body = resp.json()
                    if isinstance(body, list):
                        rows.extend(body)
            except Exception:
                # One dead endpoint must not cost the others their sample.
                log.debug("futures history: %s failed", path, exc_info=True)
    return rows


def record_futures_probs(session) -> int:
    """One row per futures leg that currently has a model number. Returns writes."""
    from sqlalchemy import func

    from app.db.models import FuturesProbHistory

    rows = _fetch_rows()
    if not rows:
        return 0

    now = datetime.datetime.utcnow()
    cutoff = now - MIN_INTERVAL
    recent = {
        mid for (mid,) in session.query(FuturesProbHistory.market_id)
        .filter(FuturesProbHistory.ts >= cutoff).distinct().all()
    }

    written = 0
    for r in rows:
        mid = r.get("id")
        model = r.get("model_prob")
        # A leg with no model number carries no information here -- the market
        # price is already in MarketSnapshot either way.
        if mid is None or model is None or mid in recent:
            continue
        session.add(FuturesProbHistory(
            market_id=mid, ts=now, model_prob=model, implied_prob=r.get("implied_prob"),
        ))
        recent.add(mid)
        written += 1

    if written:
        session.commit()
        log.info("futures history: recorded %d model probabilities", written)
    return written
