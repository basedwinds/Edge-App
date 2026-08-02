"""DB upsert layer for WNBA games/markets -- parallel to market_catalog_nba.py.
Every Market/PlacedBet row written here gets sport="wnba". The MarketSnapshot
written on each moneyline upsert is what gives WNBA real closing-price capture
(and therefore CLV) for free, the same mechanism every other sport uses.
"""
import datetime

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot, WnbaGame
from app.ingestion.market_matcher_wnba import to_espn_abbr


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
