"""Record every priced matchup before it happens, so models can be scored
against the MARKET on outcomes.

THE PROBLEM THIS SOLVES. This app can measure whether a model became more
accurate. It cannot currently measure whether a model beats the market -- and
that is the question that decides whether anything makes money. It came up three
times in a single day (racing top_n, the MLB season sim, MMA style/defence) and
was unanswerable each time, for two structural reasons:

  * PlacedBet only holds matchups where the model ALREADY found an edge, so
    scoring on it measures the tail we chose to bet. A model looks good on the
    bets it liked almost by construction.
  * paper_logger was designed to be judged on CLV, and this app then established
    that CLV does not predict profit -- retiring the yardstick without a
    replacement.

So this logs the UNSELECTED population: every priced market, edge or no edge,
bettable or not. Boring rows where model and market agree are the most important
ones here, because they are exactly what a selected sample throws away.

HOW IT WORKS. Reads each sport's own market route -- the same function the UI
calls, so the model number logged is precisely the number the app would show,
never a reimplementation that could drift. One row per market, UPDATED while the
event is still upcoming (ratings and team news move) and frozen once it starts,
so the stored view is the last honest pre-event one.

WHAT IT DOES NOT DO. No stake, no bankroll, no bet, no effect on
recommendations. Deliberately a separate table from PlacedBet so it can never
touch the tracker's P/L or the exposure caps.

SCORING, once results accrue: compare model_prob and market_prob against the
realised outcome by log loss. Three outcomes are all useful --
  model beats market -> a real edge
  market beats model -> the market is efficient here; stop looking
  model beats a PRIOR model but not the market -> the gain is real but already
    priced, which is exactly what the MMA style block turned out to be.
Expect ~6 months before the sample means much. That wait is why this is worth
building now rather than when it is next needed.
"""
from __future__ import annotations

import datetime as dt
import sqlalchemy as sa
import logging

from app.db.database import SessionLocal
from app.db.models import ModelObservation

log = logging.getLogger("observation_logger")

# (sport key, module path, function name). Each is called in its own try/except
# so one sport's failure never costs the others their observations.
SPORT_ROUTES = [
    ("nfl", "app.api.routers.markets", "list_markets"),
    ("nba", "app.api.routers.nba_markets", "list_nba_markets"),
    ("wnba", "app.api.routers.wnba_markets", "list_wnba_markets"),
    ("mlb", "app.api.routers.mlb_markets", "list_mlb_markets"),
    ("cfb", "app.api.routers.cfb_markets", "list_cfb_markets"),
    ("soccer", "app.api.routers.soccer_markets", "list_soccer_markets"),
    ("tennis", "app.api.routers.tennis_markets", "list_tennis_markets"),
    ("mma", "app.api.routers.mma_markets", "list_mma_markets"),
    ("cs2", "app.api.routers.cs2_markets", "list_cs2_markets"),
    ("valorant", "app.api.routers.valorant_markets", "list_valorant_markets"),
    ("lol", "app.api.routers.lol_markets", "list_lol_markets"),
    ("racing", "app.api.routers.racing_markets", "list_racing_markets"),
]

# Entity id attributes to copy across when the route row exposes them. Names
# match PlacedBet/ModelObservation so bet_settlement's graders can grade an
# observation with no changes.
ENTITY_FIELDS = [
    "nfl_game_id", "nba_game_id", "wnba_game_id", "cfb_game_id", "mlb_game_id",
    "mma_fight_id", "tennis_match_id", "soccer_match_id", "valorant_match_id",
    "cs2_match_id", "lol_match_id", "race_event_id",
]

