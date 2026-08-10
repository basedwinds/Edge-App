"""DB upsert layer for Valorant matches/maps/markets -- parallel to
market_catalog_soccer.py. vlr.gg's own live schedule listing IS the schedule
(see valorant_data.py's module docstring, same "the live listing IS the
schedule" pattern as Tennis/Soccer) -- find_or_create_upcoming_match derives
ValorantMatch rows directly from vlr.gg's own scrape, with Kalshi/Polymarket
matching onto that same row by team name (via
market_matcher_valorant.py::match_by_names_only) rather than creating a
duplicate.

market_type reuse of existing Market columns (no new columns needed, same
sparse-field-reuse discipline as every other sport in this app):
  - "map_winner": team = the team this row's YES favors, line = map_number
    (an integer stored in a Float column, same "repurposed field" pattern as
    MLB's F5 tie-side reuse) -- see elo_valorant.py's SeriesDistribution for
    why this needs the map NUMBER, not just a boolean per-map flag.
  - "series_winner": team = favored team, no line.
  - "series_handicap": team = favored team, line = map-margin threshold.
  - "series_total": team = None, line = total-maps threshold, side = "over".
  - "tournament_winner": team = favored team, group_label = tournament name,
    no valorant_match_id (season-long futures, same shape as every other
    sport's league_winner/title-futures market_type in this app).
"""
import datetime

from sqlalchemy.orm import Session

from app.clients.polymarket_client import quote_fields
from app.ingestion.start_times import apply_start
from app.ingestion.esports_event_name import clean_event_name
from app.db.models import Market, MarketSnapshot, ValorantMap, ValorantMatch, ValorantRosterChangeCache
from app.ingestion.market_matcher_valorant import match_by_names_only, team_names_match
from app.ingestion.series_orientation import oriented_result


def _load_upcoming_matches(session: Session) -> list[dict]:
    rows = session.query(ValorantMatch).filter(ValorantMatch.winner.is_(None)).all()
    return [{"id": r.id, "team_a": r.team_a, "team_b": r.team_b, "match_date": r.match_date} for r in rows]



# See market_catalog_lol.py for the real bug this prevents: matching a rematch on
# team names ALONE bound it to the earlier fixture's row and overwrote that row's
# date, orphaning an already-played match so it could never settle.
_SAME_FIXTURE_DAYS = 2


