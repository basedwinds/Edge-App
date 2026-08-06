"""CORRECTNESS invariants over the stored data -- the complement to
health.py's LIVENESS checks.

WHY THIS EXISTS. health.py already answers "is the plumbing running" (stale
poller, unlinked markets, missing platform, no price, race dates). It answers
nothing about whether what we stored is TRUE. On 2026-08-06 a single session
found nine distinct data-integrity defects, and not one would have tripped any
existing check:

  * a market priced at exactly 0.500 with no book (6,077 of them)
  * a totals ladder quoting every rung the same (120 bets graded off it)
  * a tennis match with a winner and an impossible score, graded anyway (52)
  * markets frozen at "active" that the exchange had already finalized (2,172)
  * finished esports matches with no winner ever written (432)
  * a rated team read as an unrated 1500 because its history sits under
    another spelling

Every one surfaced because a human noticed something odd -- a 100% market still
showing live, a bet not grading, an Elo of exactly 1500 -- and it was chased by
hand. Each is also expressible as a cheap invariant, which is what this module
does. The point is to make the NEXT instance of these classes announce itself.

DESIGN. Every check is a pure DB read, no network and no per-sport scraper, so
this stays cheap enough to run beside the existing health check. Each returns
plain dicts; health.py decides severity and presentation.

These REPORT ONLY. Nothing here mutates a bet or a market -- a false positive
should cost a line in a report, never a wrongly-settled bet.
"""
import datetime
import logging
from collections import defaultdict

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot, TennisMatch

log = logging.getLogger("integrity_checks")

# A ladder quoting every rung within this band is not a tight market, it is a
# market with no opinion -- see the flat-ladder cleanup for the measured case.
_FLAT_LADDER_SPAN = 0.10
_MIN_LADDER_RUNGS = 3
# Prices this close to 0 or 1 on a market we still call active are the shape a
# RESOLVED market leaves behind.
_RESOLVED_EDGE = 0.02


def _latest_snapshots(session: Session, market_ids: list[int]) -> dict:
    """Latest snapshot per market. Imported lazily from the markets router so
    there is exactly one implementation of this query in the app."""
    from app.api.routers.markets import _batch_latest_snapshots

    return _batch_latest_snapshots(session, market_ids)


def _active_with_snapshots(session: Session, cache: dict | None = None):
    """(active markets, {market_id: latest snapshot}) -- fetched ONCE per run.

    Three of the checks below need exactly this, and the first version had each
    of them query it independently: three full passes over ~24k markets and
    their snapshots, which took the /health-check endpoint to 55s. `cache` is
    threaded through run_all so a single run pays for it once.
    """
    if cache is not None and "markets" in cache:
        return cache["markets"], cache["snaps"]
    markets = session.query(Market).filter(Market.status == "active").all()
    snaps = _latest_snapshots(session, [m.id for m in markets])
    if cache is not None:
        cache["markets"], cache["snaps"] = markets, snaps
    return markets, snaps


def phantom_priced_markets(session: Session, cache: dict | None = None) -> list[dict]:
    """Active markets whose only 'price' is a seeded 0.500 with no book.

    _implied_prob now returns None for these, so they cannot be bet -- this
    check exists to notice if that ever regresses, or if a NEW seeded value
    starts appearing (the guard is deliberately narrow: exactly 0.500, no bid,
    no ask, no volume).
    """
    markets, snaps = _active_with_snapshots(session, cache)
    by_sport: dict = defaultdict(int)
    for m in markets:
        sn = snaps.get(m.id)
        if sn is None or sn.yes_bid is not None or sn.yes_ask is not None:
            continue
        if sn.last_price is not None and abs(sn.last_price - 0.5) < 1e-9 and not sn.volume:
            by_sport[m.sport] += 1
    return [{"sport": s, "count": n} for s, n in sorted(by_sport.items(), key=lambda x: -x[1])]


