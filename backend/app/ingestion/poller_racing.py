"""Racing poller (F1 / IndyCar / NASCAR). Warms the rating service, then fetches
the live Kalshi racing markets and persists them (RaceEvent + Market rows +
price snapshots) so racing is a first-class sport in the recommendation + CLV
pipeline. Network fetch happens BEFORE the DB write lock (same anti-QueuePool-
exhaustion pattern as every other poller)."""
import logging

from app.clients.kalshi_racing_client import fetch_racing_markets
from app.clients.polymarket_racing_client import fetch_polymarket_racing, fetch_polymarket_racing_futures
from app.clients.espn_racing_schedule import fetch_race_dates, resolve_race_date
from app.db.database import SessionLocal
from app.ingestion.market_catalog_racing import upsert_race_event, upsert_racing_market
from app.ingestion.poller_lock import db_write_lock
from app.models.baseline import racing_ratings, racing_championship

log = logging.getLogger("poller_racing")


def refresh_racing_ratings():
    try:
        racing_ratings.refresh_ratings()
    except Exception:
        log.exception("racing ratings refresh failed")
    # Warm the season-championship cache off the request path (the compute does
    # ~22 sequential ESPN name fetches; TTL-guarded so it's cheap most cycles).
    # Per-series try: one series failing must not skip the rest. Shared, this
    # loop meant an F1 standings hiccup left IndyCar's cache cold, and a cold
    # cache is SILENT -- _get returns {} and every championship market for that
    # series is simply left unpriced, with nothing in the UI saying why.
    for _series in racing_championship.PRICED_SERIES:
        try:
            racing_championship.warm(_series)
        except Exception:
            log.exception("racing championship warm failed for %s", _series)


def refresh_racing_markets():
    try:
        rows = fetch_racing_markets()
    except Exception:
        log.exception("kalshi racing fetch failed -- skipping this cycle")
        rows = []
    # Polymarket carries PRICED F1/NASCAR Winner + Pole markets while Kalshi's
    # sit unpriced this far out -- the real source of racing prices/edges/CLV.
    try:
        rows = rows + fetch_polymarket_racing()
    except Exception:
        log.exception("polymarket racing fetch failed -- continuing with kalshi only")
    # Season Drivers'/Constructors' Champion futures (F1) -- priced by the
    # standings-aware championship sim, not racing_sim.
    try:
        rows = rows + fetch_polymarket_racing_futures()
    except Exception:
        log.exception("polymarket racing futures fetch failed -- continuing")
    if not rows:
        return
    # Real race dates from ESPN's season calendar (Kalshi close_time is an
    # unreliable settlement deadline) -- fetched once per cycle, before the lock.
    try:
        race_dates = fetch_race_dates()
    except Exception:
        log.exception("espn race-date fetch failed -- falling back to close_time")
        race_dates = {}
    with db_write_lock():
        session = SessionLocal()
        try:
            event_ids: dict[str, int] = {}
            for r in rows:
                et = r["event_ticker"]
                if not et:
                    continue
                if et not in event_ids:
                    real_start = resolve_race_date(r["series"], r.get("event_title") or et, race_dates)
                    event_ids[et] = upsert_race_event(session, r["series"], et, r.get("event_title"), r.get("close_time"), real_start)
                upsert_racing_market(session, r, event_ids[et])
            session.commit()
            log.info("racing poll: %d markets across %d events", len(rows), len(event_ids))
            reconcile_canonical_race_dates(session)
            # AFTER the canonical pass, because this is the authoritative source
            # and should have the last word on a NASCAR date.
            reconcile_nascar_dates_from_feed(session)
        except Exception:
            log.exception("racing market persist failed -- skipping this cycle")
        finally:
            session.close()


