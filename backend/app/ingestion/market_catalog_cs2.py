"""DB upsert layer for CS2 matches/maps/markets -- parallel to
market_catalog_valorant.py. liquipedia.net's own live schedule listing IS the
schedule (see cs2_data.py), covering both upcoming AND recently-decided
matches on one page (unlike vlr.gg's separate /matches vs /matches/results
split).

market_type reuse of existing Market columns (no new columns needed, same
sparse-field-reuse discipline as every other sport):
  - "series_winner": team = favored team, no line.
  - "series_total": team = None, line = total-maps threshold, side = "over".
  - "map_winner": team = favored team, line = map_number (currently zero
    real Kalshi inventory, see kalshi_cs2_client.py -- code ready regardless).
  - "tournament_winner": team = favored team, group_label = tournament name,
    no cs2_match_id (season-long futures; currently zero real Kalshi
    inventory too, same "ready, not yet populated" status).
"""
import datetime

from sqlalchemy.orm import Session

from app.db.models import Cs2Map, Cs2Match, Cs2RosterChangeCache, Market, MarketSnapshot
from app.ingestion.market_matcher_cs2 import match_by_names_only, team_names_match


def _load_upcoming_matches(session: Session) -> list[dict]:
    rows = session.query(Cs2Match).filter(Cs2Match.winner.is_(None)).all()
    return [{"id": r.id, "team_a": r.team_a, "team_b": r.team_b, "team_a_display": None, "team_b_display": None} for r in rows]


def find_or_create_upcoming_match(
    session: Session, team_a_name: str, team_b_name: str,
    match_date: str | None = None, event_name: str | None = None,
) -> Cs2Match | None:
    if not team_a_name or not team_b_name:
        return None
    upcoming = _load_upcoming_matches(session)
    found = match_by_names_only(team_a_name, team_b_name, upcoming)
    if found is not None:
        return session.get(Cs2Match, found["id"])

    resolved_date = match_date or datetime.date.today().isoformat()
    match = Cs2Match(
        source="live", source_match_id=f"live:{team_a_name}:{team_b_name}:{resolved_date}",
        event_name=event_name or "", match_date=resolved_date,
        team_a=team_a_name, team_b=team_b_name,
    )
    session.add(match)
    session.flush()
    return match


def upsert_liquipedia_match(session: Session, row: dict) -> Cs2Match:
    """Upserts a liquipedia.net-scraped match row (see cs2_data.py). Same
    "whichever source sees it first, the other reconciles onto it"
    reconciliation as market_catalog_valorant.py::upsert_vlr_match."""
    match = session.query(Cs2Match).filter_by(source="liquipedia", source_match_id=row["source_match_id"]).one_or_none()
    if match is None:
        upcoming = _load_upcoming_matches(session)
        found = match_by_names_only(row["team_a"], row["team_b"], upcoming)
        match = session.get(Cs2Match, found["id"]) if found else None
    if match is None:
        match = Cs2Match(source="liquipedia", source_match_id=row["source_match_id"], team_a=row["team_a"], team_b=row["team_b"], event_name=row["event_name"], match_date=row["match_date"])
        session.add(match)
    else:
        match.source = "liquipedia"
        match.source_match_id = row["source_match_id"]
    match.event_name = row["event_name"] or match.event_name
    if row.get("best_of") is not None:
        match.best_of = match.best_of or row["best_of"]  # Liquipedia states best_of upfront -- never overwrite a real known value, but fill if somehow missing
    if match.winner is None and row.get("estimated_start_time"):
        match.estimated_start_time = row["estimated_start_time"]
    if row.get("maps_won_a") is not None:
        match.maps_won_a = row["maps_won_a"]
    if row.get("maps_won_b") is not None:
        match.maps_won_b = row["maps_won_b"]
    if row.get("winner") is not None:
        match.winner = row["winner"]
    session.flush()
    return match


