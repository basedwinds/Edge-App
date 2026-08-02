"""DB upsert layer for Soccer matches/markets -- parallel to
market_catalog_tennis.py. Like Tennis, football-data.co.uk's schedule
situation was checked live (2026-07-19, see SoccerMatch's own docstring in
app/db/models.py: its `fixtures.csv` is real but far too thin/short-horizon
to serve as an external schedule) -- so `find_or_create_upcoming_match`
derives SoccerMatch rows directly from whichever platform's poller runs
first, same "the live listing IS the schedule" pattern as Tennis, and the
OTHER platform's poller matches onto that same row by team name (via
market_matcher_soccer.py::match_upcoming_soccer_match) rather than creating
a duplicate.

UNLIKE Tennis, Soccer has a real home/away distinction -- home_team/
away_team are stored and matched IN ORDER (see market_matcher_soccer.py's
docstring on why a swapped-order match is never accepted), and each match
maps to THREE Market rows (home/draw/away), not two.

Every Market/PlacedBet row this writes gets sport="soccer"."""
import datetime
import json

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot, SoccerMatch, SoccerNewsAdjustmentCache
from app.ingestion.market_matcher_soccer import match_upcoming_soccer_match
from app.models.news_adjustment.schema import NewsAdjustment


def _load_upcoming_matches(session: Session, league: str) -> list[dict]:
    rows = session.query(SoccerMatch).filter(
        SoccerMatch.league == league, SoccerMatch.result_ft.is_(None),
    ).all()
    return [{"id": r.id, "home_team": r.home_team, "away_team": r.away_team} for r in rows]


def _infer_season(league: str, match_date: str) -> str:
    """MLS runs within a single calendar year -- season is just the match's
    own year. The 5 European leagues span two calendar years (Aug-May) --
    same convention as football_data_client.py's historical season codes:
    a match in month >= 7 belongs to the season STARTING that year, a match
    in month < 7 belongs to the season that started the PRIOR year."""
    year, month = int(match_date[:4]), int(match_date[5:7])
    if league == "MLS":
        return str(year)
    start_year = year if month >= 7 else year - 1
    return f"{start_year}-{start_year + 1}"


def find_or_create_upcoming_match(
    session: Session, league: str, home_team_name: str, away_team_name: str,
    match_date: str | None = None,
) -> SoccerMatch | None:
    if not home_team_name or not away_team_name:
        return None
    upcoming = _load_upcoming_matches(session, league)
    found = match_upcoming_soccer_match(home_team_name, away_team_name, upcoming)
    if found is not None:
        return session.get(SoccerMatch, found["id"])

    resolved_date = match_date or datetime.date.today().isoformat()
    source_match_id = f"live:{league}:{home_team_name}:{away_team_name}:{resolved_date}"
    # Same bug fixed in market_catalog_cs2.py 2026-08-02, where it was hit for
    # real: the existence check above reads _load_upcoming_matches(), a snapshot
    # taken ONCE per run, so a fallback row created earlier in the SAME run is
    # invisible to it and the second insert dies on "UNIQUE constraint failed:
    # soccer_matches.source, soccer_matches.source_match_id", taking the whole
    # refresh down with it. Trigger is any two markets resolving to the same
    # league + team-pair + date. Fixed here proactively since the table carries
    # the identical constraint.
    existing = (
        session.query(SoccerMatch)
        .filter_by(source="live", source_match_id=source_match_id)
        .one_or_none()
    )
    if existing is not None:
        return existing
    match = SoccerMatch(
        source="live", source_match_id=source_match_id,
        league=league, season=_infer_season(league, resolved_date), match_date=resolved_date,
        home_team=home_team_name, away_team=away_team_name,
    )
    session.add(match)
    session.flush()
    return match


def update_match_estimated_start_time(match: SoccerMatch | None, estimated_start_time: str | None) -> None:
    """Same "genuine estimate, always overwrite while upcoming" reasoning as
    TennisMatch/MmaFight's own estimated_start_time handling."""
    if match is not None and match.result_ft is None and estimated_start_time:
        match.estimated_start_time = estimated_start_time


def _upsert_snapshot(session: Session, market: Market, last_price: float | None, volume: float | None,
                      yes_bid: float | None = None, yes_ask: float | None = None) -> None:
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=yes_bid, yes_ask=yes_ask, last_price=last_price, volume=volume,
    ))


def upsert_kalshi_soccer_moneyline_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="moneyline_3way", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.side = row["side"]  # "home" | "draw" | "away"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_polymarket_soccer_moneyline_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['side']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="moneyline_3way", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.side = row["side"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"))
    return market