def reconcile_canonical_race_dates(session) -> int:
    """Give every event in a canonical race group the EARLIEST start_time in it.

    WHY THIS IS SOUND, not just a heuristic: the two sources fail in opposite
    directions. Kalshi's close_time is a settlement DEADLINE, so it can only sit
    at or after the race -- never before it -- while Polymarket's event carries
    the race date itself (its slug even spells it out:
    "nascar-xfinity-hyvee-perks-250-winner-2026-08-08"). So within a group of
    events that are the same real race, the minimum is the one that isn't a
    deadline. This module's own docstring already records the Brickyard 400
    close_time sitting a month after the race.

    THE BUG THIS FIXES, and it is not cosmetic. resolve_race_date matches ESPN's
    calendar on name tokens, but ESPN labels races by VENUE and Kalshi names
    them by SPONSOR, so every sponsor-named race falls through to close_time.
    Measured 2026-08-07: the HyVee Perks 250 was stored at 2026-08-23 for a race
    ESPN dates 2026-08-08 -- 15 days out -- while its Polymarket twin, already
    grouped with it by canonical_race_event_ids, had 2026-08-08 21:00 all along.

    That wrong date does two things. It shows the wrong day on every bet for the
    race, and -- worse -- refresh_racing_results matches a RaceEvent to an ESPN
    race BY DATE within _ESPN_DATE_SLOP_DAYS. Fifteen days out is far outside
    that window, so the race would never have settled at all and its bets would
    have hung pending forever. The Xfinity pricing shipped hours earlier would
    have produced bets nothing could ever grade.

    Runs after each poll and is idempotent: groups already agreeing are skipped.
    """
    from app.db.models import RaceEvent
    from app.models.duplicate_fixtures import canonical_race_event_ids

    try:
        canon = canonical_race_event_ids(session)
    except Exception:
        log.exception("racing dates: canonical grouping failed -- leaving dates as ingested")
        return 0
    if not canon:
        return 0

    groups: dict[int, list[int]] = {}
    for eid, gid in canon.items():
        groups.setdefault(gid, []).append(eid)

    fixed = 0
    for gid, ids in groups.items():
        if len(ids) < 2:
            continue
        evs = [e for e in (session.get(RaceEvent, i) for i in ids) if e is not None and e.start_time]
        if len(evs) < 2:
            continue
        earliest = min(e.start_time for e in evs)
        for e in evs:
            if e.start_time != earliest:
                log.info("racing dates: %s %s -> %s (canonical group %s)",
                         e.event_ticker, e.start_time, earliest, gid)
                e.start_time = earliest
                fixed += 1
    if fixed:
        session.commit()
        log.info("racing dates: corrected %d event start times from canonical groups", fixed)
    return fixed


# How far our RaceEvent date may sit from ESPN's event date and still be the
# same race. A race WEEKEND is the unit here, not a day: ESPN dates a Grand Prix
# at the START of the weekend while Kalshi's occurrence_datetime is the race
# itself, so the two legitimately differ by 2-3 days.
_ESPN_DATE_SLOP_DAYS = 4


def _match_espn_event(scoreboard: list, edate: str) -> "str | None":
    """Pick the ESPN event id for the RaceEvent's date: NEAREST within
    _ESPN_DATE_SLOP_DAYS. scoreboard = [(eid, name, date), ...].

    REAL BUG this fixes (found 2026-08-05). The window was +/-1 day and the scan
    returned the FIRST match rather than the closest. ESPN dates the 2026
    Hungarian Grand Prix 2026-07-24 (weekend start) while our RaceEvent carries
    2026-07-26 (race day) -- two days apart, so it never matched, and
    refresh_racing_results silently skipped it forever. Measured at the time:
    F1 and IndyCar had ZERO of their events carrying a result, NASCAR only 5,
    and every racing paper bet (193) sat pending with nothing to grade against
    -- which is why racing had no settled outcomes to validate anything with.
    ESPN itself was fine: fetching Hungary by id returns a full 22-car order.

    Nearest-wins also fixes a second, quieter case it inherited: NASCAR runs the
    Duels two days before the Daytona 500, so a first-match scan at +/-4 could
    grade the 500 against a Duel. Choosing the smallest delta picks the race
    whose date actually matches. Races in all three series are >= 7 days apart,
    so a 4-day window cannot reach the neighbouring round.
    """
    import datetime
    try:
        target = datetime.date.fromisoformat(edate)
    except ValueError:
        return None
    best, best_delta = _match_espn_event_with_delta(scoreboard, edate)
    return best


