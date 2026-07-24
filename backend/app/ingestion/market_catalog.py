import datetime
import json

from sqlalchemy.orm import Session

from app.clients.polymarket_client import extract_market_prices
from app.db.models import Market, MarketSnapshot, NewsAdjustmentCache, NflGame
from app.ingestion.market_matcher import resolve_polymarket_team_name, to_nflverse_abbr
from app.models.news_adjustment.schema import NewsAdjustment


def upsert_nfl_games(session: Session, games: list[dict]) -> int:
    count = 0
    for g in games:
        existing = session.get(NflGame, g["game_id"])
        if existing is None:
            existing = NflGame(id=g["game_id"])
            session.add(existing)
        existing.season = g["season"]
        existing.week = g["week"]
        existing.game_type = g["game_type"]
        existing.gameday = g["gameday"]
        existing.gametime = g.get("gametime") or None
        existing.away_team = g["away_team"]
        existing.home_team = g["home_team"]
        existing.away_score = g.get("away_score")
        existing.home_score = g.get("home_score")
        existing.spread_line = g.get("spread_line")
        existing.total_line = g.get("total_line")
        existing.home_moneyline = g.get("home_moneyline")
        existing.away_moneyline = g.get("away_moneyline")
        existing.home_qb_name = g.get("home_qb_name") or None
        existing.away_qb_name = g.get("away_qb_name") or None
        existing.away_rest = g.get("away_rest")
        existing.home_rest = g.get("home_rest")
        existing.div_game = g.get("div_game")
        existing.roof = g.get("roof") or None
        existing.home_coach = g.get("home_coach") or None
        existing.away_coach = g.get("away_coach") or None
        existing.location = g.get("location") or None
        existing.stadium = g.get("stadium") or None
        existing.surface = g.get("surface") or None
        count += 1
    session.commit()
    return count


def upsert_kalshi_moneyline_market(
    session: Session, row: dict, nfl_game_id: str | None
) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="moneyline",
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = to_nflverse_abbr(row["team_abbr_kalshi"])
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_polymarket_moneyline_event(
    session: Session, event: dict, nfl_game_id: str | None, away_abbr: str, home_abbr: str
) -> list[Market]:
    """Polymarket lists one binary market per game with two outcomes
    (away, home), unlike Kalshi's two-separate-markets-per-game. We still
    store one Market row per team side so both sources join on
    (nfl_game_id, market_type, team)."""
    if not event.get("markets"):
        return []
    prices = extract_market_prices(event["markets"][0])
    outcomes = prices["outcomes"]
    outcome_prices = prices["outcome_prices"]
    if len(outcomes) != 2 or len(outcome_prices) != 2:
        return []

    team_abbrs = [away_abbr, home_abbr]  # Polymarket lists [away, home] matching title order
    results = []
    for i, team_abbr in enumerate(team_abbrs):
        source_ticker = f"{prices['condition_id']}-{team_abbr}"
        market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
        if market is None:
            market = Market(
                source="polymarket",
                source_ticker=source_ticker,
                source_event_id=prices["slug"],
                market_type="moneyline",
            )
            session.add(market)
        market.nfl_game_id = nfl_game_id
        market.team = team_abbr
        market.status = "active"
        session.flush()

        # outcomePrices is the most trustworthy fair-value signal here --
        # bestBid/bestAsk can be implausibly wide on thin Polymarket markets
        # (e.g. observed 0.08/0.89 on a real preseason game), so we deliberately
        # don't feed those into yes_bid/yes_ask and let the edge calculator
        # fall back to last_price instead of averaging a noisy spread. volume
        # is a different, more reliable signal (a real trade total, not a
        # quote) -- now wired through has_real_trading's zero-volume gate.
        snapshot = MarketSnapshot(
            market_id=market.id,
            ts=datetime.datetime.utcnow(),
            yes_bid=None,
            yes_ask=None,
            last_price=outcome_prices[i],
            volume=prices.get("volume"),
        )
        session.add(snapshot)
        results.append(market)
    return results