def upsert_kalshi_soccer_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """market.team is the side this market's YES favors ("wins by more than
    line goals"); market.line holds the goal-margin threshold -- same
    "wins by more than line" convention as every other sport's spread in
    this app (e.g. game_lines_tennis.py::prob_game_spread_cover)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="game_spread", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_polymarket_soccer_spread_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="polymarket", source_ticker=row["condition_id"]).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=row["condition_id"], source_event_id=row["event_slug"],
            market_type="game_spread", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"))
    return market


def upsert_kalshi_soccer_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """market.line holds the total-goals threshold; market.team=None (game-
    level, not per-team); market.side="over" (single-sided ladder, same
    convention as this app's other totals)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="game_total", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_polymarket_soccer_total_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="game_total", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("over_price"), row.get("volume"))
    return market


def upsert_kalshi_soccer_btts_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """market.team=None, market.side="yes" -- single binary match-level
    market (see kalshi_soccer_client.py::get_btts_markets' own docstring:
    exactly one market per event, no per-team/per-line split)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="btts", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = None
    market.side = "yes"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_soccer_relegation_market(session: Session, row: dict) -> Market:
    """Season-long futures, no soccer_match_id -- same team-less-of-a-single-
    game shape as league_winner above."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="relegation", sport="soccer",
        )
        session.add(market)
    market.team = row["team"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_soccer_news_adjustment(session: Session, soccer_match_id: int, adjustment: NewsAdjustment) -> SoccerNewsAdjustmentCache:
    cache = session.get(SoccerNewsAdjustmentCache, soccer_match_id)
    if cache is None:
        cache = SoccerNewsAdjustmentCache(soccer_match_id=soccer_match_id)
        session.add(cache)
    cache.adjustment_pct = adjustment.adjustment_pct
    cache.confidence = adjustment.confidence
    cache.factors_json = json.dumps([f.model_dump() for f in adjustment.factors])
    cache.requires_review = 1 if adjustment.requires_review else 0
    cache.computed_at = datetime.datetime.utcnow()
    session.commit()
    return cache


def get_soccer_news_adjustment_cache(session: Session, soccer_match_id: int) -> SoccerNewsAdjustmentCache | None:
    return session.get(SoccerNewsAdjustmentCache, soccer_match_id)


def soccer_news_cache_to_pydantic(cache: SoccerNewsAdjustmentCache) -> NewsAdjustment:
    return NewsAdjustment(
        adjustment_pct=cache.adjustment_pct,
        confidence=cache.confidence,
        factors=json.loads(cache.factors_json),
        requires_review=bool(cache.requires_review),
    )


def upsert_kalshi_soccer_league_winner_market(session: Session, row: dict) -> Market:
    """No soccer_match_id -- a season-long futures market, not tied to one
    match (same "team-less-of-a-single-game" shape as every other sport's
    league_winner-style futures in this app)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="league_winner", sport="soccer",
        )
        session.add(market)
    market.team = row["team"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


# ---------------------------------------------------------------------------
# Second batch (added 2026-07-19): First Half / Second Half / First Team To
# Score / Correct Score / Team Total -- see kalshi_soccer_client.py/
# polymarket_soccer_client.py's own docstrings for the real live inventory
# audit that found these. Generic upsert helpers parameterized by
# market_type below, since the underlying Market row SHAPE repeats exactly
# across (kalshi, polymarket) x (first half, second half) for the winner/
# spread/total/btts family -- unlike the FIRST batch (moneyline/spread/
# total/btts), written before this shape repetition was as obvious, these
# are deliberately factored to avoid 8 near-identical copy-pasted
# functions. Still fully sport-specific (Soccer only), not a cross-sport
# generalization.
# ---------------------------------------------------------------------------

def _upsert_kalshi_3way_market(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    """Shared by first_half_winner/second_half_winner (and reusable for any
    future 3-way-shaped market_type) -- team/side/status/snapshot, same
    field usage as moneyline_3way."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.side = row["side"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_soccer_first_half_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_3way_market(session, row, soccer_match_id, "first_half_winner")


def upsert_kalshi_soccer_second_half_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_3way_market(session, row, soccer_match_id, "second_half_winner")


def upsert_kalshi_soccer_ftts_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """side is "home"|"away"|"none" (not "draw") -- FTTS's own tie-analogue,
    see kalshi_soccer_client.py::get_ftts_markets' own docstring."""
    return _upsert_kalshi_3way_market(session, row, soccer_match_id, "ftts")


def _upsert_kalshi_spread_shaped_market(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    """Shared by first_half_spread/second_half_spread -- team/line, same
    field usage as game_spread."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_soccer_first_half_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_spread_shaped_market(session, row, soccer_match_id, "first_half_spread")


def upsert_kalshi_soccer_second_half_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_spread_shaped_market(session, row, soccer_match_id, "second_half_spread")


def upsert_kalshi_soccer_team_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """Same field usage as game_spread (team + line), genuinely different
    semantics (this team's OWN total, not a margin) but no new Market
    columns needed -- model_prob dispatch in soccer_markets.py is what
    actually distinguishes them, keyed off market_type."""
    return _upsert_kalshi_spread_shaped_market(session, row, soccer_match_id, "team_total")


def _upsert_kalshi_total_shaped_market(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    """Shared by first_half_total/second_half_total -- line + side="over",
    same field usage as game_total."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_soccer_first_half_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_total_shaped_market(session, row, soccer_match_id, "first_half_total")


def upsert_kalshi_soccer_second_half_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_total_shaped_market(session, row, soccer_match_id, "second_half_total")


def _upsert_kalshi_btts_shaped_market(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    """Shared by first_half_btts/second_half_btts -- same shape as btts
    (side="yes", team=None)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = None
    market.side = "yes"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_soccer_first_half_btts_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_btts_shaped_market(session, row, soccer_match_id, "first_half_btts")


def upsert_kalshi_soccer_second_half_btts_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_kalshi_btts_shaped_market(session, row, soccer_match_id, "second_half_btts")


def upsert_kalshi_soccer_correct_score_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """Uses the new correct_score_home/correct_score_away columns (added
    2026-07-19, see Market's own docstring) -- no existing field could hold
    a two-integer outcome."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="correct_score", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.correct_score_home = row["home_score"]
    market.correct_score_away = row["away_score"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


# --- Polymarket side of the same second batch ---

def _upsert_polymarket_3way_market(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    source_ticker = f"{row['condition_id']}-{row['side']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.side = row["side"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"))
    return market


def upsert_polymarket_soccer_first_half_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_3way_market(session, row, soccer_match_id, "first_half_winner")


def upsert_polymarket_soccer_second_half_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_3way_market(session, row, soccer_match_id, "second_half_winner")


def upsert_polymarket_soccer_ftts_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_3way_market(session, row, soccer_match_id, "ftts")


def _upsert_polymarket_spread_shaped_row(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    source_ticker = f"{row['condition_id']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = row["team"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("over_price"), row.get("volume"))
    return market


def upsert_polymarket_soccer_team_total_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_spread_shaped_row(session, row, soccer_match_id, "team_total")


def upsert_polymarket_soccer_first_half_team_total_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_spread_shaped_row(session, row, soccer_match_id, "first_half_team_total")


def upsert_polymarket_soccer_second_half_team_total_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_spread_shaped_row(session, row, soccer_match_id, "second_half_team_total")


def _upsert_polymarket_total_shaped_row(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("over_price"), row.get("volume"))
    return market


def upsert_polymarket_soccer_first_half_total_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_total_shaped_row(session, row, soccer_match_id, "first_half_total")


def upsert_polymarket_soccer_second_half_total_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_total_shaped_row(session, row, soccer_match_id, "second_half_total")


def _upsert_polymarket_btts_shaped_row(session: Session, row: dict, soccer_match_id: int | None, market_type: str) -> Market:
    source_ticker = f"{row['condition_id']}-yes"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.team = None
    market.side = "yes"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"))
    return market


def upsert_polymarket_soccer_btts_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_btts_shaped_row(session, row, soccer_match_id, "btts")


def upsert_polymarket_soccer_first_half_btts_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_btts_shaped_row(session, row, soccer_match_id, "first_half_btts")


def upsert_polymarket_soccer_second_half_btts_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    return _upsert_polymarket_btts_shaped_row(session, row, soccer_match_id, "second_half_btts")


def upsert_polymarket_soccer_correct_score_row(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-yes"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="correct_score", sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.correct_score_home = row["home_score"]
    market.correct_score_away = row["away_score"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"))
    return market


def upsert_kalshi_soccer_top_n_market(session: Session, row: dict) -> Market:
    """Season-long futures, no soccer_match_id -- same team-less-of-a-
    single-game shape as league_winner/relegation above. market_type is
    the row's own real threshold ("top_half"/"top4"/"top2", see
    kalshi_soccer_client.py::TOP_N_SERIES)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=row["threshold"], sport="soccer",
        )
        session.add(market)
    market.team = row["team"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                      yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market