def _match_espn_event_with_delta(scoreboard: list, edate: str) -> "tuple[str | None, int | None]":
    """As _match_espn_event, but also returns how many days off the match was.

    The delta is what lets the NASCAR settler choose between calendars. That
    function's own note used to reason that "races in all three series are >= 7
    days apart, so a 4-day window cannot reach the neighbouring round" -- true
    while Cup was the only NASCAR pool, and false the moment Xfinity and Truck
    got their own. Cup and Xfinity race the SAME VENUE on ADJACENT DAYS (Iowa:
    Xfinity 08-08, Cup 08-09), so a +/-4 day window reaches straight across the
    series boundary and both calendars claim the race. Comparing deltas breaks
    the tie on the only evidence that distinguishes them: 0 days beats 1.
    """
    import datetime
    try:
        target = datetime.date.fromisoformat(edate)
    except ValueError:
        return None, None
    best, best_delta = None, None
    for eid, _name, d in scoreboard:
        try:
            delta = abs((datetime.date.fromisoformat(d) - target).days)
        except ValueError:
            continue
        if delta <= _ESPN_DATE_SLOP_DAYS and (best_delta is None or delta < best_delta):
            best, best_delta = eid, delta
    return best, best_delta



RACING_GRID_CACHE = "racing_grid_cache.json"


def reconcile_nascar_dates_from_feed(session) -> int:
    """Correct NASCAR start_times against NASCAR's own calendar.

    THE BUG. espn_racing_schedule.resolve_race_date accepts a match on ONE shared
    token and searches only the CUP calendar (every NASCAR RaceEvent stores
    series="nascar"). ESPN names races by VENUE, Kalshi by SPONSOR, so a lower
    series race sharing a venue with a Cup race that weekend inherits the CUP
    race's date. Measured 2026-08-14 over all 34 stored NASCAR events, 10 were
    wrong and every one was a Truck or Xfinity race wearing a Cup date:

        Truck   Black's Tire 250   stored 08-15 23:00 -> real 08-14 23:30  (x4)
        Truck   TSport 200         stored 08-08 06:00 -> real 07-25 00:00  (x3)
        Xfinity Pennzoil 250       stored 08-09 02:00 -> real 07-25 20:00  (x3)

    That is not cosmetic. The grid lookup picks the nearest ESPN event ACROSS
    calendars, so a Cup date makes the Cup race an exact-day hit and the Truck
    race a day away -- it would fetch and cache the CUP grid against a TRUCK
    event. And refresh_racing_results matches by date within a slop window, so
    TSport and Pennzoil sat 15 days outside it and could never settle.

    WHY NASCAR'S CALENDAR AND NOT ESPN'S: it names races by SPONSOR, exactly as
    the market feeds do, so the names line up directly instead of through a
    venue-token guess. Cross-series ties and moves beyond _MAX_TIEBREAK_DAYS are
    REFUSED, not guessed -- see nascar_feed.match_race.

    Verified before wiring: 31 of 34 events matched uniquely, 0 ambiguous, and
    all 7 Cup events held their correct 08-15 date rather than drifting to the
    identically-named March race at Martinsville. The 3 non-matches are two
    season-championship futures and one race whose feed name carries a sponsor
    the market title omits; all three keep their existing date.

    Idempotent -- events already agreeing are skipped.
    """
    from app.clients import nascar_feed
    from app.db.models import RaceEvent
    fixed = 0
    try:
        evs = session.query(RaceEvent).filter(
            RaceEvent.series == "nascar", RaceEvent.result_json.is_(None)).all()
    except Exception:
        log.exception("nascar dates: query failed")
        return 0
    for e in evs:
        if not e.name or not e.start_time:
            continue
        try:
            m = nascar_feed.match_race(e.name, near=e.start_time)
        except Exception:
            log.exception("nascar dates: match failed for event %s", e.id)
            continue
        if not m:
            continue
        _series_key, _rid, rname, start = m
        if start and e.start_time != start:
            log.info("nascar dates: #%s %s -> %s (%s)", e.id, e.start_time, start, rname)
            e.start_time = start
            fixed += 1
    if fixed:
        session.commit()
        log.info("nascar dates: corrected %d start times from the NASCAR calendar", fixed)
    return fixed