_START_FIELDS = ["estimated_start_time", "start_time", "commence_time", "match_date",
                 "event_date", "gameday", "game_date",
                 # CFB game rows call it `gametime` -- not covered by any name
                 # above, so its game markets logged event_start=NULL too.
                 # (Its ~1,380 FUTURES rows are correctly NULL: a season-long
                 # market has no single start. Same for WNBA futures. Only the
                 # game rows were a real miss.)
                 "gametime",
                 # RACING carries none of the above -- its rows expose `event`
                 # (a slug), `race_event_id`, and `close_time`, and nothing
                 # else time-shaped. So every one of its observations was
                 # written with event_start=NULL, which quietly defeats the
                 # whole point of a FORWARD observation log: with no start
                 # time there is no way to tell whether a row was captured
                 # before its race or after it, and settle() cannot tell
                 # either (it gates on `event_start <= now`).
                 #
                 # close_time is the market's own close, which for a race
                 # market is effectively the green flag -- Kalshi closes it at
                 # the start. Measured live: populated on 759 of 853 racing
                 # rows. The 94 without it are season-long futures
                 # (drivers_champion etc), which genuinely have no single
                 # start, so NULL is the correct answer there rather than a
                 # missing one.
                 #
                 # LAST in the list on purpose: this is a fallback, and any
                 # sport that exposes a real start time above still wins. It
                 # is not a claim that close == start in general.
                 "close_time"]


def _get(row, *names):
    for n in names:
        v = getattr(row, n, None)
        if v is not None:
            return v
    return None


def _parse_dt(v):
    if v is None or isinstance(v, dt.datetime):
        return v
    try:
        s = str(v).replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        return d.replace(tzinfo=None)
    except Exception:
        return None


def refresh() -> int:
    """Upsert one observation per priced market across every sport. Returns the
    number of rows written or updated. Never raises."""
    now = dt.datetime.utcnow()
    written = 0
    session = SessionLocal()
    try:
        existing = {o.market_id: o for o in session.query(ModelObservation).all()}
        for sport, module_path, fn_name in SPORT_ROUTES:
            try:
                mod = __import__(module_path, fromlist=[fn_name])
                rows = getattr(mod, fn_name)(session)
            except Exception:
                log.exception("observation logger: %s route failed, skipping", sport)
                continue

            # RACING IS THREE SPORTS AND THIS TABLE MUST SAY SO.
            #
            # SPORT_ROUTES keys are ROUTE names. For every other entry the route
            # name and the sport happen to coincide, but racing's route serves
            # f1 + irl + nascar, and PlacedBet stores those three separately.
            # Writing the route name here cost the app its entire racing
            # measurement: bet_settlement dispatches on `sport in ("f1","irl",
            # "nascar")` in FOUR places, so a row saying "racing" missed all of
            # them, fell through _get_game's final line to the NFL branch, found
            # a null nfl_game_id and returned None. All 2,798 gradeable racing
            # observations sat pending forever -- and racing was the one sport
            # carrying real money with no forward measurement at all, so a bad
            # model and a bad run were indistinguishable there.
            #
            # Fixing it at the SOURCE rather than teaching the settler a fourth
            # name also buys per-series scoring, which racing actually needs:
            # on real money nascar is -61% and irl is +66%, and a pooled
            # "racing" row cannot tell those apart.
            #
            # One batched query per refresh, not one per row.
            sport_by_market = {}
            if sport == "racing":
                from app.db.models import Market as _Market
                sport_by_market = dict(
                    session.query(_Market.id, _Market.sport)
                    .filter(_Market.sport.in_(("f1", "irl", "nascar"))).all()
                )

            n_rows = n_priced = 0
            for r in rows:
                n_rows += 1
                mid = getattr(r, "id", None)
                model_p = getattr(r, "model_prob", None)
                if mid is None or model_p is None:
                    continue  # nothing to score without a model number
                n_priced += 1

                obs = existing.get(mid)
                # Frozen once the event has started -- the stored view should be
                # the last PRE-event one, not a mid-game or post-hoc price.
                if obs is not None and obs.event_start is not None and obs.event_start <= now:
                    continue
                if obs is not None and obs.status != "pending":
                    continue

                start = _parse_dt(_get(r, *_START_FIELDS))
                implied = getattr(r, "implied_prob", None)
                if obs is None:
                    obs = ModelObservation(
                        market_id=mid,
                        sport=sport_by_market.get(mid, sport),
                        first_seen_at=now,
                    )
                    session.add(obs)
                    existing[mid] = obs
                obs.market_type = getattr(r, "market_type", None)
                obs.source = getattr(r, "source", None)
                # `driver` IS racing's `team`. Every other sport's route row calls
                # the entity `team` -- tennis and mma deliberately reuse the
                # field for a player/fighter name -- but RacingMarketOut names it
                # `driver`, so this copied None for all 3,010 racing rows.
                #
                # Every racing grader resolves its subject from bet.team
                # (_grade_racing_race_winner/top_n/pole via _race_did, and h2h via
                # split_h2h_label), so a null team meant each one returned None
                # and the row stayed pending even once the race result existed.
                # This was the SECOND of two independent faults blocking racing
                # measurement; fixing the sport key alone left 1,577 rows still
                # ungradeable.
                obs.team = getattr(r, "team", None) or getattr(r, "driver", None)
                obs.side = getattr(r, "side", None)
                obs.line = getattr(r, "line", None)
                for f in ENTITY_FIELDS:
                    v = getattr(r, f, None)
                    if v is not None:
                        setattr(obs, f, v)
                obs.model_prob = model_p
                obs.market_prob = implied
                obs.edge = getattr(r, "edge", None)
                obs.volume = getattr(r, "volume", None)
                # The decision and the quote behind it -- see ModelObservation's
                # own comment for the measurement error these exist to prevent.
                obs.would_stake_dollars = getattr(r, "suggested_stake_dollars", None)
                obs.yes_bid = getattr(r, "yes_bid", None)
                obs.yes_ask = getattr(r, "yes_ask", None)
                obs.observed_at = now
                obs.event_start = start
                if obs.status is None:
                    obs.status = "pending"
                written += 1

            # LOUD when a sport returns markets but prices NONE of them. That is
            # the cold-cache failure mode -- routes like soccer/mma/cfb/tennis
            # read model services that are empty until their poller has run, and
            # a fresh process silently yields model_prob=None for every row.
            # Measured while building this: from cold, 8 of 12 sports logged
            # nothing at all, and the only symptom was a smaller number. A
            # logger quietly covering a third of the app is worse than no logger,
            # because the gap only shows up months later as missing evidence.
            if n_rows and not n_priced:
                log.warning("observation logger: %s returned %d markets but priced NONE -- "
                            "model caches are probably cold; run this after the pollers",
                            sport, n_rows)
        session.commit()
        log.info("observation logger: %d observations written/updated", written)
    except Exception:
        log.exception("observation logger failed")
        session.rollback()
    finally:
        session.close()
    return written