def flat_ladders(session: Session, cache: dict | None = None) -> list[dict]:
    """Totals ladders where every rung carries the same price.

    A totals ladder is monotonic by construction -- P(over 0.5) >= P(over 1.5)
    >= P(over 2.5). Flat means the quotes are placeholders, and anything graded
    or priced off them is fiction.
    """
    all_markets, snaps = _active_with_snapshots(session, cache)
    markets = [
        m for m in all_markets
        if m.line is not None
        and m.market_type in ("game_total", "total", "team_total", "series_total")
    ]
    ladders: dict = defaultdict(dict)
    for m in markets:
        sn = snaps.get(m.id)
        if sn is None or sn.last_price is None:
            continue
        key = (m.sport, m.market_type, m.source,
               m.soccer_match_id or m.mlb_game_id or m.tennis_match_id
               or m.nfl_game_id or m.cs2_match_id or m.lol_match_id, m.team)
        ladders[key][m.line] = sn.last_price
    out: dict = defaultdict(int)
    for (sport, *_rest), rungs in ladders.items():
        if len(rungs) < _MIN_LADDER_RUNGS:
            continue
        if max(rungs.values()) - min(rungs.values()) < _FLAT_LADDER_SPAN:
            out[sport] += 1
    return [{"sport": s, "count": n} for s, n in sorted(out.items(), key=lambda x: -x[1])]


def resolved_looking_active_markets(session: Session, hours: int = 6, cache: dict | None = None) -> list[dict]:
    """Markets we still call active, priced at an extreme, on an event that
    already STARTED over `hours` ago.

    A local proxy for "the exchange resolved this and we never noticed" -- no
    network call, so it can run every cycle. The authoritative fix is
    reconcile_kalshi_market_status; this is the alarm that says it has not run,
    or does not cover a source (Polymarket has no equivalent).

    BOTH CONDITIONS ARE REQUIRED, and the first draft of this check got that
    wrong: price alone flagged 11,901 tennis rows, because a pre-game heavy
    favourite legitimately trades at 0.98. An extreme price is only suspicious
    once the event is well underway, so the start-time gate is what turns this
    from noise into a signal. The band is also tightened to 1c.
    """
    from app.db.models import Cs2Match, LolMatch, SoccerMatch, ValorantMatch

    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).date().isoformat()
    started: set = set()
    for model, fk in ((Cs2Match, "cs2_match_id"), (ValorantMatch, "valorant_match_id"),
                      (LolMatch, "lol_match_id"), (SoccerMatch, "soccer_match_id"),
                      (TennisMatch, "tennis_match_id")):
        for r in session.query(model).all():
            if (getattr(r, "match_date", None) or "") and str(r.match_date)[:10] < cutoff:
                started.add((fk, r.id))

    markets, snaps = _active_with_snapshots(session, cache)
    by: dict = defaultdict(int)
    for m in markets:
        sn = snaps.get(m.id)
        if sn is None or sn.last_price is None:
            continue
        if not (sn.last_price <= 0.01 or sn.last_price >= 0.99):
            continue
        if not any((fk, getattr(m, fk, None)) in started
                   for fk in ("cs2_match_id", "valorant_match_id", "lol_match_id",
                              "soccer_match_id", "tennis_match_id")):
            continue
        by[(m.sport, m.source)] += 1
    return [{"sport": s, "source": src, "count": n}
            for (s, src), n in sorted(by.items(), key=lambda x: -x[1])]


def impossible_tennis_scores(session: Session) -> list[dict]:
    """Tennis matches carrying a winner AND a score that cannot be a finished
    match -- i.e. a retirement the is_retirement flag failed to record.

    That flag is measured to never fire (0 of 95 real cases), so the score is
    the only honest signal. Derivative graders already refuse these; this counts
    them so a change in the rate is visible.
    """
    from app.models.bet_settlement import _tennis_match_incomplete

    rows = session.query(TennisMatch).filter(TennisMatch.winner_key.isnot(None)).all()
    bad = [m for m in rows if _tennis_match_incomplete(m)]
    flagged = sum(1 for m in bad if m.is_retirement)
    return [{
        "resolved": len(rows),
        "incomplete_score": len(bad),
        "flagged_as_retirement": flagged,
        "flag_missed": len(bad) - flagged,
    }]


