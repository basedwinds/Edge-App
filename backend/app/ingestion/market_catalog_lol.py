"""DB upsert layer for LoL matches/maps/markets -- parallel to
market_catalog_valorant.py. Real live inventory here (confirmed 2026-07-19)
is map_winner + series_total, both Kalshi-only -- no whole-series-winner
Kalshi ticker exists for LoL (unlike CS2's KXCS2GAME) and no Polymarket
match-level market type exists at all (checked live).

market_type reuse of existing Market columns, same discipline as every other
sport here:
  - "map_winner": team = favored team, line = map_number.
  - "series_total": team = None, line = total-maps threshold, side = "over".
  - "tournament_winner": team = favored team, group_label = tournament name,
    no lol_match_id (season-long futures).
"""
import datetime

from sqlalchemy.orm import Session

from app.db.models import LolMap, LolMatch, LolRosterChangeCache, Market, MarketSnapshot
from app.ingestion.market_matcher_lol import match_by_names_only, team_names_match


def _load_upcoming_matches(session: Session) -> list[dict]:
    rows = session.query(LolMatch).filter(LolMatch.winner.is_(None)).all()
    return [{"id": r.id, "team_a": r.team_a, "team_b": r.team_b} for r in rows]


def find_or_create_upcoming_match(
    session: Session, team_a_name: str, team_b_name: str,
    match_date: str | None = None, event_name: str | None = None,
) -> LolMatch | None:
    if not team_a_name or not team_b_name:
        return None
    upcoming = _load_upcoming_matches(session)
    found = match_by_names_only(team_a_name, team_b_name, upcoming)
    if found is not None:
        return session.get(LolMatch, found["id"])

    resolved_date = match_date or datetime.date.today().isoformat()
    match = LolMatch(
        source="live", source_match_id=f"live:{team_a_name}:{team_b_name}:{resolved_date}",
        event_name=event_name or "", match_date=resolved_date,
        team_a=team_a_name, team_b=team_b_name,
    )
    session.add(match)
    session.flush()
    return match


def upsert_leaguepedia_match(session: Session, row: dict) -> LolMatch:
    match = session.query(LolMatch).filter_by(source="leaguepedia", source_match_id=row["source_match_id"]).one_or_none()
    if match is None:
        upcoming = _load_upcoming_matches(session)
        found = match_by_names_only(row["team_a"], row["team_b"], upcoming)
        match = session.get(LolMatch, found["id"]) if found else None
    if match is None:
        match = LolMatch(source="leaguepedia", source_match_id=row["source_match_id"], team_a=row["team_a"], team_b=row["team_b"], event_name=row["event_name"], match_date=row["match_date"])
        session.add(match)
    else:
        match.source = "leaguepedia"
        match.source_match_id = row["source_match_id"]
    match.event_name = row["event_name"] or match.event_name
    if row.get("best_of") is not None:
        match.best_of = match.best_of or row["best_of"]
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


def backfill_best_of(session: Session, match_id: int, max_map_number_seen: int) -> bool:
    """See ValorantMatch.best_of's own docstring / market_catalog_valorant.py::
    backfill_best_of -- same real fix, LoL's own version. REAL BUG this
    fixes (found live 2026-07-20, user report: "not seeing any model %"
    for LoL): max_map_by_code was already being computed in
    refresh_kalshi_lol_markets() but never actually passed to a
    backfill_best_of call (LoL never had this function at all until now) --
    every LolMatch's best_of stayed None forever for matches that only ever
    arrived via the live-fallback path (find_or_create_upcoming_match,
    common here given how often Leaguepedia's own rate limit makes
    refresh_lol_matches() fail outright), and _game_model_prob() requires a
    real best_of to build a series distribution at all, so LoL could never
    show a model_prob for those matches no matter how good its Elo ratings
    were. Never overwrites an already-known value, and only accepts odd
    best_of values (1/3/5) -- a Bo3 clinched 2-0 only ever shows Map 1/Map 2
    markets, so max_map_number_seen can UNDER-count a genuine Bo3/Bo5; only
    round UP to the next valid odd number when the observed max is even
    (2 -> 3, 4 -> 5), never down."""
    match = session.get(LolMatch, match_id)
    if match is None or match.best_of is not None:
        return False
    best_of = max_map_number_seen if max_map_number_seen % 2 == 1 else max_map_number_seen + 1
    match.best_of = min(best_of, 5)
    return True


def upsert_lol_map(session: Session, lol_match_id: int, map_number: int, team_a_score: int | None = None, team_b_score: int | None = None, winner: str | None = None, map_name: str | None = None) -> LolMap:
    m = session.query(LolMap).filter_by(lol_match_id=lol_match_id, map_number=map_number).one_or_none()
    if m is None:
        m = LolMap(lol_match_id=lol_match_id, map_number=map_number)
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


def upsert_kalshi_lol_map_winner_market(session: Session, row: dict, lol_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="map_winner", sport="lol")
        session.add(market)
    market.lol_match_id = lol_match_id
    market.team = row["team_name"]
    market.line = float(row["map_number"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_lol_series_winner_market(session: Session, row: dict, lol_match_id: int | None) -> Market:
    """KXLOLGAME -- see kalshi_lol_client.py's own real-bug note (found live
    2026-07-20) for why this didn't exist until now. No `line` (whole-match
    winner, not per-map), same shape as
    market_catalog_valorant.py::upsert_kalshi_valorant_series_winner_market."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="series_winner", sport="lol")
        session.add(market)
    market.lol_match_id = lol_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_lol_total_maps_market(session: Session, row: dict, lol_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="series_total", sport="lol")
        session.add(market)
    market.lol_match_id = lol_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_lol_tournament_winner_market(session: Session, row: dict) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="tournament_winner", sport="lol")
        session.add(market)
    market.team = row["team_name"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


# --- Polymarket LoL upserts (2026-07-24) -- mirror market_catalog_valorant's own
# Polymarket upserts; source_ticker = conditionId(-team) so a Kalshi + Polymarket
# copy of the same real bet stay distinct rows that the recommender then dedups
# by proposition (crossPlatformKey), keeping whichever platform's edge is better.
def upsert_polymarket_lol_series_winner_row(session: Session, row: dict, lol_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_winner", sport="lol")
        session.add(market)
    market.lol_match_id = lol_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"))
    return market


def upsert_polymarket_lol_map_winner_row(session: Session, row: dict, lol_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="map_winner", sport="lol")
        session.add(market)
    market.lol_match_id = lol_match_id
    market.team = row["team_name"]
    market.line = float(row["map_number"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"))
    return market


def upsert_polymarket_lol_total_row(session: Session, row: dict, lol_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_total", sport="lol")
        session.add(market)
    market.lol_match_id = lol_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    outcomes, prices = row.get("outcomes", []), row.get("outcome_prices", [])
    over_price = prices[outcomes.index("Over")] if "Over" in outcomes and len(prices) == len(outcomes) else None
    _upsert_snapshot(session, market, over_price, row.get("volume"))
    return market


def upsert_polymarket_lol_handicap_row(session: Session, row: dict, lol_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_handicap", sport="lol")
        session.add(market)
    market.lol_match_id = lol_match_id
    market.team = row["team_name"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"))
    return market


def upsert_polymarket_lol_futures_row(session: Session, row: dict) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="tournament_winner", sport="lol")
        session.add(market)
    market.team = row["team_name"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"))
    return market


# Roster-change cache read/write helpers removed 2026-07-23 with the esports
# roster "Wait" badge (see market_catalog_cs2.py's note). Table left in place,
# no longer read or written.
