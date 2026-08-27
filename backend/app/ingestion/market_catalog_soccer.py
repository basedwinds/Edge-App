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
import logging

from sqlalchemy import or_ as _or
from sqlalchemy.orm import Session

from app.clients.polymarket_client import quote_fields
from app.db.models import Market, MarketSnapshot, SoccerMatch, SoccerNewsAdjustmentCache
from app.ingestion.market_matcher_soccer import canonical_team_key, match_upcoming_soccer_match
from app.models.news_adjustment.schema import NewsAdjustment

log = logging.getLogger("market_catalog_soccer")


def _load_upcoming_matches(session: Session, league: str) -> list[dict]:
    # EXCLUDE FIXTURES ALREADY MERGED AWAY (2026-08-18).
    #
    # scripts/dedupe_soccer_fixtures.py repoints a duplicate's bets and markets
    # onto the surviving row and tags the loser's source_match_id ":dup-of-N".
    # It does NOT delete it, deliberately -- deleting would orphan anything that
    # still referenced it. But the tagged row stayed a CANDIDATE here, so it
    # matched its own names on the very next poll and started collecting fresh
    # markets and bets again. That is why 49 bets were still sitting on
    # dup-tagged rows after previous merges: the merge worked and then silently
    # un-did itself.
    #
    # A tagged row is never the right answer to "which fixture is this?", so it
    # is filtered here rather than at each of the several call sites.
    rows = session.query(SoccerMatch).filter(
        SoccerMatch.league == league, SoccerMatch.result_ft.is_(None),
        _or(SoccerMatch.source_match_id.is_(None),
            SoccerMatch.source_match_id.notlike("%:dup-of-%")),
    ).all()
    return [{"id": r.id, "home_team": r.home_team, "away_team": r.away_team,
             "match_date": r.match_date, "estimated_start_time": r.estimated_start_time}
            for r in rows]