def ensure_observation_for_bet(session, bet) -> bool:
    """Create an observation row for a market the hourly sweep never saw.

    THE GAP THIS CLOSES. refresh() runs hourly. Tennis and esports markets get
    listed, bet and started inside one window, so a bet can exist for a market
    with no pre-event row at all. Measured 2026-08-25: 66 of 232 settled real
    tennis moneyline bets had no observation, and those 66 returned +35.9%
    against +4.2% for the logged ones -- so the log's tennis verdict was
    measuring a subset that excluded the best bets. That is a coverage bias
    masquerading as a finding, and it is why the log could not arbitrate against
    the tracker.

    Chosen over raising the logger's cadence deliberately: this adds no
    scheduled work at all, and the app has a history of degrading under extra
    periodic load.

    The row is flagged logged_at_placement=1 -- see ModelObservation's own
    comment for the two analyses that must treat these rows differently. In
    short: exclude them from unselected-population calibration (they exist
    BECAUSE of a bet), and never read their price agreement as evidence (it is
    copied from the bet's own snapshot).

    Returns True if a row was written. Never raises: a logging failure must not
    cost the user a bet.
    """
    # READ THE ID BEFORE THE MAIN TRY, IN ITS OWN GUARD. Attribute access on a
    # detached or expired SQLAlchemy instance raises, and the handler below used
    # `getattr(bet, "market_id", None)` for its log message -- getattr only
    # swallows AttributeError, so anything else re-raised FROM THE ERROR
    # HANDLER and escaped, defeating the whole "never raises" guarantee. Caught
    # by a test that fed in an object whose every attribute raises.
    try:
        market_id = bet.market_id if bet is not None else None
    except Exception:
        log.exception("placement-time observation: bet row is unreadable")
        return False
    if market_id is None:
        return False
    try:
        existing = (session.query(ModelObservation)
                    .filter(ModelObservation.market_id == market_id).first())
        if existing is not None:
            return False
        now = dt.datetime.utcnow()
        obs = ModelObservation(
            market_id=market_id,
            sport=getattr(bet, "sport", None),
            market_type=getattr(bet, "market_type", None),
            source=getattr(bet, "source", None),
            team=getattr(bet, "team", None),
            side=getattr(bet, "side", None),
            line=getattr(bet, "line", None),
            model_prob=getattr(bet, "model_prob_at_placement", None),
            market_prob=getattr(bet, "market_prob_at_placement", None),
            edge=getattr(bet, "edge_at_placement", None),
            would_stake_dollars=getattr(bet, "stake_dollars", None),
            logged_at_placement=1,
            first_seen_at=now,
            observed_at=now,
            status="pending",
        )
        for f in ENTITY_FIELDS:
            v = getattr(bet, f, None)
            if v is not None:
                setattr(obs, f, v)
        # VOLUME AND QUOTE FROM THE MARKET'S OWN LATEST SNAPSHOT, not from the
        # bet. Without this the row carries volume=None and can never pass the
        # `volume > 0` filter that every serious analysis applies -- which would
        # make the coverage fix useless for exactly the liquid-arm questions it
        # was built to unblock.
        #
        # Read from MarketSnapshot rather than assumed: it is TRUE that the app
        # only stakes markets clearing has_real_trading, so these are liquid by
        # construction, but encoding that as an implicit "flagged rows count as
        # liquid" rule would break silently the first time the staking path
        # changed.
        try:
            from app.db.models import MarketSnapshot
            snap = (session.query(MarketSnapshot)
                    .filter(MarketSnapshot.market_id == market_id)
                    .order_by(MarketSnapshot.ts.desc()).first())
            if snap is not None:
                obs.volume = snap.volume
                obs.yes_bid = snap.yes_bid
                obs.yes_ask = snap.yes_ask
        except Exception:
            log.exception("snapshot lookup failed for market %s", market_id)
        start = _parse_dt(getattr(bet, "original_start_time", None))
        if start is not None:
            obs.event_start = start
        session.add(obs)
        return True
    except Exception:
        log.exception("placement-time observation failed for market %s", market_id)
        return False


