"""DB upsert layer for CS2 matches/maps/markets -- parallel to
market_catalog_valorant.py. liquipedia.net's own live schedule listing IS the
schedule (see cs2_data.py), covering both upcoming AND recently-decided
matches on one page (unlike vlr.gg's separate /matches vs /matches/results
split).

market_type reuse of existing Market columns (no new columns needed, same
sparse-field-reuse discipline as every other sport):
  - "series_winner": team = favored team, no line.
  - "series_total": team = None, line = total-maps threshold, side = "over".
  - "map_winner": team = favored team, line = map_number (currently zero
    real Kalshi inventory, see kalshi_cs2_client.py -- code ready regardless;
    Polymarket DOES carry real inventory here, see below).
  - "series_handicap": team = that team's own side, line = its map handicap
    (Polymarket only -- Kalshi lists no CS2 handicap series).
  - "tournament_winner": team = favored team, group_label = tournament name,
    no cs2_match_id (season-long futures; currently zero real Kalshi
    inventory too, same "ready, not yet populated" status). KALSHI ONLY --
    Polymarket's CS2 tag carries no team-to-win-a-tournament futures, only
    props, and mistaking the latter for the former is an active trap
    documented in polymarket_cs2_client.py.

CORRECTION (2026-08-02): this module previously carried a note, referenced
from poller_cs2.py and catalog_scan.py, that "Polymarket has no CS2
match-outcome market type -- an honest inventory gap, not a build gap". That
was WRONG, and it was wrong because it was derived from the wrong tag: the app
queried `tag_slug=cs2`, which returns props only (roster changes, Valve
sticker trade-ups). The real head-to-head CS2 events are tagged
`counter-strike-2` -- 62 live match events carrying ~$2.7M of liquidity,
confirmed live. Polymarket CS2 match-outcome ingestion is now built (the four
upsert_polymarket_cs2_* functions below); see polymarket_cs2_client.py for the
tag story, the market-type inventory, and the staleness gating it needs.
"""
import datetime

from sqlalchemy.orm import Session

from app.clients.polymarket_client import quote_fields
from app.ingestion.start_times import apply_start
from app.db.models import Cs2Map, Cs2Match, Cs2RosterChangeCache, Market, MarketSnapshot
from app.ingestion.market_matcher_cs2 import match_by_names_only, team_names_match
from app.ingestion.series_orientation import oriented_result


def _load_upcoming_matches(session: Session) -> list[dict]:
    rows = session.query(Cs2Match).filter(Cs2Match.winner.is_(None)).all()
    return [{"id": r.id, "team_a": r.team_a, "team_b": r.team_b, "team_a_display": None, "team_b_display": None, "match_date": r.match_date} for r in rows]



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
) -> Cs2Match | None:
    if not team_a_name or not team_b_name:
        return None
    upcoming = _load_upcoming_matches(session)
    upcoming = [m for m in upcoming if _within_rematch_window(m.get("match_date"), match_date)]
    found = match_by_names_only(team_a_name, team_b_name, upcoming)
    if found is not None:
        return session.get(Cs2Match, found["id"])

    resolved_date = match_date or datetime.date.today().isoformat()
    source_match_id = f"live:{team_a_name}:{team_b_name}:{resolved_date}"
    # REAL BUG this fixes (surfaced live 2026-08-02, once the cs2 refresh was fast
    # enough to actually reach this code): the existence check above reads
    # _load_upcoming_matches(), a snapshot taken ONCE at the start of the run, so a
    # fallback row created earlier in the SAME run is invisible to it -- the second
    # insert then dies on "UNIQUE constraint failed: cs2_matches.source,
    # cs2_matches.source_match_id" and takes the whole refresh down with it. Real
    # trigger: Kalshi markets whose team name doesn't resolve share the placeholder
    # "???", so several genuinely different matches collapse onto one key/date.
    # Re-check against the DB (not the stale snapshot) before inserting.
    existing = (
        session.query(Cs2Match)
        .filter_by(source="live", source_match_id=source_match_id)
        .one_or_none()
    )
    if existing is not None:
        return existing
    match = Cs2Match(
        source="live", source_match_id=source_match_id,
        event_name=event_name or "", match_date=resolved_date,
        team_a=team_a_name, team_b=team_b_name,
    )
    session.add(match)
    session.flush()
    return match


def upsert_liquipedia_match(session: Session, row: dict) -> Cs2Match:
    """Upserts a liquipedia.net-scraped match row (see cs2_data.py). Same
    "whichever source sees it first, the other reconciles onto it"
    reconciliation as market_catalog_valorant.py::upsert_vlr_match."""
    match = session.query(Cs2Match).filter_by(source="liquipedia", source_match_id=row["source_match_id"]).one_or_none()
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
        match = session.get(Cs2Match, found["id"]) if found else None
    if match is None:
        match = Cs2Match(source="liquipedia", source_match_id=row["source_match_id"], team_a=row["team_a"], team_b=row["team_b"], event_name=row["event_name"], match_date=row["match_date"])
        session.add(match)
    else:
        match.source = "liquipedia"
        match.source_match_id = row["source_match_id"]
    match.event_name = row["event_name"] or match.event_name
    if row.get("best_of") is not None:
        match.best_of = match.best_of or row["best_of"]  # Liquipedia states best_of upfront -- never overwrite a real known value, but fill if somehow missing
    # apply_start also keeps match_date in step with the real start -- see its
    # docstring for the user-reported wrong-date-in-the-UI case this closes.
    if match.winner is None:
        apply_start(match, row.get("estimated_start_time"))
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