def _kickoff_day(match_date, estimated_start_time):
    """Real kickoff DAY. estimated_start_time wins when present -- a live row's
    match_date is the SCRAPE date and can sit days from kickoff (measured up to
    7), which is exactly why a date-equality join on it would not work."""
    for v in (estimated_start_time, match_date):
        if not v:
            continue
        try:
            return datetime.date.fromisoformat(str(v)[:10])
        except ValueError:
            continue
    return None


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
    match_date: str | None = None, start_time: str | None = None,
) -> SoccerMatch | None:
    if not home_team_name or not away_team_name:
        return None
    upcoming = _load_upcoming_matches(session, league)
    found = match_upcoming_soccer_match(home_team_name, away_team_name, upcoming, league)
    if found is not None:
        existing = session.get(SoccerMatch, found["id"])
        # BACKFILL A KICKOFF THE ROW NEVER GOT.
        #
        # A fixture ingested before its platform published a start time is
        # created with estimated_start_time NULL and match_date set to the
        # SCRAPE day. Every later ingest found it here and returned it
        # unchanged, so the kickoff never landed -- and the markets, which
        # attach to this row, stayed on a fixture with no start time forever.
        #
        # Measured on Leagues Cup 2026-08-10: 23 of 30 rows had no start time,
        # and ALL 252 active Leagues Cup markets pointed at those 23. Those
        # markets are priced and stakeable, so the "already started" gates could
        # not bind for a single one of them -- the same exposure as the tennis
        # Santos incident, and soccer has no calibrated live-trading arm to
        # catch it either.
        #
        # Only FILLS A HOLE: never overwrites a time the row already has, so it
        # cannot fight the ESPN correction path (which exists precisely to
        # replace a wrong platform time and tags start_time_source itself).
        # `start_time` is the FULL instant, not match_date -- callers pass a
        # date-only string as match_date (estimated_start_time[:10]), and writing
        # that into this column would store "2026-08-12" where every reader
        # expects an ISO datetime.
        if existing is not None and not existing.estimated_start_time and start_time:
            existing.estimated_start_time = start_time
            log.info("backfilled kickoff for %s %s vs %s: %s",
                     league, existing.home_team, existing.away_team, start_time)
        return existing

    # FIXTURE FALLBACK -- the name match above failed, which does NOT mean this
    # is a new fixture. It routinely means the other platform spells the clubs
    # differently, and creating a row here is how this app accumulated 14
    # duplicate fixtures holding 91 bets (2026-08-08): CS Marítimo vs Madeira,
    # Vitória SC vs Guimarães, and nine MLS pairs where one feed simply
    # TRUNCATES the name ("Los Angeles F", "New York RB"). Neither row could
    # settle, because refresh_soccer_results also matches ESPN by name.
    #
    # So before minting a row, ask whether this fixture already exists: same
    # league, kickoff within a day, and at least ONE side canonicalizing to the
    # same club. That is the identity test scripts/dedupe_soccer_fixtures.py
    # uses to merge them after the fact, applied here to stop making them.
    #
    # ONLY when exactly one candidate matches. Names are what is unreliable
    # here, so an ambiguous answer must create a new row rather than guess --
    # a missed join costs one duplicate, a wrong one silently merges two real
    # fixtures and corrupts both their bets.
    ours = _kickoff_day(match_date, None)
    if ours is not None:
        # SCOPED to this league. _load_upcoming_matches already restricted the
        # candidates to it, so every name on both sides of this comparison is
        # canonicalised under the same scope -- the identity test cannot be
        # skewed by giving one side a more specific alias than the other.
        #
        # This is what makes the duplicate STOP BEING CREATED rather than merely
        # being priced later: Polymarket's "FC Bayern München" now canonicalises
        # to the same key as Kalshi's "Bayern Munich", so the fixture joins.
        hk = canonical_team_key(home_team_name, league)
        ak = canonical_team_key(away_team_name, league)
        cands = []
        for row in upcoming:
            theirs = _kickoff_day(row.get("match_date"), row.get("estimated_start_time"))
            if theirs is None or abs((theirs - ours).days) > 1:
                continue
            rh = canonical_team_key(row["home_team"], league)
            ra = canonical_team_key(row["away_team"], league)
            if hk in (rh, ra) or ak in (rh, ra):
                cands.append(row)
        if len(cands) == 1:
            log.info("soccer: joined %s vs %s onto existing fixture %s (%s vs %s) by FIXTURE, "
                     "not name", home_team_name, away_team_name, cands[0]["id"],
                     cands[0]["home_team"], cands[0]["away_team"])
            return session.get(SoccerMatch, cands[0]["id"])
        if len(cands) > 1:
            log.warning("soccer: %s vs %s matches %d existing fixtures by date+side -- "
                        "creating a new row rather than guessing", home_team_name,
                        away_team_name, len(cands))

    # UTC, not local. Callers should pass a real kickoff day and this fallback
    # should be rare -- but when it fires, the stamp is compared downstream
    # against a UTC "today" (/soccer/markets drops any match_date before it), so
    # a LOCAL stamp is a day behind for several hours every evening in a western
    # timezone and marks a brand-new fixture as already played the moment it is
    # created. Observed at 20:23 local on 2026-08-08: local 08-08 vs UTC 08-09.
    resolved_date = match_date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
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


ESPN_START_SOURCE = "espn"