def _within_rematch_window(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True
    try:
        da = datetime.date.fromisoformat(a[:10])
        db = datetime.date.fromisoformat(b[:10])
    except ValueError:
        return True
    return abs((da - db).days) <= _SAME_FIXTURE_DAYS


def find_or_create_upcoming_match(
    session: Session, team_a_name: str, team_b_name: str,
    match_date: str | None = None, event_name: str | None = None,
) -> ValorantMatch | None:
    if not team_a_name or not team_b_name:
        return None
    upcoming = _load_upcoming_matches(session)
    upcoming = [m for m in upcoming if _within_rematch_window(m.get("match_date"), match_date)]
    found = match_by_names_only(team_a_name, team_b_name, upcoming)
    if found is not None:
        return session.get(ValorantMatch, found["id"])

    resolved_date = match_date or datetime.date.today().isoformat()
    source_match_id = f"live:{team_a_name}:{team_b_name}:{resolved_date}"
    # Same guard cs2/lol/soccer already carry, and the one sport still missing it
    # (tennis got it on 2026-08-03 after this exact failure took its whole
    # Polymarket refresh down). The existence check above reads a snapshot taken
    # once at the start of the run AND only sees UNFINISHED matches, so a row
    # created earlier in this same run -- or a finished row still holding the key
    # -- is invisible to it, and the insert then dies on "UNIQUE constraint
    # failed: valorant_matches.source, valorant_matches.source_match_id", aborting
    # the entire refresh rather than skipping one match. Re-check the DB itself.
    existing = (
        session.query(ValorantMatch)
        .filter_by(source="live", source_match_id=source_match_id)
        .one_or_none()
    )
    if existing is not None:
        return existing
    match = ValorantMatch(
        source="live", source_match_id=source_match_id,
        # A platform event title IS the matchup for these markets; the UI
        # renders event_name as the LEAGUE. See esports_event_name.py.
        event_name=clean_event_name(event_name, team_a_name, team_b_name), match_date=resolved_date,
        team_a=team_a_name, team_b=team_b_name,
    )
    session.add(match)
    session.flush()
    return match


def upsert_vlr_match(session: Session, row: dict) -> ValorantMatch:
    """Upserts a vlr.gg-scraped match row (see valorant_data.py). Real
    vlr.gg source_match_id takes priority over a "live:" synthetic row
    already created by a platform poller for the same real-world match --
    if a live row already matches these two team names, this fills it in
    with vlr.gg's own real source_match_id rather than creating a duplicate
    (same "whichever source sees it first, the other reconciles onto it"
    principle as find_or_create_upcoming_match, just entered from the
    vlr.gg side this time)."""
    match = session.query(ValorantMatch).filter_by(source="vlr", source_match_id=row["source_match_id"]).one_or_none()
    if match is None:
        # Same rematch window find_or_create_upcoming_match applies. Without it the
        # scraper matched on names ALONE, so a rematch bound to the earlier fixture
        # and moved its start into the future, orphaning the played match. This is
        # the path that actually did the damage on LoL.
        upcoming = [
            m for m in _load_upcoming_matches(session)
            if _within_rematch_window(m.get("match_date"), row.get("match_date"))
        ]
        found = match_by_names_only(row["team_a"], row["team_b"], upcoming)
        match = session.get(ValorantMatch, found["id"]) if found else None
    if match is None:
        match = ValorantMatch(source="vlr", source_match_id=row["source_match_id"], team_a=row["team_a"], team_b=row["team_b"], event_name=row["event_name"], match_date=row["match_date"])
        session.add(match)
    else:
        match.source = "vlr"
        match.source_match_id = row["source_match_id"]
    match.event_name = row["event_name"] or match.event_name
    # apply_start also keeps match_date in step with the real start -- see its
    # docstring for the user-reported wrong-date-in-the-UI case this closes.
    if match.winner is None:
        apply_start(match, row.get("estimated_start_time"), source="vlr")
    # match_by_names_only above matches the pair in EITHER order, so the row
    # can describe this fixture with the sides swapped. Re-orient before
    # writing -- writing these three positionally is what put "FALKE VENOM 2-0
    # GIANTX GC" on a match GIANTX GC won 2-0 and paid the wrong side.
    won_a, won_b, winner = oriented_result(row, match, team_names_match)
    if won_a is not None:
        match.maps_won_a = won_a
    if won_b is not None:
        match.maps_won_b = won_b
    if winner is not None:
        match.winner = winner
    session.flush()
    return match


def backfill_best_of(session: Session, match_id: int, max_map_number_seen: int) -> bool:
    """See ValorantMatch.best_of's own docstring -- backfilled from the real
    KXVALORANTMAP ladder rather than guessed upfront, same pattern as
    MmaFight.scheduled_rounds. Never overwrites an already-known value, and
    only accepts odd best_of values (1/3/5) -- a Bo3 clinched 2-0 only ever
    shows Map 1/Map 2 markets, so max_map_number_seen can UNDER-count a
    genuine Bo3/Bo5; only round UP to the next valid odd number when the
    observed max is even (2 -> 3, 4 -> 5), never down."""
    match = session.get(ValorantMatch, match_id)
    if match is None or match.best_of is not None:
        return False
    best_of = max_map_number_seen if max_map_number_seen % 2 == 1 else max_map_number_seen + 1
    match.best_of = min(best_of, 5)
    return True


def upsert_valorant_map(session: Session, valorant_match_id: int, map_number: int, team_a_score: int | None = None, team_b_score: int | None = None, winner: str | None = None, map_name: str | None = None) -> ValorantMap:
    m = session.query(ValorantMap).filter_by(valorant_match_id=valorant_match_id, map_number=map_number).one_or_none()
    if m is None:
        m = ValorantMap(valorant_match_id=valorant_match_id, map_number=map_number)
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


def upsert_kalshi_valorant_map_winner_market(session: Session, row: dict, valorant_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="map_winner", sport="valorant")
        session.add(market)
    market.valorant_match_id = valorant_match_id
    market.team = row["team_name"]
    market.line = float(row["map_number"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_valorant_series_winner_market(session: Session, row: dict, valorant_match_id: int | None) -> Market:
    """KXVALORANTGAME -- see kalshi_valorant_client.py's own real-bug note
    (found live 2026-07-20) for why this didn't exist until now. No `line`
    (whole-match winner, not per-map), same shape as
    market_catalog_cs2.py::upsert_kalshi_cs2_series_winner_market."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="series_winner", sport="valorant")
        session.add(market)
    market.valorant_match_id = valorant_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_valorant_tournament_winner_market(session: Session, row: dict) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="tournament_winner", sport="valorant")
        session.add(market)
    market.team = row["team_name"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_polymarket_valorant_match_winner_row(session: Session, row: dict, valorant_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_winner", sport="valorant")
        session.add(market)
    market.valorant_match_id = valorant_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
    return market


def upsert_polymarket_valorant_map_winner_row(session: Session, row: dict, valorant_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="map_winner", sport="valorant")
        session.add(market)
    market.valorant_match_id = valorant_match_id
    market.team = row["team_name"]
    market.line = float(row["map_number"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
    return market


def upsert_polymarket_valorant_total_row(session: Session, row: dict, valorant_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_total", sport="valorant")
        session.add(market)
    market.valorant_match_id = valorant_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    outcomes, prices = row.get("outcomes", []), row.get("outcome_prices", [])
    over_price = prices[outcomes.index("Over")] if "Over" in outcomes and len(prices) == len(outcomes) else None
    _upsert_snapshot(session, market, over_price, row.get("volume"),**quote_fields(row, over_price))
    return market


def upsert_polymarket_valorant_handicap_row(session: Session, row: dict, valorant_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_handicap", sport="valorant")
        session.add(market)
    market.valorant_match_id = valorant_match_id
    market.team = row["team_name"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
    return market


def upsert_polymarket_valorant_futures_row(session: Session, row: dict) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="tournament_winner", sport="valorant")
        session.add(market)
    market.team = row["team_name"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"),**quote_fields(row, row.get("yes_price")))
    return market


# Roster-change cache read/write helpers removed 2026-07-23 with the esports
# roster "Wait" badge (see market_catalog_cs2.py's note). Table left in place,
# no longer read or written.
