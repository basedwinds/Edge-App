"""DB upsert layer for NBA markets/games -- parallel to market_catalog.py
(NFL), same architecture-decision reasoning as market_matcher_nba.py.
Every Market/PlacedBet row this writes gets sport="nba" (see
db/models.py::Market.sport for why that column exists).
"""
import datetime
import json

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot, NbaCoachSnapshot, NbaGame, NbaNewsAdjustmentCache
from app.ingestion.market_matcher_nba import to_espn_abbr
from app.models.news_adjustment.schema import NewsAdjustment


def upsert_nba_games(session: Session, games: list[dict]) -> int:
    """Handles both nba_data.py's regular-season/playoff rows (id/season/
    game_type/gameday/... shape) and nba_summer_league_data.py's Summer
    League rows -- same dict shape (game_type="SUMMER" is just another
    value), so one upsert function covers both sources."""
    count = 0
    for g in games:
        existing = session.get(NbaGame, g["id"])
        if existing is None:
            existing = NbaGame(id=g["id"])
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


def upsert_kalshi_nba_moneyline_market(session: Session, row: dict, nba_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="moneyline",
            sport="nba",
        )
        session.add(market)
    market.nba_game_id = nba_game_id
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
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


def upsert_polymarket_nba_moneyline_row(session: Session, row: dict, nba_game_id: str | None) -> Market:
    """Takes one already-flattened per-team row (see
    polymarket_nba_client.py::get_summer_league_moneyline_markets, which
    returns one row per team rather than NFL's whole-event-with-outcomes
    shape) -- simpler upsert than NFL's event-based version since the
    flattening already happened in the client."""
    source_ticker = f"{row['condition_id']}-{row['team_espn_abbr']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket",
            source_ticker=source_ticker,
            source_event_id=row["event_slug"],
            market_type="moneyline",
            sport="nba",
        )
        session.add(market)
    market.nba_game_id = nba_game_id
    market.team = row["team_espn_abbr"]
    market.status = "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
        session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id,
            ts=datetime.datetime.utcnow(),
            yes_bid=None,
            yes_ask=None,
            last_price=row.get("last_price"),
            volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_nba_spread_market(session: Session, row: dict, nba_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="spread", sport="nba",
        )
        session.add(market)
    market.nba_game_id = nba_game_id
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
        session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_nba_total_market(session: Session, row: dict, nba_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="total", sport="nba",
        )
        session.add(market)
    market.nba_game_id = nba_game_id
    market.team = None
    market.line = row["line"]
    market.side = "over"  # Kalshi's total is a single-sided ladder ("Over X points scored?")
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
        session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_nba_team_total_market(session: Session, row: dict, nba_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="team_total", sport="nba",
        )
        session.add(market)
    market.nba_game_id = nba_game_id
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.side = "over"  # single-sided ladder, same convention as total
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
        session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_nba_half_spread_market(session: Session, row: dict, nba_game_id: str | None, market_type: str) -> Market:
    """market_type is "spread_1h" or "spread_2h" -- see poller_nba.py."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="nba",
        )
        session.add(market)
    market.nba_game_id = nba_game_id
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
        session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_nba_half_total_market(session: Session, row: dict, nba_game_id: str | None, market_type: str) -> Market:
    """market_type is "total_1h" or "total_2h"."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="nba",
        )
        session.add(market)
    market.nba_game_id = nba_game_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
        session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id, ts=datetime.datetime.utcnow(),
            yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
            last_price=row.get("last_price"), volume=row.get("volume"),
        )
    )
    return market


def upsert_kalshi_nba_futures_market(session: Session, row: dict) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type=row["market_kind"],
            sport="nba",
        )
        session.add(market)
    market.team = to_espn_abbr(row["team_abbr_kalshi"])
    market.group_label = row.get("group_label")
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
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


