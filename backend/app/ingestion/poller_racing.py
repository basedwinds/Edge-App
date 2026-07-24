"""Racing poller (F1 / IndyCar / NASCAR). Warms the rating service, then fetches
the live Kalshi racing markets and persists them (RaceEvent + Market rows +
price snapshots) so racing is a first-class sport in the recommendation + CLV
pipeline. Network fetch happens BEFORE the DB write lock (same anti-QueuePool-
exhaustion pattern as every other poller)."""
import logging

from app.clients.kalshi_racing_client import fetch_racing_markets
from app.clients.polymarket_racing_client import fetch_polymarket_racing
from app.clients.espn_racing_schedule import fetch_race_dates, resolve_race_date
from app.db.database import SessionLocal
from app.ingestion.market_catalog_racing import upsert_race_event, upsert_racing_market
from app.ingestion.poller_lock import db_write_lock
from app.models.baseline import racing_ratings

log = logging.getLogger("poller_racing")


def refresh_racing_ratings():
    try:
        racing_ratings.refresh_ratings()
    except Exception:
        log.exception("racing ratings refresh failed")


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


def run_full_refresh_racing():
    refresh_racing_ratings()
    refresh_racing_markets()