def _entrants_for_event(session, race_event_id) -> set[str]:
    """The driver ids this event's markets are actually about."""
    from app.db.models import Market
    from app.models.baseline import racing_ratings
    out = set()
    for (team,) in session.query(Market.team).filter(
            Market.race_event_id == race_event_id,
            Market.market_type.in_(("race_winner", "top_n"))).distinct().all():
        for series in ("nascar", "nascar_xfinity", "nascar_truck", "irl", "f1"):
            d = racing_ratings.resolve_driver_id(series, team or "")
            if d:
                out.add(d)
                break
    return out


def _grid_matches_entrants(session, race_event_id, grid: dict) -> bool:
    """Does this grid describe THIS event's field?

    THE FAILURE THIS EXISTS TO MAKE STRUCTURALLY IMPOSSIBLE. Cup, Xfinity and
    Truck run the same venue on adjacent days, and every NASCAR RaceEvent stores
    series="nascar", so a date-based lookup can hand one series' grid to another.
    The comment in the loop below already warned that a wrong-but-present grid is
    WORSE than none: the staking gate in racing_markets only asks whether a grid
    exists, so a foreign grid LIFTS the gate while strength() still sees grid=None
    for every real entrant -- the race goes back to being staked on the flat
    pre-qualifying model, silently, which is exactly what the gate is for.

    Found live 2026-08-14: the Truck race at Richmond had a cached grid whose
    driver ids matched none of its 39 entrants, and it priced flat all week.

    So this does not TRUST the series routing -- it VERIFIES the result. A grid is
    accepted only if it covers most of the field the markets are about, which no
    other series' grid can do. The threshold is 0.5 rather than something stricter
    because a legitimate grid can miss a few entrants (unrated part-timers: 36 of
    39 resolved on the Richmond race), while a foreign grid overlaps at ~0.
    """
    entrants = _entrants_for_event(session, race_event_id)
    if not entrants or not grid:
        return False
    overlap = len(entrants & set(grid)) / len(entrants)
    if overlap < 0.5:
        log.warning("racing grids: REJECTED grid for event %s -- covers %.0f%% of its "
                    "%d entrants, so it belongs to a different race/series",
                    race_event_id, overlap * 100, len(entrants))
        return False
    return True


def _nascar_feed_grid(race_event_id):
    """Starting grid from NASCAR's own feed, keyed by the app's driver ids."""
    from app.clients import nascar_feed
    from app.db.models import RaceEvent
    from app.models.baseline import racing_ratings
    try:
        session = SessionLocal()
        try:
            ev = session.get(RaceEvent, race_event_id)
            if ev is None or not ev.name:
                return None
            m = nascar_feed.match_race(ev.name)
            if not m:
                return None
            series_key, race_id, _rname, _start = m
            raw = nascar_feed.fetch_grid(series_key, race_id)
            if not raw:
                return None
            grid = {}
            for name, pos in raw.items():
                d = racing_ratings.resolve_driver_id(series_key, name)
                if d:
                    grid[d] = pos
            return grid or None
        finally:
            session.close()
    except Exception:
        log.exception("nascar feed grid failed for event %s", race_event_id)
        return None


