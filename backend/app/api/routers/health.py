"""Automated data-integrity health check -- the "catch dumb bugs before they
mislead us" report. Runs a handful of cheap DB checks (plus an ESPN cross-check
for racing dates) and returns issues grouped by severity, so a glance at the
Health page answers "is anything obviously wrong right now?": stalled pollers,
markets that can't be priced, unlinked tickers (the WNBA SPN/COO class), sports
with no schedule, and race dates that disagree with the real calendar (the exact
class of bug that once said a race was weeks out when it was this weekend).
"""
import datetime
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.db.models import Market, MarketSnapshot, RaceEvent

log = logging.getLogger("health")
router = APIRouter(prefix="/health-check", tags=["health"])

# Per-game market types that MUST be tied to a game/match/fight/race -- an active
# one with no link is a data glitch (e.g. WNBA SPN/COO moneyline with no game).
_GAME_TYPES = {
    "moneyline", "spread", "total", "team_total", "f5", "rfi", "moneyline_3way",
    "game_spread", "game_total", "first_half_winner", "second_half_winner", "btts", "ftts",
    "map_winner", "series_winner", "series_total", "series_handicap",
    "set_winner", "set_total", "total_sets", "exact_score",
    "method_of_victory", "method_of_finish", "rounds", "distance", "round_of_victory",
    "race_winner", "top_n", "pole",
}
_LINK_FIELDS = [
    "nfl_game_id", "nba_game_id", "wnba_game_id", "mlb_game_id", "mma_fight_id",
    "tennis_match_id", "soccer_match_id", "valorant_match_id", "cs2_match_id",
    "lol_match_id", "race_event_id",
]
_RACING = {"f1", "nascar", "irl"}
# A sport must have at least this share of its active markets carrying a real
# quote before "prices are stale" means a stalled poller (see the stale_poller
# check). Measured: cs2 100% = real stall, nascar 9% = just-not-quoted-yet.
_STALL_MIN_PRICED_FRACTION = 0.25


def _issue(issues, severity, category, sport, detail):
    issues.append({"severity": severity, "category": category, "sport": sport, "detail": detail})