def upsert_kalshi_nba_win_total_market(session: Session, row: dict) -> Market:
    """Win-total ladder (KXNBAWINS-{team}, 0 open right now -- season starts
    October, same "not listed this far out" pattern every other regular-
    season-adjacent market in this app was in before one opened) -- built
    now so it activates automatically the moment Kalshi lists it, same
    reasoning as building spread/total ingestion for NFL before real markets
    existed to verify against."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="win_total",
            sport="nba",
        )
        session.add(market)
    market.team = row["team"]  # already ESPN-resolved by get_win_total_markets
    market.line = row["line"]
    market.status = row.get("status") or "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
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


def upsert_polymarket_nba_futures_market(session: Session, row: dict) -> Market | None:
    if row.get("team_espn_abbr") is None:
        return None  # unresolved team name -- same "unknown, don't guess" convention as everywhere else
    source_ticker = f"{row['condition_id']}-{row['team_espn_abbr']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket",
            source_ticker=source_ticker,
            source_event_id=row["slug"],
            market_type=row["market_kind"],
            sport="nba",
        )
        session.add(market)
    market.team = row["team_espn_abbr"]
    market.group_label = row.get("group_label")
    market.status = "active"
    # Flush only for a market that has no id yet -- see market_catalog_soccer's
    # copy of this note. Flushing on EVERY upsert forces a round trip per row
    # instead of one batched commit, and the id it exists to populate is already
    # set on any market that was not created this cycle.
    if market.id is None:
        session.flush()

    session.add(
        MarketSnapshot(
            market_id=market.id,
            ts=datetime.datetime.utcnow(),
            yes_bid=None,
            yes_ask=None,
            last_price=row.get("yes_price"),
            volume=row.get("volume"),
        )
    )
    return market


def upsert_nba_news_adjustment(session: Session, nba_game_id: str, adjustment: NewsAdjustment) -> NbaNewsAdjustmentCache:
    cache = session.get(NbaNewsAdjustmentCache, nba_game_id)
    if cache is None:
        cache = NbaNewsAdjustmentCache(nba_game_id=nba_game_id)
        session.add(cache)
    cache.adjustment_pct = adjustment.adjustment_pct
    cache.confidence = adjustment.confidence
    cache.factors_json = json.dumps([f.model_dump() for f in adjustment.factors])
    cache.requires_review = 1 if adjustment.requires_review else 0
    cache.computed_at = datetime.datetime.utcnow()
    session.commit()
    return cache


def get_nba_news_adjustment_cache(session: Session, nba_game_id: str) -> NbaNewsAdjustmentCache | None:
    return session.get(NbaNewsAdjustmentCache, nba_game_id)


def nba_news_cache_to_pydantic(cache: NbaNewsAdjustmentCache) -> NewsAdjustment:
    return NewsAdjustment(
        adjustment_pct=cache.adjustment_pct,
        confidence=cache.confidence,
        factors=json.loads(cache.factors_json),
        requires_review=bool(cache.requires_review),
    )


def upsert_coach_snapshot(session: Session, team: str, coach_name: str, season: int) -> tuple[NbaCoachSnapshot, bool]:
    """Returns (snapshot, changed) -- changed=True the FIRST poll cycle a
    new coach_name is observed for this team (the row's `since` is reset to
    now() at that moment, and previous_coach_name records what it changed
    FROM), False on every subsequent cycle while it stays the same. See
    NbaCoachSnapshot's own docstring for why `since` is an "app-observed"
    timestamp, not the real hire date.

    previous_coach_name stays NULL on this app's first-ever observation of a
    team -- that's the signal coach_rules_nba.py uses to tell "no real
    history yet" apart from "a genuine transition happened," since without
    it every team would look like a fresh coaching change for the first
    ~45 days after this feature ships (whenever every row is first created)."""
    row = session.get(NbaCoachSnapshot, team)
    if row is None:
        row = NbaCoachSnapshot(team=team, coach_name=coach_name, season=season, since=datetime.datetime.utcnow())
        session.add(row)
        session.commit()
        return row, False  # first time this team has ever been observed -- not a "change" to react to
    changed = row.coach_name != coach_name
    if changed:
        row.previous_coach_name = row.coach_name
        row.coach_name = coach_name
        row.season = season
        row.since = datetime.datetime.utcnow()
        session.commit()
    return row, changed


def get_coach_snapshot(session: Session, team: str) -> NbaCoachSnapshot | None:
    return session.get(NbaCoachSnapshot, team)