def update_match_estimated_start_time(match: SoccerMatch | None, estimated_start_time: str | None,
                                      source: str = "platform") -> None:
    """Keep estimated_start_time fresh from the platform while a match is
    upcoming -- but NEVER over an ESPN kickoff.

    `source` NAMES THE VENUE ("kalshi" / "polymarket"), and the reason it is not
    just "platform" any more is that the two venues put DIFFERENT THINGS in this
    field and the shared tag made that undiagnosable:

        kalshi      occurrence_datetime -- the market's occurrence point, a flat
                    3h AFTER kickoff. Corrected at the client now, in
                    kalshi_soccer_client._kickoff_from_occurrence.
        polymarket  gameStartTime -- a real kickoff, correct as supplied.

    So a blanket -3h applied HERE, in the shared writer, would have silently
    moved every Polymarket fixture three hours EARLY while fixing Kalshi. The
    correction has to live where the field's meaning is known. Tagging the venue
    is what makes a future measurement able to tell them apart at all -- the
    2026-08-14 audit below could only separate them by reading client source.

    THE BUG THIS CLOSES, user-reported 2026-08-09: two Brasileirao matches were
    recommended as 6pm fixtures while they were ~55 minutes into play. Every
    BRA1 match that day was stored +3h late, except the one whose time had come
    from ESPN:

        Vasco da Gama at Bahia      ESPN 19:00Z (live, 54')   stored 22:00:00Z
        Internacional at Palmeiras  ESPN 19:00Z (live, 57')   stored 22:00:00Z
        Mirassol at Cruzeiro        ESPN 14:00Z               stored 14:00Z

    poller_soccer ALREADY corrects kickoffs from ESPN, and it was working. This
    function then overwrote the correction with the platform's value on the very
    next poll, unconditionally -- so the fix could never survive a cycle. Two
    writers racing for one field, which is exactly the note on TennisMatch's own
    start_time_source ("Both writers running poll is what made matches
    flicker"); soccer had the same race and no guard.

    A platform's occurrence_datetime is an estimate it never revises. ESPN's is
    an observation. So ESPN wins, and this defers rather than fighting it.

    THAT DEFERRAL TREATED THE SYMPTOM, NOT THE CAUSE -- reopened 2026-08-14 by
    the same user report, on Real Sociedad B vs Castellon: board said 21:30Z,
    ESPN said 18:30Z, the match was at 46'. Note that the table above ALREADY
    names the offset ("stored +3h late"), so the size of the error was known
    here and simply never corrected; making ESPN win only hides it wherever
    ESPN also has the fixture. Every fixture ESPN does NOT cover, or whose name
    does not match (ESPN calls that club "Real Sociedad II", the market calls it
    "Real Sociedad B"), kept the raw +3h value and stayed recommended while
    being played.

    Re-measured across 2026-08-13..16, matched BY TEAM NAME against ESPN:
    espn-sourced 125/125 at +0.00h, platform-sourced 16/17 at exactly +3.00h.
    Eight fixtures were live-but-listed-as-upcoming at the moment of the report,
    two of them already full time. Fixed at the Kalshi client; this function now
    receives an already-correct kickoff."""
    if match is None or match.result_ft is not None or not estimated_start_time:
        return
    if match.start_time_source == ESPN_START_SOURCE:
        return
    match.estimated_start_time = estimated_start_time
    match.start_time_source = source


def _upsert_snapshot(session: Session, market: Market, last_price: float | None, volume: float | None,
                      yes_bid: float | None = None, yes_ask: float | None = None) -> None:
    # FLUSH ONLY WHEN THE MARKET IS NEW. This used to flush unconditionally, on
    # EVERY upsert -- and the soccer poller alone does 3,751 of them a cycle, so
    # that was 3,751 forced round trips where SQLAlchemy emits SQL for every
    # pending change instead of batching them into one commit.
    #
    # The flush exists for exactly one reason: a market added this cycle has no
    # id yet, and MarketSnapshot.market_id needs one. A market that already
    # exists in the DB already has its id populated, so flushing for it buys
    # nothing. Only a handful of markets are genuinely new on any given cycle.
    #
    # Measured 2026-08-26 via per-stage timing added to poller_soccer: the
    # soccer upsert stage was 764s for 3,751 rows.
    if market.id is None:
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
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
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
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),**quote_fields(row, row.get("last_price")))
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
    _upsert_snapshot(session, market, row.get("over_price"), row.get("volume"),**quote_fields(row, row.get("over_price")))
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


