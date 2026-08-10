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

from app.clients.polymarket_client import quote_fields
from app.ingestion.start_times import apply_start
from app.ingestion.esports_event_name import clean_event_name
from app.db.models import LolMap, LolMatch, LolRosterChangeCache, Market, MarketSnapshot
from app.ingestion.market_matcher_lol import match_by_names_only, team_names_match
from app.ingestion.series_orientation import oriented_result


def _load_upcoming_matches(session: Session) -> list[dict]:
    rows = session.query(LolMatch).filter(LolMatch.winner.is_(None)).all()
    return [{"id": r.id, "team_a": r.team_a, "team_b": r.team_b, "match_date": r.match_date} for r in rows]


# How far apart two fixtures between the SAME two teams may be and still be
# treated as the same match. A single fixture's date can wobble by a day across
# sources/timezones; a rematch is separated by many.
_SAME_FIXTURE_DAYS = 2


def _within_rematch_window(a: str | None, b: str | None) -> bool:
    """True when two match_dates are close enough to be the same fixture. Unknown
    dates fall back to True, preserving the old name-only behaviour rather than
    silently splitting a row we cannot date."""
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
) -> LolMatch | None:
    if not team_a_name or not team_b_name:
        return None
    # REAL BUG (user-reported 2026-08-03): matching on team names ALONE meant a
    # REMATCH bound to the earlier fixture's row and overwrote its date.
    # Invictus Gaming vs LNG Esports was played 2026-08-02 and bet on; because
    # the result had not been scraped the row still had winner=None, so it was
    # still "upcoming", so the 2026-08-09 rematch matched it and moved its start
    # forward. The played match was then orphaned -- never settled, and invisible
    # to any "past its start" check, since its own row claimed a future date.
    #
    # Restricting candidates to fixtures near the incoming date keeps the
    # legitimate case (several markets for ONE match reusing one row) while
    # splitting genuine rematches into their own rows.
    upcoming = [
        m for m in _load_upcoming_matches(session)
        if _within_rematch_window(m.get("match_date"), match_date)
    ]
    found = match_by_names_only(team_a_name, team_b_name, upcoming)
    if found is not None:
        return session.get(LolMatch, found["id"])

    resolved_date = match_date or datetime.date.today().isoformat()
    source_match_id = f"live:{team_a_name}:{team_b_name}:{resolved_date}"
    # Same bug fixed in market_catalog_cs2.py 2026-08-02, where it was hit for
    # real: the existence check above reads _load_upcoming_matches(), a snapshot
    # taken ONCE per run, so a fallback row created earlier in the SAME run is
    # invisible to it and the second insert dies on "UNIQUE constraint failed:
    # lol_matches.source, lol_matches.source_match_id", taking the whole refresh
    # down with it. Trigger is any two markets that resolve to the same team-pair
    # + date -- notably unresolved team names collapsing onto one placeholder.
    # Fixed here proactively since the table carries the identical constraint.
    existing = (
        session.query(LolMatch)
        .filter_by(source="live", source_match_id=source_match_id)
        .one_or_none()
    )
    if existing is not None:
        return existing
    match = LolMatch(
        source="live", source_match_id=source_match_id,
        # A platform event title IS the matchup for these markets; the UI
        # renders event_name as the LEAGUE. See esports_event_name.py.
        event_name=clean_event_name(event_name, team_a_name, team_b_name), match_date=resolved_date,
        team_a=team_a_name, team_b=team_b_name,
    )
    session.add(match)
    session.flush()
    return match


def upsert_leaguepedia_match(session: Session, row: dict) -> LolMatch:
    match = session.query(LolMatch).filter_by(source="leaguepedia", source_match_id=row["source_match_id"]).one_or_none()
    if match is None:
        # SAME rematch window find_or_create_upcoming_match applies. Without it
        # this fallback matched on team names ALONE, so the scraper bound a
        # rematch to the earlier fixture's row and then moved that row's start
        # into the future -- orphaning the played match. This is the path that
        # actually did the damage to Invictus Gaming vs LNG Esports; the poller
        # side had already been fixed, which is exactly why the row kept coming
        # back after being repaired.
        upcoming = [
            m for m in _load_upcoming_matches(session)
            if _within_rematch_window(m.get("match_date"), row.get("match_date"))
        ]
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
    # apply_start keeps match_date in step with the real start -- the rule that
    # used to live inline here (Invictus Gaming vs LNG showed 2026-07-24 while
    # its real start was 2026-08-02, user-reported). It was only ever applied on
    # THIS path, so the same staleness kept reappearing everywhere else; see
    # start_times.apply_start for the Valorant case that forced generalising it.
    if match.winner is None:
        apply_start(match, row.get("estimated_start_time"), source="leaguepedia")
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
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
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
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
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
    _upsert_snapshot(session, market, over_price, row.get("volume"),**quote_fields(row, over_price))
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
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
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
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"),**quote_fields(row, row.get("yes_price")))
    return market


# Roster-change cache read/write helpers removed 2026-07-23 with the esports
# roster "Wait" badge (see market_catalog_cs2.py's note). Table left in place,
# no longer read or written.
