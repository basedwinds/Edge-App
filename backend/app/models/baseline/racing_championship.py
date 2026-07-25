"""Season-championship pricing service for racing futures.

Combines the three real inputs the title question needs -- live standings
(ESPN), remaining-race count (ESPN calendar), and driver strengths
(racing_ratings) -- and runs the cumulative-points Monte Carlo
(racing_championship_sim). Cached hourly: the sim is a few thousand simulated
seasons and standings only move on race weekends, so there's no need to recompute
per request.

Only F1 is priced (cumulative-points title). NASCAR's playoff-elimination format
is a different question and is deliberately left unpriced -- pricing it with a
points sim would fabricate edges, the exact failure this whole model exists to
avoid.
"""
import datetime
import logging
import threading
import time

from app.clients.espn_racing_schedule import fetch_race_dates
from app.clients.espn_racing_standings import fetch_f1_driver_standings
from app.models import racing_championship_sim as sim
from app.models.baseline import racing_ratings

log = logging.getLogger("racing_championship")

_TTL = 3600  # recompute at most hourly
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _remaining_races(series: str) -> int:
    try:
        dates = fetch_race_dates().get(series, [])
    except Exception:
        return 0
    now = datetime.datetime.utcnow()
    return sum(1 for _toks, dt in dates if dt >= now)


def _norm_constructor(name: str) -> str:
    """Fold Polymarket's constructor labels onto the ratings labels
    ('Red Bull Racing'->'Red Bull', 'Audi Revolut'->'Audi')."""
    return (name or "").lower().replace("racing", "").replace("revolut", "").strip()


def _compute_f1() -> dict:
    standings = fetch_f1_driver_standings()  # {name: points}
    remaining = _remaining_races("f1")
    st = racing_ratings._series_state("f1")
    cc = st.get("current_constructor", {})

    ids: list[str] = []
    cur: dict[str, float] = {}
    strengths: dict[str, float] = {}
    id2name: dict[str, str] = {}
    constructors: dict[str, list[str]] = {}
    for name, pts in standings.items():
        did = racing_ratings.resolve_driver_id("f1", name)
        if not did:
            continue
        s = racing_ratings.strength("f1", did, cc.get(did), None)
        if s is None:
            continue
        ids.append(did)
        cur[did] = pts
        strengths[did] = s
        id2name[did] = name
        con = cc.get(did)
        if con:
            constructors.setdefault(con, []).append(did)

    # 12k trials -> ~0.4pp sampling SE on a 70% estimate, so hourly re-warms
    # don't jitter the title prices that feed CLV. The sim is vectorised (~1s).
    drivers = sim.simulate_driver_championship(ids, cur, strengths, remaining, trials=12000) if ids else {}
    cons = sim.simulate_constructor_championship(constructors, cur, strengths, remaining, trials=12000) if constructors else {}
    return {
        "driver_probs": {id2name[d]: p for d, p in drivers.items()},  # keyed by display name
        "constructor_probs": cons,  # keyed by ratings constructor label
        "constructor_norm": {_norm_constructor(c): c for c in cons},  # lookup helper
        "remaining_races": remaining,
        "points": {id2name[d]: cur[d] for d in ids},
    }


def warm(series: str = "f1") -> None:
    """Recompute + cache the championship probabilities. Called by the racing
    poller (OFF the web-request path) because the compute does ~22 sequential
    ESPN name fetches -- doing it inside a request blocks the endpoint. Respects
    the TTL so frequent polls don't refetch standings every cycle."""
    if series != "f1":
        return
    now = time.time()
    with _lock:
        hit = _cache.get(series)
        if hit and now - hit[0] < _TTL:
            return
    try:
        data = _compute_f1()
    except Exception:
        log.exception("championship warm failed for %s", series)
        data = {}
    with _lock:
        _cache[series] = (now, data)


def _get(series: str) -> dict:
    """Read-only cache accessor for the request path -- NEVER computes inline
    (see warm). Cold cache -> {} -> markets are simply left unpriced until the
    next poll warms it."""
    if series != "f1":
        return {}
    with _lock:
        hit = _cache.get(series)
    return hit[1] if hit else {}


def driver_championship_prob(series: str, driver_name: str) -> float | None:
    """P(driver wins the drivers' title), matched by resolving both names to a
    driver id so Polymarket's label and ESPN's label agree."""
    data = _get(series)
    probs = data.get("driver_probs") or {}
    if driver_name in probs:
        return probs[driver_name]
    target = racing_ratings.resolve_driver_id(series, driver_name)
    if not target:
        return None
    for name, p in probs.items():
        if racing_ratings.resolve_driver_id(series, name) == target:
            return p
    return None


def constructor_championship_prob(series: str, constructor_name: str) -> float | None:
    """P(constructor wins the constructors' title)."""
    data = _get(series)
    probs = data.get("constructor_probs") or {}
    if constructor_name in probs:
        return probs[constructor_name]
    return probs.get((data.get("constructor_norm") or {}).get(_norm_constructor(constructor_name)))


def championship_meta(series: str) -> dict:
    """Remaining-race count + current points, for reasoning text."""
    data = _get(series)
    return {"remaining_races": data.get("remaining_races"), "points": data.get("points") or {}}