def _settle_season_futures(session) -> int:
    """Grade season-long futures, which the loop below structurally cannot.

    That loop resolves ONE event via bet_settlement._get_game and skips anything
    it cannot find. A league title has no single event, so every futures row was
    skipped forever -- the mechanical reason most of the ungradeable forward log
    is ungradeable.

    A SEPARATE PASS, and these types are deliberately NOT added to
    AUTO_SETTLE_MARKET_TYPES. That set is shared with bet_settlement, so adding
    them would also let this grade REAL BETS, and it must not: the underlying
    bottom-N rule is ~98% right in the modern era (validated against next
    season's participant list, 134/141 all-time, 2 misses since 2005), which is
    fine for scoring a model and not fine for paying one. Real bets keep settling
    on the platform's own resolution.

    Never raises, and grades nothing it is unsure of -- an unfinished season, an
    unmapped ticker or a club absent from the table all return None.
    """
    from app.db.models import Market
    from app.models import season_futures as SF

    settled = 0
    try:
        pending = (
            session.query(ModelObservation)
            .filter(ModelObservation.status == "pending",
                    ModelObservation.market_type.in_(
                        sorted(SF.SEASON_FUTURES_MARKET_TYPES)))
            .all()
        )
        if not pending:
            return 0
        # One Market lookup per market_id, and standings computed once per
        # (division, season): load_matches parses a 122 MB cache, so doing it
        # per ROW would be thousands of passes over the same data.
        market_by_id = {}
        for mid in {o.market_id for o in pending if o.market_id is not None}:
            market_by_id[mid] = session.get(Market, mid)
        # ONE cache for the whole pass, shared across sports: soccer standings
        # parse a 122 MB file and the CFB win table is a full-season query, so
        # both must be computed once per (sport, season), not once per row.
        cache: dict = {}
        for obs in pending:
            try:
                market = market_by_id.get(obs.market_id)
                result = SF.grade(session, obs, market=market, cache=cache)
                if result not in ("won", "lost", "push"):
                    continue
                obs.status = result
                obs.settled_at = dt.datetime.utcnow()
                settled += 1
            except Exception:
                continue  # one bad row must not stop the rest
        if settled:
            log.info("season futures: %d observations settled", settled)
    except Exception:
        log.exception("season futures settlement failed")
    return settled


