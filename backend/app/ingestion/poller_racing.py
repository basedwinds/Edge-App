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
    try:
        racing_championship.warm("f1")
    except Exception:
        log.exception("racing championship warm failed")


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


def _match_espn_event(scoreboard: list, edate: str) -> "str | None":
    """Pick the ESPN event id whose date matches the RaceEvent's date (exact,
    then +/-1 day for TZ slop). scoreboard = [(eid, name, date), ...]."""
    import datetime
    for eid, _name, d in scoreboard:
        if d == edate:
            return eid
    try:
        target = datetime.date.fromisoformat(edate)
    except ValueError:
        return None
    for eid, _name, d in scoreboard:
        try:
            if abs((datetime.date.fromisoformat(d) - target).days) <= 1:
                return eid
        except ValueError:
            continue
    return None


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
        for rid, series, season, edate in want:
            key = (series, season)
            if key not in scoreboards:
                scoreboards[key] = _event_ids_for_season(series, season)
            eid = _match_espn_event(scoreboards[key], edate)
            if not eid:
                continue
            r = fetch_race_result(series, eid)
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
    refresh_racing_results()
