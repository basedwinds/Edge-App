"""DB upsert layer for MLB markets/games -- parallel to market_catalog_nba.py,
same architecture-decision reasoning as market_matcher_mlb.py. Every Market/
PlacedBet row this writes gets sport="mlb".
"""
import datetime
import json

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot, MlbGame, MlbNewsAdjustmentCache
from app.models.news_adjustment.schema import NewsAdjustment


def upsert_mlb_games(session: Session, games: list[dict]) -> int:
    count = 0
    for g in games:
        existing = session.get(MlbGame, g["id"])
        if existing is None:
            existing = MlbGame(id=g["id"])
            session.add(existing)
        existing.season = g["season"]
        existing.game_type = g["game_type"]
        existing.game_number = g.get("game_number", 1)
        existing.gameday = g["gameday"]
        existing.gametime = g.get("gametime") or None
        existing.away_team = g["away_team"]
        existing.home_team = g["home_team"]
        existing.away_score = g.get("away_score")
        existing.home_score = g.get("home_score")
        existing.away_probable_pitcher = g.get("away_probable_pitcher")
        existing.home_probable_pitcher = g.get("home_probable_pitcher")
        existing.away_probable_pitcher_id = g.get("away_probable_pitcher_id")
        existing.home_probable_pitcher_id = g.get("home_probable_pitcher_id")
        existing.away_rest = g.get("away_rest")
        existing.home_rest = g.get("home_rest")
        existing.venue = g.get("venue")
        count += 1
    session.commit()
    return count


def upsert_kalshi_mlb_moneyline_market(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="moneyline", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = row["team_abbr"]  # Kalshi's own MLB codes already match this app's canonical convention
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mlb_moneyline_row(session: Session, row: dict, mlb_game_id: str | None) -> Market | None:
    if row.get("team_abbr") is None:
        return None  # unresolved team name -- same "unknown, don't guess" convention as everywhere else
    source_ticker = f"{row['condition_id']}-{row['team_abbr']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="moneyline", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = row["team_abbr"]
    market.status = "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=None, yes_ask=None,
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mlb_spread_market(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="spread", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = row["team_abbr"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mlb_total_market(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="total", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = None
    market.line = row["line"]
    market.side = "over"  # Kalshi's total is a single-sided ladder ("Over X runs scored?")
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mlb_team_total_market(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="team_total", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = row["team_abbr"]
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mlb_spread_row(session: Session, row: dict, mlb_game_id: str | None) -> Market | None:
    if row.get("team_abbr") is None:
        return None
    source_ticker = f"{row['condition_id']}-{row['team_abbr']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="spread", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = row["team_abbr"]
    market.line = row["line"]
    market.status = "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=None, yes_ask=None,
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mlb_total_row(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['side']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="total", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = None
    market.line = row["line"]
    market.side = row["side"]
    market.status = "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=None, yes_ask=None,
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mlb_f5_market(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="f5", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    if row["outcome"] == "TIE":
        market.team = None
        market.side = "tie"
    else:
        market.team = row["outcome"]
        market.side = None
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mlb_f5_row(session: Session, row: dict, mlb_game_id: str | None) -> Market | None:
    if row["outcome"] is None:
        return None  # unresolved team name (resolve_polymarket_team_name failed) -- unknown, don't guess
    source_ticker = f"{row['condition_id']}-{row['outcome']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="f5", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    if row["outcome"] == "TIE":
        market.team = None
        market.side = "tie"
    else:
        market.team = row["outcome"]
        market.side = None
    market.status = "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=None, yes_ask=None,
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mlb_rfi_market(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="rfi", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = None
    market.side = "yes"  # Kalshi's RFI is a single market, "Yes" = a run scores in the 1st inning
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mlb_rfi_row(session: Session, row: dict, mlb_game_id: str | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['side']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="rfi", sport="mlb",
        )
        session.add(market)
    market.mlb_game_id = mlb_game_id
    market.team = None
    market.side = row["side"]  # "yes" = RFI happened, matching the real question polarity -- see get_rfi_markets
    market.status = "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=None, yes_ask=None,
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mlb_futures_market(session: Session, row: dict) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=row["market_kind"], sport="mlb",
        )
        session.add(market)
    market.team = row["team_abbr"]
    market.group_label = row.get("group_label")
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mlb_win_total_market(session: Session, row: dict) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="win_total", sport="mlb",
        )
        session.add(market)
    market.team = row["team"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mlb_futures_market(session: Session, row: dict) -> Market | None:
    if row.get("team_abbr") is None:
        return None
    source_ticker = f"{row['condition_id']}-{row['team_abbr']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["slug"],
            market_type=row["market_kind"], sport="mlb",
        )
        session.add(market)
    market.team = row["team_abbr"]
    market.group_label = row.get("group_label")
    market.status = "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=None, yes_ask=None,
        last_price=row.get("yes_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mlb_win_total_market(session: Session, row: dict) -> Market | None:
    if row.get("team_abbr") is None:
        return None
    source_ticker = f"{row['condition_id']}-{row['team_abbr']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=source_ticker,
            market_type="win_total", sport="mlb",
        )
        session.add(market)
    market.team = row["team_abbr"]
    market.line = row["line"]
    market.status = "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=None, yes_ask=None,
        last_price=row.get("yes_price"), volume=row.get("volume"),
    ))
    return market


def upsert_mlb_news_adjustment(session: Session, mlb_game_id: str, adjustment: NewsAdjustment) -> MlbNewsAdjustmentCache:
    cache = session.get(MlbNewsAdjustmentCache, mlb_game_id)
    if cache is None:
        cache = MlbNewsAdjustmentCache(mlb_game_id=mlb_game_id)
        session.add(cache)
    cache.adjustment_pct = adjustment.adjustment_pct
    cache.confidence = adjustment.confidence
    cache.factors_json = json.dumps([f.model_dump() for f in adjustment.factors])
    cache.requires_review = 1 if adjustment.requires_review else 0
    cache.computed_at = datetime.datetime.utcnow()
    session.commit()
    return cache


def get_mlb_news_adjustment_cache(session: Session, mlb_game_id: str) -> MlbNewsAdjustmentCache | None:
    return session.get(MlbNewsAdjustmentCache, mlb_game_id)


def mlb_news_cache_to_pydantic(cache: MlbNewsAdjustmentCache) -> NewsAdjustment:
    return NewsAdjustment(
        adjustment_pct=cache.adjustment_pct,
        confidence=cache.confidence,
        factors=json.loads(cache.factors_json),
        requires_review=bool(cache.requires_review),
    )