def upsert_kalshi_soccer_team_points_market(session: Session, row: dict) -> Market:
    """Season points ladder, no soccer_match_id -- same season-long shape as
    league_winner/relegation above, but it also carries `line` (the points
    threshold), since the same team appears on several rungs and the team alone
    doesn't identify the market."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="team_points", sport="soccer",
        )
        session.add(market)
    market.team = row["team"]
    market.line = row["line"]
    # group_label carries the division code, because that is what the router's
    # _futures_division looks up to decide WHICH league's season sim prices a
    # futures row. Leaving it null would strand every one of these markets
    # unpriced with no visible error.
    market.group_label = row["division"]
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


def upsert_polymarket_soccer_league_winner_row(session: Session, row: dict) -> Market:
    """Polymarket's side of the season-title market -- same shape as the Kalshi
    league_winner above (season-long, no soccer_match_id), so both venues land
    on one market_type and the router prices them identically.

    The division is carried by `group_label`, NOT by a column: `markets` has no
    `league` field, and the router resolves a futures row's rating pool through
    _MARKET_TYPE_LABEL_TO_DIVISION[(market_type, group_label)]. So the label
    written here has to be registered in that map -- see soccer_markets.py's
    POLYMARKET_LEAGUE_WINNER_LABELS. Setting a `league` attribute instead would
    have been silently dropped by SQLAlchemy and left every row unpriced.

    N1 arrives on BOTH venues now (see the client's own note on Polymarket
    finally listing Dutch football), so these rows MUST go through
    apply_duplicate_listing_cap or one title gets staked twice.
    """
    source_ticker = f"{row['condition_id']}-league_winner"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="league_winner", sport="soccer",
        )
        session.add(market)
    market.team = row["team"]
    market.group_label = row["group_label"]
    market.status = row.get("status") or "active"
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"),
                     **quote_fields(row, row.get("yes_price")))
    return market


def upsert_kalshi_mls_playoff_market(session: Session, row: dict) -> Market:
    """MLS Cup / Eastern / Western conference bracket futures. Same season-long,
    no-soccer_match_id shape as league_winner above; the only difference is that
    market_type comes from the ROW rather than being fixed, because one fetch
    covers two market types (mls_cup_winner and mls_conference_winner) and the
    conference is carried in group_label."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=row["market_type"], sport="soccer",
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
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"),**quote_fields(row, row.get("yes_price")))
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
    _upsert_snapshot(session, market, row.get("over_price"), row.get("volume"),**quote_fields(row, row.get("over_price")))
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
    _upsert_snapshot(session, market, row.get("over_price"), row.get("volume"),**quote_fields(row, row.get("over_price")))
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
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"),**quote_fields(row, row.get("yes_price")))
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
    _upsert_snapshot(session, market, row.get("yes_price"), row.get("volume"),**quote_fields(row, row.get("yes_price")))
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


# --- DOMESTIC CUPS (2026-08-08) --------------------------------------------
# Cup ties are stored with the COMPETITION as the league code, not a division,
# because a tie is not a fixture in either club's league and must never be
# mistaken for one -- the season sim builds a round-robin from a league's team
# list, and a cup tie leaking into that would invent fixtures that do not exist.
# The two clubs' actual divisions are resolved at pricing time by
# elo_service_soccer.resolve_league (which is also what stops a club being
# priced off a rating from a division it left years ago).
CUP_LEAGUE_CODES = {"coppa_italia": "COPPA_ITALIA", "dfb_pokal": "DFB_POKAL",
                    "efl_cup": "EFL_CUP",
                    "fra_super_cup": "FRA_SUPER_CUP",
                    "ger_super_cup": "GER_SUPER_CUP"}


def cup_league_code(competition: str) -> str:
    return CUP_LEAGUE_CODES.get(competition, competition.upper())


def _cup_market(session: Session, row: dict, market_type: str, soccer_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type=market_type, sport="soccer",
        )
        session.add(market)
    market.soccer_match_id = soccer_match_id
    market.status = row.get("status") or "active"
    return market


def upsert_kalshi_cup_moneyline_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """Regulation-time 3-way. Kalshi's own label says "Reg Time", so these
    settle on 90 minutes -- who progressed is the separate ADVANCE market."""
    market = _cup_market(session, row, "cup_moneyline_3way", soccer_match_id)
    market.team = row.get("team")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cup_advance_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """Who progresses, INCLUDING extra time and penalties -- a strictly
    different question from the moneyline, priced by cup_match._advance_probs."""
    market = _cup_market(session, row, "cup_advance", soccer_match_id)
    market.team = row.get("team")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cup_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """Total goals in REGULATION -- must not be priced off the extra-time grid."""
    market = _cup_market(session, row, "cup_total", soccer_match_id)
    market.line = row.get("line")
    market.side = "over"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


# --- UEFA CLUB COMPETITIONS (2026-08-08) -----------------------------------
# Same storage shape as the domestic cups: the COMPETITION is the league code,
# so a UEFA tie can never leak into a domestic league's round-robin. Each club's
# real league is resolved at pricing time and converted with the fitted strength
# offsets (models/uefa_match.py).
UEFA_LEAGUE_CODES = {"ucl": "UCL", "uel": "UEL", "uecl": "UECL", "usc": "UEFA_SUPER_CUP"}