# How many DISTINCT tickers one pass may ask Kalshi about, in 100-ticker batched
# requests.
#
# MEASURED rather than guessed, because the first value was wrong in a way that
# mattered. 4,000 was chosen to keep a pass short, on the assumption it ran on a
# frequent poller cycle -- but `observation_settle` runs every 24 HOURS, so a
# 23,494-row backlog would have taken six days to clear.
#
# Timed against the live API: ~0.5s per 100-ticker batch, so 12,000 tickers is
# ~60s of fetch. That is affordable for a job that owns its own schedule, and it
# clears the entire settleable backlog in one pass -- only ~38% of pending
# tickers come back finalized (the rest are genuinely future events), so 23,494
# pending is really ~9,000 gradeable today.
_KALSHI_OBS_TICKER_CAP = 12000


def _settle_from_kalshi_resolution(session) -> int:
    """Grade pending observations from Kalshi's own market resolution.

    WHY THIS EXISTS. Bets and observations were being graded by DIFFERENT paths,
    and the forward log had the worse one. A PlacedBet gets
    market_resolution_settlement.settle_from_kalshi_resolution(), which grades
    every Kalshi market that finalizes with no market_type filter at all -- its
    own docstring calls it "the authoritative, 100%-coverage settlement path". A
    ModelObservation got only AUTO_SETTLE_MARKET_TYPES plus a per-sport grader,
    which needs our own results data and a type we happen to have wired.

    The gap was not subtle: cup_advance had 41 BETS settled from Kalshi and all
    70 of its OBSERVATIONS pending. Overall only 42.4% of the forward log was
    graded, with 45,551 rows stale-pending (event long finished) -- 15,653 of
    them on Kalshi markets this path can settle today.

    That matters more than any individual grader, because the forward log is the
    ONLY instrument that can measure a blocked bet. Every staking gate works by
    zeroing the stake, which stops the row becoming a paper bet (paper_logger
    gates on suggested_stake_dollars), so the bet log structurally cannot say
    whether blocking was right. Half the instrument was dark.

    Reuses the bet-side fetch and result normalisation rather than reimplementing
    them, so the scalar-means-refund handling cannot drift between the two.

    Observations are always YES-frame -- ModelObservation has no `position`
    column, so there is no NO side to invert. Never raises.
    """
    from app.db.models import Market

    settled = 0
    try:
        from app.ingestion.market_resolution_settlement import _fetch_resolutions
    except Exception:
        log.exception("kalshi resolution settler unavailable; skipping")
        return 0
    try:
        now = dt.datetime.utcnow()
        # Oldest event first: a market whose event finished long ago is the most
        # likely to have resolved, and ordering this way stops a permanently
        # unresolved ticker from crowding out newer ones every pass. event_start
        # is legitimately NULL on season-long futures, which are included -- they
        # resolve too, just later.
        pending = (
            session.query(ModelObservation)
            .join(Market, ModelObservation.market_id == Market.id)
            .filter(ModelObservation.status == "pending",
                    Market.source == "kalshi",
                    Market.source_ticker.isnot(None),
                    sa.or_(ModelObservation.event_start.is_(None),
                           ModelObservation.event_start < now))
            .order_by(ModelObservation.event_start.asc().nullslast())
            .all()
        )
        if not pending:
            return 0

        ticker_by_market = dict(
            session.query(Market.id, Market.source_ticker)
            .filter(Market.id.in_({o.market_id for o in pending})).all()
        )
        seen: list[str] = []
        for o in pending:
            tk = ticker_by_market.get(o.market_id)
            if tk and tk not in seen:
                seen.append(tk)
                if len(seen) >= _KALSHI_OBS_TICKER_CAP:
                    break

        resolution = _fetch_resolutions(seen)
        if not resolution:
            return 0
        wanted = set(seen)
        for o in pending:
            tk = ticker_by_market.get(o.market_id)
            if tk not in wanted:
                continue
            r = resolution.get(tk)
            if r is None:
                continue  # not finalized yet
            status = ("won" if r == "yes" else "lost" if r == "no"
                      else "void" if r in ("void", "") else None)
            if status is None:
                continue
            o.status = status
            o.settled_at = now
            o.settlement_note = f"auto-settled from Kalshi market resolution (result={r or 'void'})"
            settled += 1
        if settled:
            log.info("observations: %d settled from Kalshi market resolution "
                     "(%d tickers queried, %d still pending)",
                     settled, len(seen), len(pending) - settled)
    except Exception:
        log.exception("kalshi resolution settlement of observations failed")
    return settled