def finished_without_result(session: Session, hours: int = 12) -> list[dict]:
    """Events that started well over `hours` ago and still have no result.

    Sport-agnostic on purpose: it is the same symptom whatever the cause
    (blocked scraper, name-join failure, a source that never covered the tier),
    which is what makes it keep working when a NEW cause appears.
    """
    from app.db.models import Cs2Match, LolMatch, SoccerMatch, ValorantMatch

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    cutoff_date = cutoff.date().isoformat()
    out = []
    for label, model, date_attr, result_attr in (
        ("cs2", Cs2Match, "match_date", "winner"),
        ("valorant", ValorantMatch, "match_date", "winner"),
        ("lol", LolMatch, "match_date", "winner"),
        ("soccer", SoccerMatch, "match_date", "result_ft"),
        ("tennis", TennisMatch, "match_date", "winner_key"),
    ):
        try:
            rows = session.query(model).filter(getattr(model, result_attr).is_(None)).all()
        except Exception:
            log.exception("finished_without_result failed for %s", label)
            continue
        n = sum(
            1 for r in rows
            if (getattr(r, date_attr) or "") and str(getattr(r, date_attr))[:10] < cutoff_date
        )
        if n:
            out.append({"sport": label, "count": n})
    return sorted(out, key=lambda x: -x["count"])


def resolver_dependent_teams(session: Session) -> list[dict]:
    """Esports market names that are unrated as spelled but rated once resolved.

    NOT a defect list -- these are cases where name resolution is doing its job
    ("G2" -> "G2 Esports", "Kiwoom DRX" -> its real history). The first draft of
    this check called them errors, which was backwards.

    It is reported because the count is a HEALTH SIGNAL for the resolver: this
    is the exact machinery whose absence once displayed a rated team as an
    unrated 1500 and mis-seeded futures. A sudden jump means new spellings are
    arriving; a drop to zero when markets exist means the resolver stopped
    working. Both are worth seeing.
    """
    out = []
    for sport, service, fk in (
        ("cs2", "elo_service_cs2", "cs2_match_id"),
        ("valorant", "elo_service_valorant", "valorant_match_id"),
        ("lol", "elo_service_lol", "lol_match_id"),
    ):
        try:
            mod = __import__(f"app.models.baseline.{service}", fromlist=["x"])
            state = mod._cache.get("state")
            if state is None:
                continue
            names = {
                m.team for m in session.query(Market)
                .filter(Market.sport == sport, Market.status == "active", Market.team.isnot(None))
                .all() if m.team
            }
            bad = [
                n for n in names
                if state.games_played(n) == 0 and state.games_played(mod.resolve_team_name(n)) > 0
            ]
            if bad:
                out.append({"sport": sport, "count": len(bad), "examples": sorted(bad)[:5]})
        except Exception:
            log.exception("unrated_but_known_teams failed for %s", sport)
    return out


_CACHE_AWARE = {"phantom_priced_markets", "flat_ladders", "resolved_looking_active_markets"}


def run_all(session: Session) -> dict:
    """Every invariant, as {check_name: rows}. Never raises -- a check that
    fails is reported as an error string rather than taking the whole report
    down with it, since this runs beside a user-facing health endpoint."""
    checks = {
        "phantom_priced_markets": phantom_priced_markets,
        "flat_ladders": flat_ladders,
        "resolved_looking_active_markets": resolved_looking_active_markets,
        "impossible_tennis_scores": impossible_tennis_scores,
        "finished_without_result": finished_without_result,
        "resolver_dependent_teams": resolver_dependent_teams,
    }
    cache: dict = {}
    out: dict = {}
    for name, fn in checks.items():
        try:
            out[name] = fn(session, cache=cache) if name in _CACHE_AWARE else fn(session)
        except Exception as exc:
            log.exception("integrity check %s failed", name)
            out[name] = [{"error": str(exc)}]
    return out