def backfill_best_of_from_total_maps_line(session: Session, match_id: int, line: float) -> bool:
    """REAL COVERAGE GAP this closes (found live 2026-07-20: 24/30 real open
    Kalshi CS2 series_winner matches had no model_prob at all, not just an
    unmatched-team problem -- every one of them had a real Cs2Match row
    (via find_or_create_upcoming_match's live-fallback path) but best_of was
    None, and _game_model_prob requires a real best_of to build a series
    distribution at all). Valorant/LoL both backfill best_of from the max
    map NUMBER seen across live per-map KXVALORANTMAP/KXLOLMAPWINNER
    markets (see their own backfill_best_of) -- that signal doesn't exist
    for CS2 (KXCS2MAPWINNER has zero real open markets, confirmed live, see
    kalshi_cs2_client.py's own docstring). KXCS2TOTALMAPS's own O/U line is
    a real, always-available substitute: a total-maps line is only ever
    sensible strictly between a series' real min and max possible map
    count -- 2.5 for a Bo3 (min 2, max 3), 3.5 or 4.5 for a Bo5 (min 3, max
    5) -- so the line value alone determines best_of directly, with no
    under-counting risk the per-map-ladder-depth signal has (a Bo3 clinched
    2-0 never even lists a Map 3 market, but a genuine Bo3 always still
    lists its own 2.5 total-maps line). Never overwrites an already-known
    value; any other line value is left alone rather than guessed."""
    match = session.get(Cs2Match, match_id)
    if match is None or match.best_of is not None:
        return False
    if line == 2.5:
        match.best_of = 3
    elif line in (3.5, 4.5):
        match.best_of = 5
    else:
        return False
    return True


def backfill_best_of(session: Session, match_id: int, max_map_number_seen: int) -> bool:
    """CS2's own version of Valorant/LoL's backfill_best_of (see their own
    docstrings) -- became usable 2026-07-20 once kalshi_cs2_client.py's
    real KXCS2MAP ticker fix gave CS2 real per-map ladder data for the
    first time (see that module's own docstring for the real-bug story).
    Never overwrites an already-known value (including one already set by
    backfill_best_of_from_total_maps_line above) and only accepts odd
    best_of values (1/3/5) -- a Bo3 clinched 2-0 only ever shows Map 1/Map 2
    markets, so max_map_number_seen can UNDER-count a genuine Bo3/Bo5; only
    round UP to the next valid odd number when the observed max is even
    (2 -> 3, 4 -> 5), never down."""
    match = session.get(Cs2Match, match_id)
    if match is None or match.best_of is not None:
        return False
    best_of = max_map_number_seen if max_map_number_seen % 2 == 1 else max_map_number_seen + 1
    match.best_of = min(best_of, 5)
    return True


def upsert_cs2_map(session: Session, cs2_match_id: int, map_number: int, team_a_score: int | None = None, team_b_score: int | None = None, winner: str | None = None, map_name: str | None = None) -> Cs2Map:
    m = session.query(Cs2Map).filter_by(cs2_match_id=cs2_match_id, map_number=map_number).one_or_none()
    if m is None:
        m = Cs2Map(cs2_match_id=cs2_match_id, map_number=map_number)
        session.add(m)
    if team_a_score is not None:
        m.team_a_score = team_a_score
    if team_b_score is not None:
        m.team_b_score = team_b_score
    if winner is not None:
        m.winner = winner
    if map_name is not None:
        m.map_name = map_name
    return m


def _upsert_snapshot(session: Session, market: Market, last_price: float | None, volume: float | None,
                      yes_bid: float | None = None, yes_ask: float | None = None) -> None:
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=yes_bid, yes_ask=yes_ask, last_price=last_price, volume=volume,
    ))


def upsert_kalshi_cs2_series_winner_market(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="series_winner", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cs2_total_maps_market(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="series_total", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cs2_map_winner_market(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="map_winner", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = row["team_name"]
    market.line = float(row["map_number"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cs2_tournament_winner_market(session: Session, row: dict) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="tournament_winner", sport="cs2")
        session.add(market)
    market.team = row["team_name"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


# Roster-change cache read/write helpers removed 2026-07-23 along with the
# esports roster "Wait" badge (no post-roster-change accuracy penalty found --
# see scripts/calibrate_cs2_roster_window.py). The Cs2RosterChangeCache table
# is left in place but is no longer read or written.