# Distinct Polymarket conditionIds one pass may ask Gamma about. A condition
# covers ALL outcomes of a market, so this buys far more rows than the same
# number of Kalshi tickers does -- one condition can back a dozen observations.
_POLY_OBS_CONDITION_CAP = 6000


def _settle_from_polymarket_resolution(session) -> int:
    """Grade pending observations from Polymarket's own resolution.

    The other half of the bet-vs-observation asymmetry. Closing the Kalshi side
    took forward coverage 42.0% -> 54.8%, and left 45,398 Polymarket rows pending
    with **30,660 of them on events that have already finished** -- tennis alone
    22,495. A Polymarket BET has had an authoritative path since
    ingestion/polymarket_settlement.py was built; the observation behind it got
    neither that nor the per-sport graders.

    DELEGATES THE HARD PART rather than re-deriving it. That module's `grade()`
    is pure -- stored side plus one Gamma row in, a status out -- and it encodes
    three guards found by probing 2,804 real pending bets:

      * outcome names rarely match exactly (214 of 1,500 matched Gamma's list
        verbatim), so matching is tiered and REFUSES an ambiguous hit
      * Yes/No markets carry their subject in the question, not the outcomes, so
        a market whose stored side is a team code is left pending
      * "resolved" can mean a 50/50 refund, which maps to void rather than being
        read as a win by max(prices)

    Re-implementing any of that here would be three chances to grade the wrong
    thing on 30k rows. Anything the shared grader will not commit to stays
    pending, and the reasons are counted and logged so a growing skip class is
    visible rather than silent.

    Observations have no `position` column, so there is no NO side to invert.
    Never raises.
    """
    from app.db.models import Market

    try:
        from app.ingestion.polymarket_resolution import condition_id
        from app.ingestion.polymarket_settlement import (
            fetch_closed_markets, grade, stored_side)
    except Exception:
        log.exception("polymarket resolution settler unavailable; skipping")
        return 0

    settled = 0
    try:
        now = dt.datetime.utcnow()
        pending = (
            session.query(ModelObservation.id, Market.source_ticker)
            .join(Market, ModelObservation.market_id == Market.id)
            .filter(ModelObservation.status == "pending",
                    Market.source == "polymarket",
                    Market.source_ticker.isnot(None),
                    sa.or_(ModelObservation.event_start.is_(None),
                           ModelObservation.event_start < now))
            .order_by(ModelObservation.event_start.asc().nullslast())
            .all()
        )
        if not pending:
            return 0

        cids: list[str] = []
        seen = set()
        for _oid, ticker in pending:
            c = condition_id(ticker)
            if c and c not in seen:
                seen.add(c)
                cids.append(c)
                if len(cids) >= _POLY_OBS_CONDITION_CAP:
                    break
        if not cids:
            return 0

        gamma = fetch_closed_markets(cids)
        if not gamma:
            return 0

        skipped: dict[str, int] = {}
        for oid, ticker in pending:
            c = condition_id(ticker)
            if c not in seen:
                continue
            g = gamma.get(c or "")
            if g is None:
                continue  # not resolved yet -- normal
            status, reason = grade(stored_side(ticker), g)
            if status is None:
                k = reason.split(" -- ")[0][:40]
                skipped[k] = skipped.get(k, 0) + 1
                continue
            obs = session.get(ModelObservation, oid)
            if obs is None or obs.status != "pending":
                continue
            obs.status = status
            obs.settled_at = now
            obs.settlement_note = f"auto-settled from Polymarket resolution ({reason})"
            settled += 1
        if settled:
            log.info("observations: %d settled from Polymarket resolution "
                     "(%d conditions queried)", settled, len(cids))
        if skipped:
            log.info("polymarket observation settlement left rows pending: %s", skipped)
    except Exception:
        log.exception("polymarket resolution settlement of observations failed")
    return settled