def refresh_racing_grids():
    """Cache the starting grid for races that are SOON but not yet run, so live
    pricing can use it. The grid is set at qualifying (hours before the race) and
    is the strongest feature the racing model has -- but live pricing had no way
    to get it, so it passed grid=None forever and never sharpened after quali.

    Cached to a JSON file (same pattern as the other data/racing_*.json caches)
    rather than a new DB column, and deliberately NOT written to
    RaceEvent.result_json: settlement treats a non-null result_json as "this race
    finished", so putting a grid there would settle races that haven't run.

    Fetches only races starting within the next few days; ESPN publishes no field
    at all a week out (measured), so anything further is a wasted call."""
    import datetime
    import json
    from pathlib import Path
    from app.clients.espn_racing_results import (
        RACE_SESSION, SPRINT_SESSION, _event_ids_for_season, fetch_race_grid,
    )
    from app.clients.kalshi_racing_client import is_sprint_event
    from app.config import settings
    from app.db.models import RaceEvent
    try:
        session = SessionLocal()
        try:
            now = datetime.datetime.utcnow()
            soon = now + datetime.timedelta(days=4)
            # event_ticker rides along so a SPRINT can be told from its grand
            # prix -- same ESPN event, same weekend, different session.
            want = [
                (e.id, e.series, e.start_time.year, e.start_time.date().isoformat(), e.event_ticker)
                for e in session.query(RaceEvent).filter(RaceEvent.result_json.is_(None)).all()
                if e.start_time and now - datetime.timedelta(hours=6) <= e.start_time <= soon
            ]
        finally:
            session.close()
        if not want:
            return
        scoreboards: dict = {}
        grids: dict[str, dict] = {}
        from app.clients.espn_racing_results import NASCAR_RESULT_SERIES

        for rid, series, season, edate, ev_ticker in want:
            # SAME multi-calendar search the settler uses, and for a sharper
            # reason. Every NASCAR RaceEvent stores series="nascar", so this
            # loop used to search only the CUP calendar. For an Xfinity race
            # that does not merely fail -- it MIS-MATCHES: Cup and Xfinity run
            # the same venue on adjacent days (Iowa: Xfinity 08-08, Cup 08-09),
            # well inside the +/-4 day window, so it would fetch the CUP grid
            # and cache it against the XFINITY event.
            #
            # That is worse than finding nothing. The pre-qualifying staking
            # gate in racing_markets only asks whether a grid EXISTS for the
            # event, so a wrong-but-present grid would LIFT the gate while
            # strength() still saw grid=None for every actual entrant (the Cup
            # driver ids do not appear in an Xfinity field). The race would go
            # back to being staked on the flat pre-qualifying model -- exactly
            # what the gate exists to prevent, and silently.
            #
            # Nearest date wins across calendars; a genuine tie is skipped
            # rather than guessed.
            candidates = NASCAR_RESULT_SERIES if series == "nascar" else (series,)
            hits = []
            for cand in candidates:
                key = (cand, season)
                if key not in scoreboards:
                    scoreboards[key] = _event_ids_for_season(cand, season)
                eid, delta = _match_espn_event_with_delta(scoreboards[key], edate)
                if eid:
                    hits.append((delta, cand, eid))
            if not hits:
                continue
            hits.sort()
            if len(hits) > 1 and hits[0][0] == hits[1][0]:
                log.info("racing grids: %s and %s both match %s by %d days -- skipping event %s "
                         "rather than caching the wrong grid",
                         hits[0][1], hits[1][1], edate, hits[0][0], rid)
                continue
            _delta, cand, eid = hits[0]
            # Sprint grids come from sprint qualifying (SS), grand prix grids
            # from Saturday qualifying (Qual) -- see fetch_race_grid.
            espn_session = SPRINT_SESSION if is_sprint_event(ev_ticker) else RACE_SESSION
            g = fetch_race_grid(cand, eid, espn_session)
            if not g:
                # ESPN PUBLISHES NO FIELD FOR MANY LOWER-SERIES RACES. Measured
                # 2026-08-14 on the Truck race at Richmond, 90 minutes before the
                # green flag: scoreboard 0 competitors, core event a single
                # competition with no type, fetch_race_grid None -- while NASCAR's
                # own feed had the full 39-car qualifying result. Over the four
                # preceding Truck races ESPN missed the grid entirely on one and
                # was short by two on another.
                #
                # Trusted only because it was cross-checked, not because it is
                # official: on the three 2026 Truck races where BOTH sources
                # published a field every position agreed, 104 of 104.
                g = _nascar_feed_grid(rid)
            # VERIFIED, NOT TRUSTED. Applies to the ESPN grid as much as the
            # NASCAR one -- the wrong grid found cached on the Richmond Truck
            # race came through the ESPN path.
            if g:
                vs = SessionLocal()
                try:
                    if not _grid_matches_entrants(vs, rid, g):
                        g = None
                finally:
                    vs.close()
            if g:
                grids[str(rid)] = g
        path = Path(settings.data_dir) / RACING_GRID_CACHE
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                existing = {}
        existing.update(grids)
        path.write_text(json.dumps(existing))
        log.info("racing grids: cached %d race grids (%d events checked)", len(grids), len(want))
    except Exception:
        log.exception("racing grid refresh failed")


