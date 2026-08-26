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
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.db.models import (
    Cs2Match, CodMatch, LolMatch, Market, MarketSnapshot, MmaFight, RaceEvent,
    SoccerMatch, TennisMatch, ValorantMatch,
)

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
# DERIVED from the Market model, not retyped. The hand-written list was missing
# `cfb_game_id` -- CFB was integrated after this check was written and nobody
# updated it -- so a CFB market linked ONLY through that column had every field
# in the list NULL and was reported as unlinked. That is how this check spent
# weeks reporting "30 CFB game markets can't be priced or settled" about 30
# markets that were correctly linked the whole time (verified: every one carried
# a real cfb_game_id).
#
# A false alarm in a health check is worse than no check -- it trains you to
# ignore it, and it hides the real ones next to it. Deriving means the next sport
# added is covered automatically. This is the same drift that already bit
# paper_logger's _ENDPOINTS and scheduler's _WARM_PATHS, both since derived too.
_LINK_SUFFIXES = ("_game_id", "_match_id", "_fight_id", "_event_id")
_LINK_FIELDS = [
    c.name for c in Market.__table__.columns
    if c.name.endswith(_LINK_SUFFIXES) and not c.name.startswith("source_")
]
_RACING = {"f1", "nascar", "irl"}
# A sport must have at least this share of its active markets carrying a real
# quote before "prices are stale" means a stalled poller (see the stale_poller
# check). Measured: cs2 100% = real stall, nascar 9% = just-not-quoted-yet.
_STALL_MIN_PRICED_FRACTION = 0.25


# STARTED-EVENT CHECK. Maps each of Market's link columns to the table holding
# that event's real start instant and its result fields.
#
# WHAT THIS CATCHES, narrowly on purpose: an event whose STORED start is
# comfortably past, that has no result recorded, and that still carries active
# markets. That combination is always wrong -- the event either resolved or a
# poller is stuck, and until one is true its last price sits frozen where it can
# be compared against a live model probability.
#
# IT DELIBERATELY DOES NOT GUESS WHETHER PLAY HAS BEGUN. Koyama vs Ichikawa
# (2026-08-24) started on the 23rd, was suspended at 5-4, and carried a
# RESUMPTION time on the 25th -- so its stored start was in the FUTURE, every
# timestamp check correctly read it as not-yet-begun, and the model priced it
# from 0-0 against a market that already knew the score. The obvious tell,
# match_date sitting days before the start, was measured before being trusted
# and REFUTED: a >=2 day gap covers 773 tennis matches, 133 of them with live
# markets, nearly all ordinary fixtures whose match_date is just the round
# listing. Building on it would hide 133 real matches to catch one -- the
# false-alarm failure the comments above already warn about. Knowing a match is
# suspended needs a feed that says so; tennis_markets.py has that guard, and its
# limit is COVERAGE (neither player appeared among its 182 started pairs), not
# logic.
#
# So this finds the PERSISTENT form -- a resumed match trips it once its
# resumption time is also safely past with no result -- instead of pretending to
# catch the live one.
_STARTED_GRACE_HOURS = 6

_EVENT_SOURCES = {
    "tennis_match_id": (TennisMatch, "estimated_start_time", ("winner_key",)),
    "soccer_match_id": (SoccerMatch, "estimated_start_time", ("result_ft",)),
    "valorant_match_id": (ValorantMatch, "estimated_start_time", ("winner",)),
    "cs2_match_id": (Cs2Match, "estimated_start_time", ("winner",)),
    "lol_match_id": (LolMatch, "estimated_start_time", ("winner",)),
    "cod_match_id": (CodMatch, "estimated_start_time", ("winner",)),
    "mma_fight_id": (MmaFight, "estimated_start_time", ("winner_id",)),
    "race_event_id": (RaceEvent, "start_time", ("result_json",)),
}

