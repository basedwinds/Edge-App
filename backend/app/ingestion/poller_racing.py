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
        except Exception:
            log.exception("racing market persist failed -- skipping this cycle")
        finally:
            session.close()


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
    best, best_delta = None, None
    for eid, _name, d in scoreboard:
        try:
            delta = abs((datetime.date.fromisoformat(d) - target).days)
        except ValueError:
            continue
        if delta <= _ESPN_DATE_SLOP_DAYS and (best_delta is None or delta < best_delta):
            best, best_delta = eid, delta
    return best



RACING_GRID_CACHE = "racing_grid_cache.json"


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
    from app.clients.espn_racing_results import _event_ids_for_season, fetch_race_grid
    from app.config import settings
    from app.db.models import RaceEvent
    try:
        session = SessionLocal()
        try:
            now = datetime.datetime.utcnow()
            soon = now + datetime.timedelta(days=4)
            want = [
                (e.id, e.series, e.start_time.year, e.start_time.date().isoformat())
                for e in session.query(RaceEvent).filter(RaceEvent.result_json.is_(None)).all()
                if e.start_time and now - datetime.timedelta(hours=6) <= e.start_time <= soon
            ]
        finally:
            session.close()
        if not want:
            return
        scoreboards: dict = {}
        grids: dict[str, dict] = {}
        for rid, series, season, edate in want:
            key = (series, season)
            if key not in scoreboards:
                scoreboards[key] = _event_ids_for_season(series, season)
            eid = _match_espn_event(scoreboards[key], edate)
            if not eid:
                continue
            g = fetch_race_grid(series, eid)
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
    from app.clients.espn_racing_results import _event_ids_for_season, fetch_race_result
    from app.db.models import RaceEvent
    try:
        session = SessionLocal()
        try:
            now = datetime.datetime.utcnow()
            want = [
                (e.id, e.series, e.start_time.year, e.start_time.date().isoformat())
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

        for rid, series, season, edate in want:
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
                eid = _match_espn_event(scoreboards[key], edate)
                if eid:
                    hits.append((cand, eid))
            if len(hits) != 1:
                if hits:
                    log.info("racing results: %d calendars claim a race on %s -- leaving event %s "
                             "unsettled rather than guessing (%s)", len(hits), edate, rid,
                             ", ".join(c for c, _ in hits))
                continue
            cand, eid = hits[0]
            r = fetch_race_result(cand, eid)
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
