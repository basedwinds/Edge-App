"""DB upsert layer for college-football games/markets -- parallel to
market_catalog_wnba.py. Every Market row written here gets sport="cfb", and the
MarketSnapshot written on each upsert is what gives CFB real closing-price
capture (and therefore CLV) for free, the same mechanism every other sport uses.
"""
import datetime

from sqlalchemy.orm import Session

from app.db.models import CfbGame, Market, MarketSnapshot


def upsert_cfb_games(session: Session, games: list[dict]) -> int:
    count = 0
    for g in games:
        existing = session.get(CfbGame, g["id"])
        if existing is None:
            existing = CfbGame(id=g["id"])
            session.add(existing)
        existing.season = g["season"]
        existing.game_type = g["game_type"]
        existing.gameday = g["gameday"]
        existing.gametime = g.get("gametime") or None
        existing.away_team = g["away_team"]
        existing.home_team = g["home_team"]
        # Scores are only set once ESPN reports the game complete (see
        # espn_cfb_client.parse_event) -- never overwrite a recorded final with
        # None if a later fetch happens to omit it.
        if g.get("home_score") is not None:
            existing.home_score = g["home_score"]
        if g.get("away_score") is not None:
            existing.away_score = g["away_score"]
        existing.neutral = 1 if g.get("neutral") else 0
        existing.venue = g.get("venue") or None
        count += 1
    session.commit()
    return count


def upsert_kalshi_cfb_moneyline_market(session: Session, row: dict, cfb_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="moneyline",
            sport="cfb",
        )
        session.add(market)
    market.cfb_game_id = cfb_game_id
    # Already resolved to an ESPN abbreviation by market_matcher_cfb.resolve_team
    # before it reaches here -- storing the raw Kalshi code would break the join
    # to CfbGame.home_team/away_team.
    market.team = row["team"]
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


def upsert_kalshi_cfb_win_total_market(session: Session, row: dict, team: str) -> Market:
    """Season win-total ladder. No cfb_game_id -- this is a season-long market,
    not tied to any single game, same shape as the soccer team-points ladders."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="win_total",
            sport="cfb",
        )
        session.add(market)
    market.team = team
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_cfb_conference_market(session: Session, row: dict, team: str, market_type: str) -> Market:
    """Conference futures (champion / championship qualifier / regular-season
    top-N). Season-long, so no cfb_game_id. `line` carries the top-N depth for
    regtop markets and is null for the other two."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type=market_type,
            sport="cfb",
        )
        session.add(market)
    market.team = team
    market.line = row.get("line")
    market.group_label = row.get("series")
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    ))
    return market