def settle_from_polymarket() -> int:
    """Scheduler entry point for the Polymarket-resolution pass. Never raises."""
    session = SessionLocal()
    try:
        n = _settle_from_polymarket_resolution(session)
        if n:
            session.commit()
        return n
    except Exception:
        log.exception("polymarket observation settlement job failed")
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        session.close()


def settle_from_kalshi() -> int:
    """Scheduler entry point for the Kalshi-resolution pass. Never raises.

    ITS OWN JOB, not a step inside settle(), for two reasons. settle() runs every
    24 hours and does the local grading, which walks every pending observation --
    running that four times as often to keep this current would multiply a cost
    this pass does not incur. And this one is the opposite shape: cheap
    (~0.5s per 100-ticker batch) but time-sensitive, since a market resolves the
    moment its event ends and every hour it stays pending is an hour the forward
    log understates coverage.

    Separating them also means a failure here cannot take the local graders down
    with it, and vice versa.
    """
    session = SessionLocal()
    try:
        n = _settle_from_kalshi_resolution(session)
        if n:
            session.commit()
        return n
    except Exception:
        log.exception("kalshi observation settlement job failed")
        try:
            session.rollback()
        except Exception:
            pass
        return 0
    finally:
        session.close()


def settle() -> int:
    """Grade pending observations whose event has finished, reusing
    bet_settlement's graders unchanged -- an observation exposes the same
    attribute names a PlacedBet does, which is the whole reason those fields
    were named to match. Returns the number settled. Never raises."""
    settled = 0
    session = SessionLocal()
    try:
        from app.db.models import Market
        from app.models import bet_settlement as BS

        pending = (
            session.query(ModelObservation)
            .join(Market, ModelObservation.market_id == Market.id)
            .filter(ModelObservation.status == "pending",
                    Market.market_type.in_(BS.AUTO_SETTLE_MARKET_TYPES))
            .all()
        )
        for obs in pending:
            try:
                game = BS._get_game(session, obs)
                if game is None or not BS._game_is_final(obs, game):
                    continue
                grader = BS._pick_grader(obs, BS.effective_market_type(session, obs))
                if grader is None:
                    continue
                result = grader(obs, game)
                if result not in ("won", "lost", "push"):
                    continue
                obs.status = result
                obs.settled_at = dt.datetime.utcnow()
                settled += 1
            except Exception:
                continue  # one bad row must not stop the rest
        settled += _settle_season_futures(session)
        session.commit()
        log.info("observation logger: %d observations settled", settled)
    except Exception:
        log.exception("observation settle failed")
        session.rollback()
    finally:
        session.close()
    return settled
