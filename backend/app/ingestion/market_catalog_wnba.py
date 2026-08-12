"""DB upsert layer for WNBA games/markets -- parallel to market_catalog_nba.py.
Every Market/PlacedBet row written here gets sport="wnba". The MarketSnapshot
written on each moneyline upsert is what gives WNBA real closing-price capture
(and therefore CLV) for free, the same mechanism every other sport uses.
"""
import datetime
import json

from sqlalchemy.orm import Session

from app.clients.polymarket_client import quote_fields
from app.db.models import Market, MarketSnapshot, WnbaGame, WnbaNewsAdjustmentCache
from app.ingestion.market_matcher_wnba import to_espn_abbr
from app.models.news_adjustment.schema import NewsAdjustment


def upsert_wnba_games(session: Session, games: list[dict]) -> int:
    count = 0
    for g in games:
        existing = session.get(WnbaGame, g["id"])
        if existing is None:
            existing = WnbaGame(id=g["id"])
            session.add(existing)
        existing.season = g["season"]
        existing.game_type = g["game_type"]
        existing.gameday = g["gameday"]
        existing.gametime = g.get("gametime") or None
        existing.away_team = g["away_team"]
        existing.home_team = g["home_team"]
        existing.away_score = g.get("away_score")
        existing.home_score = g.get("home_score")
        existing.away_rest = g.get("away_rest")
        existing.home_rest = g.get("home_rest")
        existing.location = g.get("location") or None
        existing.arena = g.get("arena") or None
        count += 1
    session.commit()
    return count


def upsert_kalshi_wnba_moneyline_market(session: Session, row: dict, wnba_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="moneyline",
            sport="wnba",
        )
        session.add(market)
    market.wnba_game_id = wnba_game_id
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.status = row.get("status") or "active"
    session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id,
            ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"),
            yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"),
            volume=row.get("volume"),
        )
    )
    return market


def upsert_polymarket_wnba_moneyline_row(session: Session, row: dict, wnba_game_id: str | None) -> Market:
    """One already-flattened per-team row from polymarket_wnba_client.

    The quote is stored via quote_fields, which orients the book's raw
    bid/ask against THIS row's own price -- both teams share one Polymarket
    market, so an unoriented bid/ask would be the other side's for one of them.
    Volume is carried through for real: it was hardcoded None across every
    Polymarket client until 2026-08-04, which silently disabled the
    has_real_trading gate for that whole platform.
    """
    source_ticker = f"{row['condition_id']}-{row['team_espn_abbr']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket",
            source_ticker=source_ticker,
            source_event_id=row["event_slug"],
            market_type="moneyline",
            sport="wnba",
        )
        session.add(market)
    market.wnba_game_id = wnba_game_id
    market.team = row["team_espn_abbr"]
    market.status = "active"
    session.flush()

    q = quote_fields(row, row.get("last_price"))
    session.add(
        MarketSnapshot(
            market_id=market.id,
            ts=datetime.datetime.utcnow(),
            yes_bid=q["yes_bid"],
            yes_ask=q["yes_ask"],
            last_price=row.get("last_price"),
            volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_wnba_spread_market(session: Session, row: dict, wnba_game_id: str | None) -> Market:
    """Per-team spread ladder ("<Team> wins the game by over X.5 points?").
    Mirrors upsert_kalshi_nba_spread_market -- same market_type/line/team
    convention, so the shared spread grader + game-ladder collapse work
    unchanged for WNBA."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="spread", sport="wnba",
        )
        session.add(market)
    market.wnba_game_id = wnba_game_id
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_wnba_team_total_market(session: Session, row: dict, wnba_game_id: str | None) -> Market:
    """Per-team total ladder ("Will <Team> score over X.5 points?").

    Identical row shape to the spread ladder -- per-team, carries a line -- so
    this is the spread upsert with a different market_type. side is "over"
    because Kalshi lists a single-sided ladder, matching the game total.
    """
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="team_total", sport="wnba",
        )
        session.add(market)
    market.wnba_game_id = wnba_game_id
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_wnba_total_market(session: Session, row: dict, wnba_game_id: str | None) -> Market:
    """Game-level total ladder. side="over" because Kalshi lists a single-sided
    ladder ("Over X.5 points scored?"), same as NBA/NFL."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="total", sport="wnba",
        )
        session.add(market)
    market.wnba_game_id = wnba_game_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_wnba_half_market(session: Session, row: dict, wnba_game_id: str | None,
                                   half: int, kind: str) -> Market:
    """Half markets. market_type is namespaced by half ("first_half_spread",
    "second_half_total", ...) rather than reusing the game types with a `half`
    column: the CLV-selection gate and the calibration report both bucket by
    market_type, and a 1H spread is a genuinely different question from a game
    spread -- lumping them would average two distributions that the measurement
    showed differ (1H margin std 10.39 vs the game's 14.21).

    `kind` is "winner" | "spread" | "total"; only spread/total carry a line, and
    only winner/spread carry a team."""
    prefix = "first_half" if half == 1 else "second_half"
    market_type = f"{prefix}_{kind}"
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="wnba",
        )
        session.add(market)
    market.wnba_game_id = wnba_game_id
    if kind in ("winner", "spread"):
        market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = row.get("line")
    if kind == "total":
        market.side = "over"   # Kalshi lists half totals as over-only ladders
    market.status = row.get("status") or "active"
    session.flush()
    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_wnba_win_total_market(session: Session, row: dict) -> Market:
    """Season win ladder -- season-long, so NO wnba_game_id (unlike every other
    WNBA market type, which is game-tied)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="win_total", sport="wnba",
        )
        session.add(market)
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_wnba_standings_market(session: Session, row: dict) -> Market:
    """#1 seed / playoff qualifier -- season-long like win_total, so NO
    wnba_game_id. market_type comes from the row's own market_kind so the two
    series cannot collide on one type."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=row["market_kind"], sport="wnba",
        )
        session.add(market)
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = None
    market.status = row.get("status") or "active"
    session.flush()
    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def wnba_news_cache_to_pydantic(cache) -> NewsAdjustment:
    return NewsAdjustment(
        adjustment_pct=cache.adjustment_pct,
        confidence=cache.confidence,
        factors=json.loads(cache.factors_json),
        requires_review=bool(cache.requires_review),
    )


def upsert_wnba_news_adjustment(session: Session, wnba_game_id: str, adj: NewsAdjustment):
    """One cached availability adjustment per tracked WNBA game."""
    row = session.get(WnbaNewsAdjustmentCache, wnba_game_id)
    if row is None:
        row = WnbaNewsAdjustmentCache(wnba_game_id=wnba_game_id)
        session.add(row)
    row.adjustment_pct = adj.adjustment_pct
    row.confidence = adj.confidence
    row.factors_json = json.dumps([f.model_dump() for f in adj.factors])
    row.requires_review = int(adj.requires_review)
    row.computed_at = datetime.datetime.utcnow()
    return row