def uefa_league_code(competition: str) -> str:
    return UEFA_LEAGUE_CODES.get(competition, competition.upper())


def upsert_kalshi_uefa_moneyline_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "uefa_moneyline_3way", soccer_match_id)
    market.team = row.get("team")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_uefa_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """Goal handicap on ONE leg of a UEFA tie. Stored exactly like the Leagues
    Cup spread -- team + line + side -- because the pricing question is
    identical once each club's own league has been resolved."""
    market = _cup_market(session, row, "uefa_spread", soccer_match_id)
    market.team = row.get("team")
    market.line = row.get("line")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_cup_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    """Domestic-cup goal handicap, regulation time."""
    market = _cup_market(session, row, "cup_spread", soccer_match_id)
    market.team = row.get("team")
    market.line = row.get("line")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_uefa_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "uefa_total", soccer_match_id)
    market.line = row.get("line")
    market.side = "over"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


# --- CONMEBOL (2026-08-18) -------------------------------------------------
# Same storage shape as UEFA: the COMPETITION is the league code, so a
# Libertadores tie can never leak into BRA1's or ARG1's round-robin. Each club's
# real league is resolved at pricing time and converted with the fitted CONMEBOL
# offsets (models/conmebol_match.py) -- a DIFFERENT offset set and a different
# baseline mu from the UEFA one.
CONMEBOL_LEAGUE_CODES = {"libertadores": "LIBERTADORES", "sudamericana": "SUDAMERICANA"}


def conmebol_league_code(competition: str) -> str:
    return CONMEBOL_LEAGUE_CODES.get(competition, competition.upper())


def upsert_kalshi_conmebol_moneyline_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "conmebol_moneyline_3way", soccer_match_id)
    market.team = row.get("team")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_conmebol_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "conmebol_total", soccer_match_id)
    market.line = row.get("line")
    market.side = "over"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_conmebol_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "conmebol_spread", soccer_match_id)
    market.team = row.get("team")
    market.line = row.get("line")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


# --- LEAGUES CUP (2026-08-08) ----------------------------------------------
# Stored under its own league code for the same reason the cups and UEFA are:
# a Leagues Cup fixture must never join an MLS or Liga MX round-robin, and the
# competition code is what keeps it out. Each club's real league is resolved at
# pricing time and bridged with the fitted MLS/Liga MX offset
# (models/leagues_cup_match.py), which is a DIFFERENT offset set and a different
# venue term from the UEFA one.
LEAGUES_CUP_LEAGUE_CODE = "LEAGUES_CUP"


# --- NATIONAL TEAMS (2026-08-09) -------------------------------------------
# Stored under the same league code the RATINGS use ("INTL"), unlike the club
# competitions above which get a per-competition code. That is deliberate: a
# national team has exactly one rating wherever it plays, so there is no
# per-competition pool to keep a fixture out of, and using the rating pool's own
# code means resolve_league lines up with pricing without a second mapping.
NATIONAL_LEAGUE_CODE = "INTL"


def upsert_kalshi_national_moneyline_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "national_moneyline_3way", soccer_match_id)
    market.team = row.get("team")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_national_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "national_total", soccer_match_id)
    market.line = row.get("line")
    market.side = "over"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_national_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "national_spread", soccer_match_id)
    market.team = row.get("team")
    market.line = row.get("line")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_national_btts_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "national_btts", soccer_match_id)
    market.side = "yes"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_leagues_cup_moneyline_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "leagues_cup_moneyline_3way", soccer_match_id)
    market.team = row.get("team")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_leagues_cup_total_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "leagues_cup_total", soccer_match_id)
    market.line = row.get("line")
    market.side = "over"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_leagues_cup_advance_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "leagues_cup_advance", soccer_match_id)
    market.team = row.get("team")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_leagues_cup_spread_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "leagues_cup_spread", soccer_match_id)
    market.team = row.get("team")
    market.line = row.get("line")
    market.side = row["side"]
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market


def upsert_kalshi_leagues_cup_btts_market(session: Session, row: dict, soccer_match_id: int | None) -> Market:
    market = _cup_market(session, row, "leagues_cup_btts", soccer_match_id)
    market.side = "yes"
    _upsert_snapshot(session, market, row.get("last_price"), row.get("volume"),
                     yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"))
    return market
