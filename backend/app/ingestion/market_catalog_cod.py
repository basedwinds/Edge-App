"""Catalog layer for Call of Duty: upsert breakingpoint matches, and bind
Kalshi markets onto them.

Simpler than market_catalog_cs2 for one reason worth stating: breakingpoint
gives every match a STABLE numeric id ("bp:356983"), so a rematch between the
same two teams is a different row by construction. CS2 and LoL have no such id
and must guess with a name match plus a date window -- which is the mechanism
that once bound a LoL rematch onto an already-played fixture, moved its start
into the future and orphaned a real $20 bet.

So there is deliberately no name-based rematch matcher here. Matching by a
stable id is not a simplification, it is the absence of a whole bug class, and
adding a fuzzy fallback "just in case" would reintroduce it.

MARKET TYPE: match_winner only. See kalshi_cod_client -- Kalshi lists no
spread, total-maps or per-map CoD series.
"""
from __future__ import annotations

import datetime
import logging
import re

from sqlalchemy.orm import Session

from app.clients.polymarket_client import quote_fields
from app.db.models import CodMatch, Market, MarketSnapshot
from app.ingestion.start_times import apply_start

log = logging.getLogger("market_catalog_cod")


def upsert_cod_match(session: Session, row: dict) -> CodMatch:
    """Insert or update one breakingpoint match row (see cod_data.fetch_matches).

    Keyed on the source's own stable id, so this is a true upsert with no
    guessing."""
    match = (session.query(CodMatch)
             .filter_by(source=row["source"], source_match_id=row["source_match_id"])
             .one_or_none())
    if match is None:
        match = CodMatch(
            source=row["source"], source_match_id=row["source_match_id"],
            team_a=row["team_a"], team_b=row["team_b"],
            event_name=row.get("event_name"), match_date=row["match_date"],
        )
        session.add(match)

    match.event_name = row.get("event_name") or match.event_name
    if row.get("best_of") is not None:
        # breakingpoint states best_of directly. Never overwrite a real known
        # value with a null: fetchLiveMatches returns best_of as null while
        # fetchMatchesPage returns the real 7, and a Bo7 priced as a Bo5 is a
        # materially different probability.
        match.best_of = row["best_of"] or match.best_of

    # Only move the start while the match is unsettled -- apply_start refuses
    # the past-to-future jump that orphans a played row.
    if match.winner is None:
        apply_start(match, row.get("estimated_start_time"))

    if row.get("maps_won_a") is not None:
        match.maps_won_a = row["maps_won_a"]
    if row.get("maps_won_b") is not None:
        match.maps_won_b = row["maps_won_b"]
    if row.get("winner") is not None:
        match.winner = row["winner"]

    # Always written, including back to False: a match that FINISHES must stop
    # being flagged live, and only an unconditional write does that. A
    # `if row["is_live"]:` guard here would latch the flag on forever.
    match.is_live = bool(row.get("is_live"))
    return match


def _upsert_snapshot(session: Session, market: Market, last_price: float | None,
                     volume: float | None, yes_bid: float | None = None,
                     yes_ask: float | None = None) -> None:
    # FLUSH ONLY WHEN THE MARKET IS NEW. This used to flush unconditionally, on
    # EVERY upsert -- and the soccer poller alone does 3,751 of them a cycle, so
    # that was 3,751 forced round trips where SQLAlchemy emits SQL for every
    # pending change instead of batching them into one commit.
    #
    # The flush exists for exactly one reason: a market added this cycle has no
    # id yet, and MarketSnapshot.market_id needs one. A market that already
    # exists in the DB already has its id populated, so flushing for it buys
    # nothing. Only a handful of markets are genuinely new on any given cycle.
    #
    # Measured 2026-08-26 via per-stage timing added to poller_soccer: the
    # soccer upsert stage was 764s for 3,751 rows.
    if market.id is None:
        session.flush()
 # the market needs an id before a snapshot can reference it
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=yes_bid, yes_ask=yes_ask, last_price=last_price, volume=volume,
    ))