@router.get("")
def health_check(session: Session = Depends(get_session)):
    """On-demand data-integrity report. Cheap enough to run live."""
    now = datetime.datetime.utcnow()
    issues: list[dict] = []

    # Active-market counts per sport (the denominator for everything else).
    active = dict(
        session.query(Market.sport, func.count(Market.id))
        .filter(Market.status == "active")
        .group_by(Market.sport)
        .all()
    )

    # 1) Stalled poller: freshest SNAPSHOT per sport. Must use snapshot ts, NOT
    #    markets.updated_at -- the latter only bumps when a market ROW field
    #    changes, so a perfectly healthy poller writing fresh snapshots on flat
    #    odds (common for MMA/NBA futures) looked "25h stale" and false-alarmed.
    #    The snapshot ts is the true "the poller ran and wrote a price" signal.
    latest = dict(
        session.query(Market.sport, func.max(MarketSnapshot.ts))
        .join(MarketSnapshot, MarketSnapshot.market_id == Market.id)
        .filter(Market.status == "active")
        .group_by(Market.sport)
        .all()
    )
    # Distinguish a STALLED poller from inventory the exchange simply hasn't
    # started quoting. A stalled poller leaves markets that WERE being quoted and
    # stopped updating, so nearly all of them still carry a price; unquoted
    # inventory has only a sliver priced (typically leftovers from past events).
    # Measured 2026-08-02: CS2 = 1237/1237 priced (100%) and 189h stale -> a REAL
    # stall, correctly flagged and since fixed. NASCAR = 39/426 priced (9%) and
    # "190h stale" -> NOT a stall; Kalshi doesn't quote race markets until near
    # race day, and the no_market_price INFO below already says so, so the ERROR
    # was pure noise that would never clear. Hence a proportional gate, not a
    # zero-check (a zero-check would not have caught NASCAR's 9%).
    priced_counts = dict(
        session.query(Market.sport, func.count(func.distinct(Market.id)))
        .join(MarketSnapshot, MarketSnapshot.market_id == Market.id)
        .filter(Market.status == "active",
                or_(MarketSnapshot.last_price.isnot(None), MarketSnapshot.yes_bid.isnot(None)))
        .group_by(Market.sport)
        .all()
    )
    for sport, n in active.items():
        ts = latest.get(sport)
        if not ts:
            continue
        if priced_counts.get(sport, 0) < _STALL_MIN_PRICED_FRACTION * n:
            continue  # mostly unquoted inventory -> see the no_market_price INFO instead
        age_h = (now - ts).total_seconds() / 3600
        if age_h > 6:
            _issue(issues, "error" if age_h > 24 else "warning", "stale_poller", sport,
                   f"{n} active markets but the newest price snapshot is {age_h:.0f}h old — poller may be stalled.")

    # 2) Unlinked game markets: active per-game markets with no game/match link.
    #    KALSHI ONLY -- Polymarket markets are deliberately not game-linked (their
    #    matching isn't built), so flagging them is pure noise; a Kalshi game
    #    market with no link IS a real ticker→game mapping gap (the WNBA SPN/COO
    #    class). Threshold >5 so a couple of just-ended games mid-settlement don't
    #    trip it.
    unlinked_filter = [getattr(Market, f).is_(None) for f in _LINK_FIELDS]
    from sqlalchemy import and_
    unlinked = (
        session.query(Market.sport, func.count(Market.id))
        .filter(Market.status == "active", Market.source == "kalshi",
                Market.market_type.in_(_GAME_TYPES), and_(*unlinked_filter))
        .group_by(Market.sport)
        .all()
    )
    for sport, n in unlinked:
        if n > 5:
            _issue(issues, "warning", "unlinked_markets", sport,
                   f"{n} active Kalshi game market(s) with no game/match link — can't be priced or settled (ticker→game mapping gap, or stale past-game markets not yet closed).")

    # 3) No price ever, RACING ONLY (the one case we care about + cheap to check
    #    against a few hundred ids -- a full-table NOT IN over millions of
    #    snapshots was ~45s). Racing unpriced is expected (Kalshi isn't quoting
    #    it yet), surfaced as info so its absence from recommendations is
    #    explained rather than mysterious.
    racing_ids = [rid for (rid,) in session.query(Market.id)
                  .filter(Market.status == "active", Market.sport.in_(_RACING)).all()]
    if racing_ids:
        priced_racing = (
            session.query(func.count(func.distinct(MarketSnapshot.market_id)))
            .filter(MarketSnapshot.market_id.in_(racing_ids),
                    (MarketSnapshot.yes_bid.isnot(None)) | (MarketSnapshot.last_price.isnot(None)))
            .scalar() or 0
        )
        unpriced = len(racing_ids) - priced_racing
        if unpriced > 0:
            _issue(issues, "info", "no_market_price", "racing",
                   f"{unpriced} racing market(s) unpriced — Kalshi isn't quoting them yet (expected; they price near race day, so no edge/recommendation until then).")

    # 4) Sports with no active markets at all (off-season / break) → info.
    KNOWN = ["nfl", "nba", "wnba", "mlb", "mma", "tennis", "soccer", "valorant", "cs2", "lol", "f1", "nascar", "irl"]
    for sport in KNOWN:
        if active.get(sport, 0) == 0:
            _issue(issues, "info", "no_schedule", sport,
                   "No active markets — off-season, between events, or on break.")

    # 4b) Platform coverage: every sport should be checked on BOTH Kalshi AND
    #     Polymarket. Flag any sport missing a platform that DOES list it, so a
    #     whole-source gap surfaces continuously (this is how racing sat unpriced
    #     — Kalshi-only — and how CS2/LoL/WNBA are missing Polymarket right now).
    src_by_sport: dict[str, set[str]] = {}
    for sp, source in (session.query(Market.sport, Market.source)
                       .filter(Market.status == "active").distinct().all()):
        src_by_sport.setdefault(sp, set()).add(source)
    # Platforms confirmed to list each sport (probed 2026-07-24). IndyCar has no
    # Polymarket tag; every other tracked sport is on both platforms.
    POLYMARKET_SPORTS = {"nfl", "nba", "wnba", "mlb", "mma", "tennis", "soccer", "cs2", "lol", "valorant", "f1", "nascar"}
    for sport in KNOWN:
        if active.get(sport, 0) == 0:
            continue  # empty sports already flagged by no_schedule above
        srcs = src_by_sport.get(sport, set())
        if sport in POLYMARKET_SPORTS and "polymarket" not in srcs:
            _issue(issues, "warning", "missing_platform", sport,
                   "Polymarket lists this sport but we ingest none of it — a whole platform's prices/edges/CLV are missing (add a Polymarket client, like racing got).")
        if "kalshi" not in srcs:
            _issue(issues, "warning", "missing_platform", sport,
                   "No Kalshi markets ingested — Kalshi may list this sport; check the feed.")

    # 5) Racing date sanity: RaceEvent.start_time vs ESPN's real calendar date.
    #    This is the check that guards the exact bug where a race showed weeks off.
    try:
        from app.clients.espn_racing_schedule import fetch_race_dates, resolve_race_date
        race_dates = fetch_race_dates()
        for ev in session.query(RaceEvent).all():
            real = resolve_race_date(ev.series, ev.name or ev.event_ticker, race_dates)
            if real and ev.start_time and abs((real - ev.start_time).days) > 3:
                _issue(issues, "warning", "race_date_mismatch", ev.series,
                       f"{ev.name or ev.event_ticker}: stored date {ev.start_time:%Y-%m-%d} vs real race {real:%Y-%m-%d} — CLV cutoff/display would be wrong.")
    except Exception:
        log.exception("racing date sanity check failed")

    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order.get(i["severity"], 3))
    counts = {sev: sum(1 for i in issues if i["severity"] == sev) for sev in ("error", "warning", "info")}
    return {"checked_at": now.isoformat() + "Z", "counts": counts, "issues": issues}