def refresh_racing_results():
    """Backfill final finishing order onto finished RaceEvents so race bets can
    auto-settle. ESPN fetch happens BEFORE the write lock."""
    import datetime
    import json
    from app.clients.espn_racing_results import (
        RACE_SESSION, SPRINT_SESSION, _event_ids_for_season, fetch_race_result,
    )
    from app.clients.kalshi_racing_client import is_sprint_event
    from app.db.models import RaceEvent
    try:
        session = SessionLocal()
        try:
            now = datetime.datetime.utcnow()
            # event_ticker rides along so a SPRINT can be told from its grand
            # prix -- same ESPN event, same weekend, different session.
            want = [
                (e.id, e.series, e.start_time.year, e.start_time.date().isoformat(), e.event_ticker)
                for e in session.query(RaceEvent).filter(RaceEvent.result_json.is_(None)).all()
                if e.start_time and e.start_time < now
            ]
        finally:
            session.close()
        if not want:
            return
        scoreboards: dict = {}
        results: dict = {}
        from app.clients.espn_racing_results import NASCAR_RESULT_SERIES

        for rid, series, season, edate, ev_ticker in want:
            # A NASCAR RaceEvent cannot say WHICH series it is -- Kalshi files
            # Cup, Xfinity and Truck under one ticker, so they all store
            # series="nascar". Search all three calendars and require exactly
            # ONE to have a race on this date. Cup, Xfinity and Truck run on
            # different days of the same weekend, so a unique match is the
            # normal case; an ambiguous one is left unsettled rather than
            # guessed, because settling a bet against the WRONG race's finishing
            # order is far worse than settling it late.
            candidates = NASCAR_RESULT_SERIES if series == "nascar" else (series,)
            hits = []
            for cand in candidates:
                key = (cand, season)
                if key not in scoreboards:
                    scoreboards[key] = _event_ids_for_season(cand, season)
                eid, delta = _match_espn_event_with_delta(scoreboards[key], edate)
                if eid:
                    hits.append((delta, cand, eid))
            if not hits:
                continue
            # Nearest date wins ACROSS calendars, not just within one. Cup and
            # Xfinity run the same venue on adjacent days, so at +/-4 days both
            # claim the race and a plain "is it unique?" test would reject every
            # such weekend -- which is most of them. Verified on the Iowa
            # weekend: Xfinity matches at 0 days, Cup at 1.
            hits.sort()
            if len(hits) > 1 and hits[0][0] == hits[1][0]:
                log.info("racing results: %s and %s both match %s by %d days -- leaving event %s "
                         "unsettled rather than grading against the wrong race",
                         hits[0][1], hits[1][1], edate, hits[0][0], rid)
                continue
            _delta, cand, eid = hits[0]
            # A sprint and its grand prix are the SAME ESPN event on the same
            # weekend -- only the session differs. Grading a sprint market off
            # the Sunday result (or vice versa) would be confidently wrong
            # rather than merely unsettled, so the session is chosen from the
            # Kalshi event ticker, which is what distinguishes them.
            espn_session = SPRINT_SESSION if is_sprint_event(ev_ticker) else RACE_SESSION
            r = fetch_race_result(cand, eid, espn_session)
            if r:
                results[rid] = r
        if not results:
            return
        with db_write_lock():
            session = SessionLocal()
            try:
                for rid, r in results.items():
                    ev = session.get(RaceEvent, rid)
                    if ev:
                        ev.result_json = json.dumps(r)
                session.commit()
                log.info("racing results: stored finishing order for %d races", len(results))
            finally:
                session.close()
    except Exception:
        log.exception("racing results backfill failed")


def run_full_refresh_racing():
    refresh_racing_ratings()
    refresh_racing_markets()
    refresh_racing_grids()
    refresh_racing_results()