def upsert_kalshi_cod_series_winner_market(session: Session, row: dict,
                                           cod_match_id: int | None) -> Market:
    """One market per team's YES side on a match-winner event.

    market_type is "series_winner", matching CS2/Valorant/LoL rather than
    inventing a CoD-specific name -- the settlement graders and the frontend
    label maps both key off this string, and a fifth spelling for the same
    concept is how one of them silently stops handling a sport."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"],
                        source_event_id=row["event_ticker"],
                        market_type="series_winner", sport="cod")
        session.add(market)
    market.cod_match_id = cod_match_id
    market.team = row.get("team_name")
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_polymarket_cod_match_winner_row(session: Session, row: dict,
                                           cod_match_id: int | None) -> Market:
    """One Market per (Polymarket condition, team side)."""
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker,
                        source_event_id=row["event_slug"],
                        market_type="series_winner", sport="cod")
        session.add(market)
    market.cod_match_id = cod_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     **quote_fields(row, row.get("last_price")))
    return market


def upsert_polymarket_cod_total_row(session: Session, row: dict,
                                    cod_match_id: int | None) -> Market:
    """Total maps played. Stored on the OVER side, matching every other sport's
    totals convention here -- the under is the complement, not a second row."""
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker,
                        source_event_id=row["event_slug"],
                        market_type="series_total", sport="cod")
        session.add(market)
    market.cod_match_id = cod_match_id
    market.side = "over"
    market.line = float(row["line"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     **quote_fields(row, row.get("last_price")))
    return market


def find_match_for_polymarket_event(session: Session, event_title: str,
                                    start_time: str | None) -> CodMatch | None:
    """Bind a Polymarket event to a breakingpoint fixture.

    Polymarket titles read "Call of Duty: OpTic Gaming vs 100 Thieves (BO7) -
    Esports World Cup Playoffs", so the teams are extracted from between the
    "Call of Duty:" prefix and the "(BOn)" suffix, then matched EXACTLY --
    the same discipline as the Kalshi bind, and for the same reason: a wrong
    bind here stakes the wrong team."""
    if not event_title:
        return None
    body = event_title.split(":", 1)[1] if ":" in event_title else event_title
    body = re.split(r"\s*\(BO\d\)", body, maxsplit=1)[0]
    parts = body.replace(" vs. ", " vs ").split(" vs ")
    if len(parts) != 2:
        return None
    return _match_for_teams(session, parts[0].strip(), parts[1].strip(), start_time)


def find_match_for_kalshi_event(session: Session, event_title: str,
                                start_time: str | None) -> CodMatch | None:
    """Bind a Kalshi event to a breakingpoint match.

    Kalshi titles read "100 Thieves vs. OpTic Gaming" and breakingpoint stores
    the same full names, so this is an exact-name join on both sides plus a
    date window -- no alias map and no fuzzy matching. Verified live: both open
    events bound with zero mapping.

    Deliberately EXACT rather than fuzzy. A wrong bind here stakes the wrong
    team, and this app has already been burned by a 0.92-similarity match
    (Rangers -> Angers). If a real spelling difference ever appears, add an
    explicit alias -- do not loosen this.
    """
    if not event_title:
        return None
    parts = event_title.replace(" vs. ", " vs ").split(" vs ")
    if len(parts) != 2:
        return None
    return _match_for_teams(session, parts[0].strip(), parts[1].strip(), start_time)


def _match_for_teams(session: Session, a: str, b: str,
                     start_time: str | None) -> CodMatch | None:
    """Shared by both platform binds. Unsettled fixtures only, exact team-set
    match, then narrowed by day when a start time is known -- two teams can
    meet more than once in a bracket."""
    day = (start_time or "")[:10]
    same_teams = [m for m in session.query(CodMatch).all()
                  if {m.team_a, m.team_b} == {a, b}]
    if not same_teams:
        return None

    unsettled = [m for m in same_teams if m.winner is None]
    if day:
        dated = [m for m in unsettled if (m.estimated_start_time or m.match_date or "")[:10] == day]
        if dated:
            return dated[0]
    if unsettled:
        return unsettled[0]

    # Fall back to a SETTLED fixture only on an exact day match. A market on a
    # match that just finished should read "this match has finished" rather
    # than "not bound to a fixture" -- the second is true but useless, and
    # looks like a matching failure. The exact-day requirement is what stops
    # this reaching back to an earlier meeting between the same two teams.
    if day:
        settled_today = [m for m in same_teams
                         if (m.estimated_start_time or m.match_date or "")[:10] == day]
        if settled_today:
            return settled_today[0]
    return None