def backfill_best_of_from_total_maps_line(session: Session, match_id: int, line: float) -> bool:
    """REAL COVERAGE GAP this closes (found live 2026-07-20: 24/30 real open
    Kalshi CS2 series_winner matches had no model_prob at all, not just an
    unmatched-team problem -- every one of them had a real Cs2Match row
    (via find_or_create_upcoming_match's live-fallback path) but best_of was
    None, and _game_model_prob requires a real best_of to build a series
    distribution at all). Valorant/LoL both backfill best_of from the max
    map NUMBER seen across live per-map KXVALORANTMAP/KXLOLMAPWINNER
    markets (see their own backfill_best_of) -- that signal doesn't exist
    for CS2 (KXCS2MAPWINNER has zero real open markets, confirmed live, see
    kalshi_cs2_client.py's own docstring). KXCS2TOTALMAPS's own O/U line is
    a real, always-available substitute: a total-maps line is only ever
    sensible strictly between a series' real min and max possible map
    count -- 2.5 for a Bo3 (min 2, max 3), 3.5 or 4.5 for a Bo5 (min 3, max
    5) -- so the line value alone determines best_of directly, with no
    under-counting risk the per-map-ladder-depth signal has (a Bo3 clinched
    2-0 never even lists a Map 3 market, but a genuine Bo3 always still
    lists its own 2.5 total-maps line). Never overwrites an already-known
    value; any other line value is left alone rather than guessed."""
    match = session.get(Cs2Match, match_id)
    if match is None or match.best_of is not None:
        return False
    if line == 2.5:
        match.best_of = 3
    elif line in (3.5, 4.5):
        match.best_of = 5
    else:
        return False
    return True


def backfill_best_of(session: Session, match_id: int, max_map_number_seen: int) -> bool:
    """CS2's own version of Valorant/LoL's backfill_best_of (see their own
    docstrings) -- became usable 2026-07-20 once kalshi_cs2_client.py's
    real KXCS2MAP ticker fix gave CS2 real per-map ladder data for the
    first time (see that module's own docstring for the real-bug story).
    Never overwrites an already-known value (including one already set by
    backfill_best_of_from_total_maps_line above) and only accepts odd
    best_of values (1/3/5) -- a Bo3 clinched 2-0 only ever shows Map 1/Map 2
    markets, so max_map_number_seen can UNDER-count a genuine Bo3/Bo5; only
    round UP to the next valid odd number when the observed max is even
    (2 -> 3, 4 -> 5), never down."""
    match = session.get(Cs2Match, match_id)
    if match is None or match.best_of is not None:
        return False
    best_of = max_map_number_seen if max_map_number_seen % 2 == 1 else max_map_number_seen + 1
    match.best_of = min(best_of, 5)
    return True


def upsert_cs2_map(session: Session, cs2_match_id: int, map_number: int, team_a_score: int | None = None, team_b_score: int | None = None, winner: str | None = None, map_name: str | None = None) -> Cs2Map:
    m = session.query(Cs2Map).filter_by(cs2_match_id=cs2_match_id, map_number=map_number).one_or_none()
    if m is None:
        m = Cs2Map(cs2_match_id=cs2_match_id, map_number=map_number)
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


def upsert_kalshi_cs2_series_winner_market(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="series_winner", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cs2_total_maps_market(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="series_total", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cs2_map_winner_market(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="map_winner", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = row["team_name"]
    market.line = float(row["map_number"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cs2_tournament_winner_market(session: Session, row: dict) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"], market_type="tournament_winner", sport="cs2")
        session.add(market)
    market.team = row["team_name"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"), yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_polymarket_cs2_match_winner_row(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_winner", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = row["team_name"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
    return market


def upsert_polymarket_cs2_map_winner_row(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="map_winner", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = row["team_name"]
    market.line = float(row["map_number"])
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
    return market


def upsert_polymarket_cs2_total_row(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_total", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    outcomes, prices = row.get("outcomes", []), row.get("outcome_prices", [])
    over_price = prices[outcomes.index("Over")] if "Over" in outcomes and len(prices) == len(outcomes) else None
    _upsert_snapshot(session, market, over_price, row.get("volume"),**quote_fields(row, over_price))
    return market


def upsert_polymarket_cs2_handicap_row(session: Session, row: dict, cs2_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['team_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"], market_type="series_handicap", sport="cs2")
        session.add(market)
    market.cs2_match_id = cs2_match_id
    market.team = row["team_name"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
    return market


def set_best_of(session: Session, match_id: int, best_of: int | None) -> bool:
    """Polymarket's CS2 event titles state the series length outright
    ("... (BO3) - ESEA Advanced Europe"), so unlike this file's two other
    best_of helpers there is nothing to INFER here -- see
    polymarket_cs2_client.parse_best_of.

    Same never-overwrite-a-known-value discipline as backfill_best_of and
    backfill_best_of_from_total_maps_line, so whichever source resolves a
    match first wins and the other two are no-ops. Worth having as a third
    path because best_of is a hard gate on CS2 pricing at all (see
    poller_cs2.py's own coverage-gap note: 24/30 open matches once had no
    model_prob purely for want of it) and this is the only one of the three
    that reads it DIRECTLY rather than inferring it from a totals line or
    from how deep a per-map ladder happens to be listed."""
    if best_of is None:
        return False
    match = session.get(Cs2Match, match_id)
    if match is None or match.best_of is not None:
        return False
    match.best_of = best_of
    return True


# Roster-change cache read/write helpers removed 2026-07-23 along with the
# esports roster "Wait" badge (no post-roster-change accuracy penalty found --
# see scripts/calibrate_cs2_roster_window.py). The Cs2RosterChangeCache table
# is left in place but is no longer read or written.