# NFL/NBA/WNBA/CFB/MLB ARE DELIBERATELY ABSENT, and the coverage warning below
# reports them every run rather than letting the gap go quiet.
#
# Those five store gameday + gametime, and gametime is LOCAL TO THE HOME TEAM's
# ballpark -- mlb_markets resolves it through TEAM_TZ and _game_kickoff_local,
# and NFL/NBA have their own separate TZ maps. Composing the two fields into a
# naive UTC instant, which is what the first version of this check did, shifts
# every game by up to 10 hours and invents "started" events that have not begun.
# Re-deriving that here would be a second, drifting copy of five timezone
# mappings; the check would be reporting its own arithmetic.
#
# So they are reported as uncovered instead. Wiring them properly means lifting
# the per-sport kickoff resolution into a shared helper both the routes and this
# check call -- worth doing, but it is a refactor, not a health check.
_GAMEDAY_SPORTS_UNCOVERED = {
    "nfl_game_id", "nba_game_id", "wnba_game_id", "cfb_game_id", "mlb_game_id",
}


def _parse_instant(value):
    """A start instant as naive UTC, or None when it cannot be trusted."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _event_start(row, spec):
    """Start instant for one event row. Every covered table stores a real ISO
    instant; see _GAMEDAY_SPORTS_UNCOVERED for the ones that do not."""
    return _parse_instant(getattr(row, spec, None))


# THE HARD EXPOSURE CAP CAN GO SILENT, and nothing noticed.
#
# exposure.remaining_for_unit_scale returns None -- meaning UNCAPPED -- when no
# snapshot has been taken. That is correct for a cold process, but it makes the
# failure mode of the refresh path INVISIBLE: if settings.get_staking_params
# ever stopped refreshing, every sizing call would quietly go uncapped and the
# board would look completely normal.
#
# THRESHOLD, reasoned rather than picked. The snapshot is refreshed by
# get_staking_params, which runs on essentially every pricing request from ~22
# routers -- so during any period the app is actually being used it is seconds
# old, not minutes. An idle app is a different matter and NOT a problem: no
# pricing is happening, so no sizing is being done uncapped. That asymmetry is
# why the two states get different severities:
#
#   MISSING  -> warning. Sizing in this process is uncapped right now.
#   STALE    -> info.    Almost always just an idle app; worth seeing, not worth
#                        alarming, and a false alarm here would train the reader
#                        to skim past the missing case sitting next to it.
_EXPOSURE_SNAPSHOT_STALE_SECONDS = 3600


def _check_exposure_snapshot(issues):
    """Assert the exposure cap is actually armed in this process."""
    try:
        from app.models import exposure
    except Exception:
        log.exception("exposure import failed")
        return
    try:
        entries, age = exposure.snapshot_status()
    except Exception:
        log.exception("exposure snapshot status failed")
        return

    if age is None or not entries:
        _issue(issues, "warning", "exposure_snapshot", None,
               "Exposure cap is NOT armed in this process -- no snapshot has been "
               "refreshed, so remaining_for_unit_scale returns None and every stake "
               "is sized UNCAPPED. Normal immediately after a restart and cleared by "
               "the first pricing request; persistent means settings.get_staking_params "
               "has stopped running.")
        return
    if age > _EXPOSURE_SNAPSHOT_STALE_SECONDS:
        _issue(issues, "info", "exposure_snapshot", None,
               f"Exposure snapshot is {age / 3600.0:.1f}h old ({entries} cap entries). "
               "Expected when the app has simply been idle -- it refreshes on any "
               "pricing request. Only meaningful if the board is being used.")


def _check_started_events(session, issues, now):
    """Active markets on events that started long ago and never resolved."""
    # DERIVED, not retyped: a sport added to Market with no entry here is
    # REPORTED rather than silently skipped. That drift made this file
    # mis-report CFB for weeks, and bit paper_logger and scheduler too.
    uncovered = [f for f in _LINK_FIELDS if f not in _EVENT_SOURCES]
    known = sorted(f for f in uncovered if f in _GAMEDAY_SPORTS_UNCOVERED)
    unexpected = sorted(f for f in uncovered if f not in _GAMEDAY_SPORTS_UNCOVERED)
    if known:
        _issue(issues, "info", "started_event_coverage", None,
               "Started-event check does not cover " + ", ".join(known)
               + " -- their kickoff is home-team-local and resolving it here would "
                 "duplicate five timezone maps. See _GAMEDAY_SPORTS_UNCOVERED.")
    if unexpected:
        _issue(issues, "warning", "started_event_coverage", None,
               "No started-event check for: " + ", ".join(unexpected)
               + " -- add them to _EVENT_SOURCES.")

    cutoff = now - datetime.timedelta(hours=_STARTED_GRACE_HOURS)
    for field in _EVENT_SOURCES:
        model, start_spec, result_attrs = _EVENT_SOURCES[field]
        col = getattr(Market, field, None)
        if col is None:
            continue
        try:
            linked = (session.query(col, func.count(Market.id))
                      .filter(Market.status == "active")
                      .filter(col.isnot(None))
                      .group_by(col).all())
        except Exception:
            log.exception("started-event query failed for %s", field)
            continue
        if not linked:
            continue
        counts = {key: n for key, n in linked}
        try:
            rows = session.query(model).filter(model.id.in_(list(counts))).all()
        except Exception:
            log.exception("started-event lookup failed for %s", field)
            continue
        stale = []
        for row in rows:
            if any(getattr(row, a, None) not in (None, "") for a in result_attrs):
                continue  # resolved -- that is settlement's problem, not this check's
            start = _event_start(row, start_spec)
            if start is None or start >= cutoff:
                continue
            stale.append((start, counts.get(row.id, 0)))
        if not stale:
            continue
        stale.sort()
        sport = (field.replace("_match_id", "").replace("_game_id", "")
                      .replace("_fight_id", "").replace("_event_id", ""))
        total = sum(n for _, n in stale)
        oldest = stale[0][0]
        age_h = (now - oldest).total_seconds() / 3600.0
        # WORDING IS DELIBERATELY FACTUAL. An earlier draft asserted these prices
        # "can be priced against a live model probability", which was checked and
        # was false for two of the three sports it fired on: 0 of 177 stale tennis
        # markets and 0 of 154 soccer ones reached their board, because those
        # routes already drop a started event. Whether a flagged row is visible
        # depends on that sport's own gate; what is certainly wrong is the event
        # having no result long after it began while its markets stay open.
        _issue(issues, "warning", "started_event", sport,
               str(len(stale)) + " " + sport + " event(s) began >"
               + str(_STARTED_GRACE_HOURS) + "h ago with no result recorded, while "
               + str(total) + " of their market(s) are still active; oldest began "
               + oldest.isoformat() + "Z (" + format(age_h, ".0f") + "h). Indicates a "
               "settlement or result-ingestion gap. Whether these also reach the board "
               "depends on that sport's own started-event gate.")


# Esports match models. Results reach these two different ways, which is why the
# check below does NOT key on `source`:
#   * cs2 / valorant -- the scraper CREATES rows (source "liquipedia" / "vlr")
#   * lol            -- gol.gg BACKFILLS onto existing market stubs, leaving
#                       source as "live", so "no row from gol.gg" is by design
_ESPORTS_MATCH_MODELS = [("cs2", "Cs2Match"), ("valorant", "ValorantMatch"),
                         ("lol", "LolMatch")]

# How far the newest match WITH A MAP SCORE may lag the newest finished match
# before the results pipeline is treated as dead rather than merely quiet.
_RESULTS_LAG_WARN_DAYS = 5
_RESULTS_LAG_ERROR_DAYS = 14


def _check_scraper_alive(session, issues):
    """Flag an esports results pipeline that has silently stopped contributing.

    THE OUTAGE THIS EXISTS FOR (2026-08-26). Liquipedia began answering the CS2
    matches page with 403 behind a Cloudflare challenge. The poller ran every 5
    minutes into that refusal for 25 DAYS and nothing noticed: fixtures kept
    appearing (they are created on demand from Kalshi/Polymarket markets, not by
    the scraper) and winners kept arriving (from platform resolution), so every
    surface looked healthy. Only the DETAIL stopped -- map scores and event names
    -- which is exactly the data nothing reads until you try to measure
    something. Scraped CS2 matches stopped at 2026-08-01 while the table ran on.

    "Rows are arriving" is therefore NOT evidence the pipeline works.

    THE SIGNAL IS MAP SCORES, NOT `source`. An earlier version of this check
    compared the newest row from a scraper SOURCE against the newest row overall,
    and false-alarmed on LoL, whose results are backfilled onto market stubs and
    never change `source` at all. Map coverage is what actually degrades in every
    case, whichever way the data arrives.
    """
    from app.db import models as M

    for sport, model_name in _ESPORTS_MATCH_MODELS:
        model = getattr(M, model_name, None)
        if model is None:
            continue
        try:
            rows = session.query(model).all()
        except Exception:
            continue
        done = [r for r in rows if getattr(r, "winner", None) is not None]
        if len(done) < 20:
            continue          # too few finished matches to say anything
        scored = [r for r in done
                  if r.maps_won_a is not None and r.maps_won_b is not None]
        pct = 100.0 * len(scored) / len(done)
        if not scored:
            _issue(issues, "error", "results_pipeline", sport,
                   f"NO finished {model_name} carries a map score ({len(done)} "
                   f"finished matches) -- series_total and series_handicap cannot "
                   f"be graded for this sport at all")
            continue
        newest_done = max((str(r.match_date or "") for r in done), default="")
        newest_scored = max((str(r.match_date or "") for r in scored), default="")
        if not newest_done or not newest_scored:
            continue
        try:
            lag = (datetime.date.fromisoformat(newest_done[:10])
                   - datetime.date.fromisoformat(newest_scored[:10])).days
        except ValueError:
            continue
        if lag >= _RESULTS_LAG_WARN_DAYS:
            _issue(issues,
                   "error" if lag >= _RESULTS_LAG_ERROR_DAYS else "warning",
                   "results_pipeline", sport,
                   f"newest {model_name} with a map score is {newest_scored} but "
                   f"finished matches run to {newest_done} -- {lag}d behind, and "
                   f"only {pct:.0f}% of finished matches are scored. Fixtures keep "
                   f"arriving as market stubs and winners keep arriving from "
                   f"platform resolution, so this does NOT surface as missing "
                   f"matches -- what stops is map scores and event names")


def _issue(issues, severity, category, sport, detail):
    issues.append({"severity": severity, "category": category, "sport": sport, "detail": detail})


@router.get("")
def health_check(session: Session = Depends(get_session)):
    """On-demand data-integrity report. Cheap enough to run live."""
    now = datetime.datetime.utcnow()
    issues: list[dict] = []

    # ONE fetch of (active markets, latest snapshot each), shared by check 1 here
    # and by every cache-aware integrity check in step 6.
    #
    # This endpoint used to take ~30s, and 20s of that was the two GROUP BY
    # aggregates check 1 originally used: each joined all 49k active markets to
    # an 18.1M-row snapshot table and scanned it. The per-market lookup below
    # walks ix_market_snapshots_market_ts instead and costs 4.3s -- which step 6
    # was already paying anyway, so check 1 is now effectively free.
    from app.models.integrity_checks import _active_with_snapshots

    integrity_cache: dict = {}
    active_markets, latest_snaps = _active_with_snapshots(session, integrity_cache)

    # Active-market counts per sport (the denominator for everything else).
    active: dict = {}
    for m in active_markets:
        active[m.sport] = active.get(m.sport, 0) + 1

    # 1) Stalled poller: freshest SNAPSHOT per sport. Must use snapshot ts, NOT
    #    markets.updated_at -- the latter only bumps when a market ROW field
    #    changes, so a perfectly healthy poller writing fresh snapshots on flat
    #    odds (common for MMA/NBA futures) looked "25h stale" and false-alarmed.
    #    The snapshot ts is the true "the poller ran and wrote a price" signal.
    #    Max over the per-market latest snapshots == max over all snapshots, so
    #    reading it off the shared cache is the same number, verified per sport
    #    against the old aggregate before the switch.
    latest: dict = {}
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
    #
    #    The counted condition tightened slightly with the cache: it now asks
    #    whether the market's LATEST snapshot carries a quote, where the old
    #    aggregate asked whether ANY snapshot ever did. That is the better
    #    question for this gate -- "is the exchange quoting this now" is exactly
    #    what separates a stall from unquoted inventory -- and both readings
    #    agree on every sport in the current data (all 49,468 active markets have
    #    a priced latest snapshot, so no row currently distinguishes them).
    priced_counts: dict = {}
    for m in active_markets:
        sn = latest_snaps.get(m.id)
        if sn is None:
            continue
        if sn.ts is not None and (m.sport not in latest or sn.ts > latest[m.sport]):
            latest[m.sport] = sn.ts
        if sn.last_price is not None or sn.yes_bid is not None:
            priced_counts[m.sport] = priced_counts.get(m.sport, 0) + 1
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
        # Chunked for the reason app/db/chunked.py documents. Summing a
        # COUNT(DISTINCT market_id) across chunks is exact here only because the
        # chunks partition racing_ids -- no market_id can be counted twice.
        from app.db.chunked import fetch_in_chunks

        priced_racing = sum(fetch_in_chunks(
            racing_ids,
            lambda chunk: [(
                session.query(func.count(func.distinct(MarketSnapshot.market_id)))
                .filter(MarketSnapshot.market_id.in_(chunk),
                        (MarketSnapshot.yes_bid.isnot(None)) | (MarketSnapshot.last_price.isnot(None)))
                .scalar() or 0
            )],
        ))
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
        # Session-aware, or this check itself becomes the bug: a sprint dated
        # correctly to its own Saturday session would read as 1 day off the
        # grand prix and warn on every sprint weekend, while a sprint wrongly
        # carrying the grand prix's date would look CLEAN.
        from app.clients.espn_racing_schedule import (
            fetch_race_dates_by_session, resolve_race_date_for_session,
        )
        from app.clients.kalshi_racing_client import is_sprint_event
        race_dates = fetch_race_dates_by_session()
        for ev in session.query(RaceEvent).all():
            real = resolve_race_date_for_session(
                ev.series, ev.name or ev.event_ticker, race_dates,
                is_sprint_event(ev.event_ticker or ""))
            # TOLERANCE IS PER SERIES, and the old flat "> 3 days" was too loose
            # to be a check at all. Every F1 sprint carried the GRAND PRIX's date
            # for weeks -- a ONE DAY error, so this never fired once. Measured
            # 2026-08-21 across all 58 upcoming race events: 32 of the 38 that
            # resolve match ESPN to the MINUTE, so a couple of hours is a real
            # signal, not noise.
            #
            # NASCAR keeps the loose bar on purpose. Kalshi files Cup, Xfinity
            # and Truck under one ticker while ESPN's feed here is CUP ONLY, so
            # a Truck race legitimately resolves to the Cup race's date and
            # would warn every week (measured: 4 events, 23.5h "off"). NASCAR
            # dates are owned by reconcile_nascar_dates_from_feed, which runs
            # after the canonical pass precisely because it is authoritative.
            tol_minutes = 3 * 24 * 60 if ev.series == "nascar" else 120
            if real and ev.start_time and abs((real - ev.start_time).total_seconds()) / 60.0 > tol_minutes:
                _issue(issues, "warning", "race_date_mismatch", ev.series,
                       f"{ev.name or ev.event_ticker}: stored {ev.start_time:%Y-%m-%d %H:%M} vs real race "
                       f"{real:%Y-%m-%d %H:%M} — CLV cutoff/display would be wrong.")
    except Exception:
        log.exception("racing date sanity check failed")

    # 6) CORRECTNESS invariants (integrity_checks.py) -- the complement to the
    #    liveness checks above. Every one of the nine data defects found on
    #    2026-08-06 would have gone unnoticed by checks 1-5, because those ask
    #    "is the plumbing running", not "is what we stored true".
    try:
        from app.models.integrity_checks import run_all as _integrity

        results = _integrity(session, cache=integrity_cache)
        for row in results.get("phantom_priced_markets", []):
            if row.get("count"):
                _issue(issues, "warning", "phantom_price", row.get("sport"),
                       f"{row['count']} active markets priced at exactly 0.500 with no book — unpriceable; _implied_prob must keep returning None for these.")
        for row in results.get("flat_ladders", []):
            if row.get("count"):
                _issue(issues, "warning", "flat_ladder", row.get("sport"),
                       f"{row['count']} totals ladders quote every rung within 10pp — a ladder is monotonic by construction, so these quotes are placeholders.")
        for row in results.get("resolved_looking_active_markets", []):
            if row.get("count"):
                # Describes the SYMPTOM, not a cause. This used to end "status
                # never reconciled", which is an assertion the check cannot
                # make: chasing 86 flagged soccer rows found Kalshi still
                # listing every one of them as genuinely active, and the real
                # problem was a stale fixture date on our side. A health check
                # that names the wrong cause sends you to the wrong file.
                _issue(issues, "warning", "stale_active_status", row.get("sport"),
                       f"{row['count']} {row.get('source')} markets still 'active' at a 0/1 price on an event whose recorded start is 6h+ ago. Either the exchange hasn't resolved them, or the stored start time is wrong — check both before assuming reconciliation is broken.")
        for row in results.get("impossible_tennis_scores", []):
            if row.get("incomplete_score"):
                _issue(issues, "info", "tennis_retirements", "tennis",
                       f"{row['incomplete_score']} of {row['resolved']} resolved matches have an unfinishable score; is_retirement caught {row['flagged_as_retirement']}, missed {row['flag_missed']} — derivative graders refuse these by score, not by the flag.")
        for row in results.get("finished_without_result", []):
            if row.get("count"):
                _issue(issues, "warning", "no_result_ingested", row.get("sport"),
                       f"{row['count']} events finished 12h+ ago with no result recorded — nothing tied to them can settle or train the model.")
        for row in results.get("resolver_dependent_teams", []):
            if row.get("count"):
                _issue(issues, "info", "resolver_dependent_teams", row.get("sport"),
                       f"{row['count']} market team names are unrated as spelled but rated once resolved (e.g. {', '.join(row.get('examples', [])[:3])}) — the resolver is load-bearing here.")
        for row in results.get("stale_bet_market_types", []):
            if row.get("count"):
                _issue(issues, "info", "stale_bet_market_type", row.get("sport"),
                       f"{row['count']} bets stored as '{row['bet_says']}' sit on markets now typed '{row['market_says']}' — the market was re-typed under them. Settlement uses the market's live type, so these grade correctly; flagged so a NEW re-typing is visible.")
        # ERROR, not warning, and the only check here that rates one. Every other
        # issue above describes data that is stale, missing or mislabelled --
        # annoying, but it does not invent a number. This one fires when a
        # mutually-exclusive leg sums past what can actually happen, which means
        # the model has manufactured probability, and the app stakes wherever
        # model > market. That is not a data-quality note; it is money.
        #
        # Measured 2026-08-11: NBA worst_record summed to 20.68 across 30 teams,
        # five of them at 1.0000 at once, four staked $2.50 each at +78 to +90pp
        # "edges". Nothing in this report would have shown it.
        # Registered explicitly, like every check here: health.py maps integrity
        # results to issues BY NAME, so a check added to run_all alone would run
        # every cycle and have its rows silently dropped.
        for row in results.get("incoherent_group_legs", []):
            _issue(issues, "error", "incoherent_sim_leg", row.get("sport"),
                   row.get("detail") or f"{row.get('sport')} {row.get('leg')} group "
                                        f"{row.get('group')} sums to {row.get('sum')}.")
        for row in results.get("incoherent_sim_legs", []):
            _issue(issues, "error", "incoherent_sim_leg", row.get("sport"),
                   row.get("detail") or f"{row.get('sport')} {row.get('leg')} sums to "
                                        f"{row.get('sum')} but only {row.get('expected')} can happen.")
    except Exception:
        log.exception("integrity checks failed")

    try:
        _check_started_events(session, issues, now)
    except Exception:
        log.exception("started-event check failed")

    try:
        _check_exposure_snapshot(issues)
        _check_scraper_alive(session, issues)
    except Exception:
        log.exception("exposure snapshot check failed")

    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order.get(i["severity"], 3))
    counts = {sev: sum(1 for i in issues if i["severity"] == sev) for sev in ("error", "warning", "info")}
    return {"checked_at": now.isoformat() + "Z", "counts": counts, "issues": issues}
