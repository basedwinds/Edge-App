"""Persist Kalshi racing markets into the Market table so racing rides the same
snapshot -> paper-log -> CLV -> recommendations machinery as every other sport
(sport = the series: f1/irl/nascar). One RaceEvent per Kalshi event (its
close_time is the CLV closing-line cutoff, a race-day proxy for the start), one
Market per driver-market with a fresh MarketSnapshot each poll.
"""
import datetime

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot, RaceEvent


def _parse_dt(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def upsert_race_event(session: Session, series: str, event_ticker: str,
                      name: str | None, close_time: str | None,
                      real_start: "datetime.datetime | None" = None) -> int:
    """`real_start` (the true race date, resolved from ESPN's calendar) is
    preferred over Kalshi's `close_time`, which is an unreliable settlement
    deadline that can sit weeks after the actual race."""
    ev = session.query(RaceEvent).filter_by(event_ticker=event_ticker).one_or_none()
    if ev is None:
        ev = RaceEvent(series=series, event_ticker=event_ticker)
        session.add(ev)
    ev.series = series
    if name:
        ev.name = name
    dt = real_start or _parse_dt(close_time)
    if dt:
        ev.start_time = dt
    session.flush()
    return ev.id


def upsert_racing_market(session: Session, row: dict, race_event_id: int) -> Market:
    m = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if m is None:
        m = Market(source="kalshi", source_ticker=row["ticker"],
                   source_event_id=row["event_ticker"], sport=row["series"])
        session.add(m)
    m.market_type = row["market_type"]      # race_winner | top_n | pole
    m.sport = row["series"]
    m.race_event_id = race_event_id
    m.team = row["driver"]
    m.line = float(row["line"]) if row["line"] is not None else None
    m.group_label = row.get("event_title") or row["event_ticker"]
    m.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=m.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return m