def upsert_kalshi_spread_market(session: Session, row: dict, nfl_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="spread"
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = to_nflverse_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_total_market(session: Session, row: dict, nfl_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="total"
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = None
    market.line = row["line"]
    market.side = "over"  # Kalshi's total is a single-sided ladder ("Over X points scored?")
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_team_total_market(session: Session, row: dict, nfl_game_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="team_total",
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = to_nflverse_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.side = "over"  # single-sided ladder, same convention as upsert_kalshi_total_market
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_half_spread_market(
    session: Session, row: dict, nfl_game_id: str | None, market_type: str
) -> Market:
    """market_type is "spread_1h" or "spread_2h" -- see poller.py."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type=market_type
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = to_nflverse_abbr(row["team_abbr_kalshi"])
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_half_total_market(
    session: Session, row: dict, nfl_game_id: str | None, market_type: str
) -> Market:
    """market_type is "total_1h" or "total_2h" -- see poller.py."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type=market_type
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_win_total_market(session: Session, row: dict) -> Market:
    """Season win total, over/under ladder per team -- see
    kalshi_client.py::get_win_total_markets. No nfl_game_id (season-long).
    `row["team"]` is already nflverse-resolved by the client (that series
    family has its own JAC/LA quirk, not the usual KALSHI_TO_NFLVERSE_ABBR
    one), so it's used directly instead of going through to_nflverse_abbr."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="win_total"
        )
        session.add(market)
    market.team = row["team"]
    market.line = row["line"]
    market.side = "over"  # single-sided ladder, same convention as upsert_kalshi_total_market
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_exact_win_total_market(session: Session, row: dict) -> Market:
    """Exact season win count per team -- see
    kalshi_client.py::get_exact_win_total_markets. `line` holds the exact
    win count (not a threshold); `side` stays None since this isn't an
    over/under market."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="exact_win_total",
        )
        session.add(market)
    market.team = row["team"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_wins_any_market(session: Session, row: dict) -> Market:
    """League-wide 'will any team hit N wins' ladder -- see
    kalshi_client.py::get_wins_any_markets. team=None like the game-level
    total ladder (upsert_kalshi_total_market), since it isn't tied to one
    team either."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="wins_any"
        )
        session.add(market)
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_polymarket_spread_market(session: Session, row: dict, nfl_game_id: str | None) -> Market | None:
    team_abbr = resolve_polymarket_team_name(row["team_full_name"])
    if team_abbr is None:
        return None
    source_ticker = f"{row['condition_id']}-{team_abbr}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="spread"
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = team_abbr
    market.line = row["line"]
    market.status = "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=None,
        yes_ask=None,
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_polymarket_total_market(session: Session, row: dict, nfl_game_id: str | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['side']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="total"
        )
        session.add(market)
    market.nfl_game_id = nfl_game_id
    market.team = None
    market.line = row["line"]
    market.side = row["side"]
    market.status = "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=None,
        yes_ask=None,
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_futures_market(session: Session, row: dict) -> Market:
    """Division winner / conference champion / 1-seed / Super Bowl champion /
    playoff qualifier -- see kalshi_client.py::get_futures_markets. No
    nfl_game_id (season-long, not tied to one game)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type=row["market_kind"],
        )
        session.add(market)
    market.team = to_nflverse_abbr(row["team_abbr_kalshi"])
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_stage_of_elim_market(session: Session, row: dict) -> Market:
    """KXNFLSTAGEOFELIM (stage of elimination) -- like the futures upsert, but
    keyed per (team, stage) so it needs `side` (the stage) set. Priced from
    season_sim's stage_exit_pct in list_futures. See
    kalshi_client.get_stage_of_elimination_markets."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="stage_of_elimination",
        )
        session.add(market)
    market.team = to_nflverse_abbr(row["team_abbr_kalshi"])
    market.side = row["stage"]          # reg | wc | div | conf | sb_loss | sb_win
    market.group_label = row["group_label"]
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


def upsert_polymarket_futures_market(session: Session, row: dict) -> Market | None:
    """Polymarket equivalent of upsert_kalshi_futures_market -- see
    polymarket_client.py::get_futures_markets. Team is keyed by full display
    name there (`groupItemTitle`), not an abbreviation -- resolve_polymarket_
    team_name also falls back to mascot-only names as a defensive measure
    (confirmed necessary for the spread/total markets below, which use that
    format instead)."""
    team_abbr = resolve_polymarket_team_name(row["team_full_name"])
    if team_abbr is None:
        return None
    market = session.query(Market).filter_by(source="polymarket", source_ticker=row["condition_id"]).one_or_none()
    if market is None:
        market = Market(
            source="polymarket",
            source_ticker=row["condition_id"],
            source_event_id=row["event_slug"],
            market_type=row["market_kind"],
        )
        session.add(market)
    market.team = team_abbr
    market.group_label = row["group_label"]
    market.status = "active"
    session.flush()

    # Same convention as moneyline: outcomePrices over bestBid/bestAsk,
    # which can be implausibly wide on thin Polymarket markets.
    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=None,
        yes_ask=None,
        last_price=row.get("yes_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_polymarket_week1_qb_market(session: Session, row: dict) -> Market | None:
    """Team-specific Week-1-starting-QB categorical market -- see
    polymarket_client.py::get_week1_qb_markets. `team` is the NFL team
    (resolved the same way futures markets are, via resolve_polymarket_team_name),
    `group_label` holds the CANDIDATE PLAYER's name (this market has no
    numeric line, so the existing team/group_label pair is repurposed here
    rather than adding a new column -- same "reuse the generic Market
    columns" pattern as every other market_type in this table)."""
    team_abbr = resolve_polymarket_team_name(row["team_full_name"])
    if team_abbr is None:
        return None
    source_ticker = f"{row['condition_id']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket",
            source_ticker=source_ticker,
            source_event_id=row["event_slug"],
            market_type="week1_qb",
        )
        session.add(market)
    market.team = team_abbr
    market.group_label = row["candidate_name"]
    market.status = "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=None,
        yes_ask=None,
        last_price=row.get("yes_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_season_stat_market(session: Session, row: dict, team_abbr: str | None, category: str) -> Market:
    """Season-total threshold ladder (KXNFLSEASON{PASSYDS,RSHYDS,RECYDS,
    REC,RECTD,RSHTD}) -- see kalshi_client.py::get_season_stat_markets and
    season_projections.py for the probability model. `team` holds the
    resolved team (may be None if unresolvable), `group_label` the
    candidate's name (same repurposing as the award/week1_qb markets),
    `line` the real numeric threshold, `side` always "over" (single-sided
    ladder, same convention as every other threshold ladder in this app).
    market_type is f"season_{category}" (e.g. "season_pass_yds")."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type=f"season_{category}",
        )
        session.add(market)
    market.team = team_abbr
    market.group_label = row["candidate_name"]
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_award_market(session: Session, row: dict, team_abbr: str | None, market_type: str) -> Market:
    """MVP / Coach of the Year -- see kalshi_client.py's
    get_mvp_markets/get_coach_of_year_markets. `team_abbr` is the resolved
    team (via awards.py's name-to-team reverse lookups, done by the caller
    since it needs season-specific depth-chart/coach data this function
    doesn't have) -- may be None for a candidate that couldn't be resolved
    (still stored so the market/price is trackable and visible in the UI,
    just with no model_prob computed for it, same "unknown = no guess"
    convention as everywhere else). `group_label` holds the candidate's
    name, same repurposing as upsert_polymarket_week1_qb_market."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type=market_type
        )
        session.add(market)
    market.team = team_abbr
    market.group_label = row["candidate_name"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_polymarket_award_market(session: Session, row: dict, team_abbr: str | None, market_type: str) -> Market:
    """Polymarket equivalent of upsert_kalshi_award_market -- see
    polymarket_client.py::get_mvp_markets."""
    market = session.query(Market).filter_by(source="polymarket", source_ticker=row["condition_id"]).one_or_none()
    if market is None:
        market = Market(
            source="polymarket",
            source_ticker=row["condition_id"],
            source_event_id=row["event_slug"],
            market_type=market_type,
        )
        session.add(market)
    market.team = team_abbr
    market.group_label = row["candidate_name"]
    market.status = "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=None,
        yes_ask=None,
        last_price=row.get("yes_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_division_wins_market(session: Session, row: dict) -> Market:
    """Ladder of 'division combines for N+ total wins' -- see
    kalshi_client.py::get_division_wins_markets. `team` is repurposed to
    hold the Kalshi-compact DIVISION code (e.g. "NFCWEST", not a team abbr)
    -- same "reuse the generic columns, document the repurposing" pattern as
    week1_qb's group_label-as-player-name."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="division_wins",
        )
        session.add(market)
    market.team = row["division_code"]
    market.group_label = row["group_label"]
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_div_extreme_market(session: Session, row: dict, market_type: str) -> Market:
    """div_least_wins / div_most_wins -- team-less-in-the-usual-sense, team
    field repurposed to hold the division code (same convention as
    division_wins above)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type=market_type
        )
        session.add(market)
    market.team = row["division_code"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_worst_to_first_market(session: Session, row: dict) -> Market:
    """Single league-wide binary market -- see
    kalshi_client.py::get_worst_to_first_markets."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="worst_to_first",
        )
        session.add(market)
    market.team = None
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_h2h_market(session: Session, row: dict) -> Market:
    """'Will team A out-win team B' -- see kalshi_client.py::get_h2h_wins_markets.
    The OPPONENT team isn't stored on the row directly (no spare column left
    to repurpose without ambiguity) -- markets.py derives it at query time by
    re-splitting `source_event_id` with the same `split_teams_blob` helper
    already used for game-ticker parsing elsewhere in this app, rather than
    adding a new column for one market type."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="h2h_wins"
        )
        session.add(market)
    market.team = to_nflverse_abbr(row["team_abbr_kalshi"])
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_kalshi_division_order_market(session: Session, row: dict) -> Market:
    """24-permutation-per-division market -- see
    kalshi_client.py::get_division_order_markets. `team` repurposed to hold
    the division code, `side` repurposed to hold the raw order_blob (needed
    at model_prob time to match against season_sim's simulated permutations)
    -- same "reuse the generic columns" pattern as this file's other
    non-standard market types."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi",
            source_ticker=row["ticker"],
            source_event_id=row["event_ticker"],
            market_type="division_order",
        )
        session.add(market)
    market.team = row["division_code"]
    market.group_label = row["group_label"]
    market.side = row["order_blob"]
    market.status = row.get("status") or "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def upsert_polymarket_undefeated_market(session: Session, row: dict) -> Market:
    """Single league-wide market (team=None) -- see
    polymarket_client.py::get_undefeated_market. No Kalshi equivalent."""
    market = session.query(Market).filter_by(source="polymarket", source_ticker=row["condition_id"]).one_or_none()
    if market is None:
        market = Market(
            source="polymarket",
            source_ticker=row["condition_id"],
            source_event_id=row["event_slug"],
            market_type="undefeated_season",
        )
        session.add(market)
    market.team = None
    market.group_label = row["group_label"]
    market.status = "active"
    session.flush()

    snapshot = MarketSnapshot(
        market_id=market.id,
        ts=datetime.datetime.utcnow(),
        yes_bid=None,
        yes_ask=None,
        last_price=row.get("yes_price"),
        volume=row.get("volume"),
    )
    session.add(snapshot)
    return market


def latest_snapshot(session: Session, market_id: int) -> MarketSnapshot | None:
    return (
        session.query(MarketSnapshot)
        .filter_by(market_id=market_id)
        .order_by(MarketSnapshot.ts.desc())
        .first()
    )


def get_previous_coach(session: Session, team: str, season: int, before_week: int) -> str | None:
    """The coach listed for `team`'s most recent PLAYED game earlier this
    season -- used to detect an in-season coaching change. Returns None for
    week 1 (nothing earlier this season to compare against) or if the team
    hasn't played yet, deliberately scoping this to in-season changes only,
    not offseason hires (a different, non-"interim bump" situation)."""
    game = (
        session.query(NflGame)
        .filter(
            NflGame.season == season,
            NflGame.week < before_week,
            NflGame.home_score.isnot(None),
            (NflGame.home_team == team) | (NflGame.away_team == team),
        )
        .order_by(NflGame.week.desc())
        .first()
    )
    if game is None:
        return None
    return game.home_coach if game.home_team == team else game.away_coach


def upsert_news_adjustment(
    session: Session,
    nfl_game_id: str,
    adjustment: NewsAdjustment,
    research_text: str,
    home_scoring_penalty_pp: float = 0.0,
    away_scoring_penalty_pp: float = 0.0,
) -> NewsAdjustmentCache:
    cache = session.get(NewsAdjustmentCache, nfl_game_id)
    if cache is None:
        cache = NewsAdjustmentCache(nfl_game_id=nfl_game_id)
        session.add(cache)
    cache.adjustment_pct = adjustment.adjustment_pct
    cache.confidence = adjustment.confidence
    cache.factors_json = json.dumps([f.model_dump() for f in adjustment.factors])
    cache.requires_review = 1 if adjustment.requires_review else 0
    cache.research_text = research_text
    cache.computed_at = datetime.datetime.utcnow()
    cache.home_scoring_penalty_pp = home_scoring_penalty_pp
    cache.away_scoring_penalty_pp = away_scoring_penalty_pp
    session.commit()
    return cache


def get_news_adjustment_cache(session: Session, nfl_game_id: str) -> NewsAdjustmentCache | None:
    return session.get(NewsAdjustmentCache, nfl_game_id)


def news_cache_to_pydantic(cache: NewsAdjustmentCache) -> NewsAdjustment:
    return NewsAdjustment(
        adjustment_pct=cache.adjustment_pct,
        confidence=cache.confidence,
        factors=json.loads(cache.factors_json),
        requires_review=bool(cache.requires_review),
    )
