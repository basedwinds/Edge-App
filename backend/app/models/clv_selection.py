"""CLV-driven bet selection: let the FORWARD closing-line-value data decide
which buckets of bet to keep staking, instead of trusting the model's edge.

The model doesn't get smarter here -- the SYSTEM stops betting the categories
that don't work. Every bet is bucketed by (sport, market_type); once a bucket
has enough settled bets with real CLV, buckets whose average CLV is negative
(we consistently got a WORSE price than the close -- the opposite of edge) get
gated out of the recommended list, while positive-CLV buckets keep flowing.

Deliberately INERT until data accrues: a bucket with fewer than `min_sample`
closed bets is ALWAYS enabled (we don't yet know if it works, so we don't
suppress it). With ~zero CLV history today this gates nothing -- it only starts
pruning once weeks of real closing prices have accumulated (the capture is now
running for every integrated sport, WNBA included). This is the honest,
data-driven successor to "bet everything the model flags," and the only
mechanism in this app designed to turn "no average edge" into "edge in the
buckets that survive."
"""
import time
from collections import defaultdict

from sqlalchemy.orm import Session

from app.db.models import PlacedBet
from app.models.clv import compute_bet_clv

DEFAULT_MIN_SAMPLE = 20   # closed bets before a bucket's CLV is trusted
DEFAULT_MIN_CLV_PP = 0.0  # require non-negative average CLV to stay enabled

# bucket_clv_stats recomputes CLV for EVERY placed bet, and is called on every
# sport list request (for gate_kelly). With paper-logging (paper_logger.py) the
# PlacedBet table grows into the thousands, so recomputing per request would add
# real latency. CLV changes only as games close (~minutes), so a short in-process
# TTL cache is safe and keeps the gate cheap. Invalidate implicitly by TTL.
_STATS_TTL_SECONDS = 300
_stats_cache: dict = {"at": 0.0, "value": None}


def bucket_clv_stats(session: Session) -> dict[tuple[str, str], dict]:
    """{(sport, market_type): {n, avg_clv_pp}} over bets with a real closing
    line (compute_bet_clv status == "closed"). TTL-cached (see _STATS_TTL)."""
    now = time.time()
    if _stats_cache["value"] is not None and now - _stats_cache["at"] < _STATS_TTL_SECONDS:
        return _stats_cache["value"]
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for bet in session.query(PlacedBet).all():
        clv = compute_bet_clv(session, bet)
        if clv["status"] == "closed" and clv["clv_pp"] is not None:
            buckets[(bet.sport, bet.market_type)].append(clv["clv_pp"])
    value = {
        key: {"n": len(vals), "avg_clv_pp": round(sum(vals) / len(vals), 4)}
        for key, vals in buckets.items()
    }
    _stats_cache.update(at=now, value=value)
    return value


def is_bucket_enabled(
    stats: dict[tuple[str, str], dict],
    sport: str,
    market_type: str,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    min_clv_pp: float = DEFAULT_MIN_CLV_PP,
) -> bool:
    """Whether to keep surfacing bets in (sport, market_type). Enabled by
    default until a bucket has `min_sample` closed bets -- only a
    well-sampled, negative-average-CLV bucket is suppressed."""
    b = stats.get((sport, market_type))
    if b is None or b["n"] < min_sample:
        return True  # not enough data to judge -> don't suppress
    return b["avg_clv_pp"] >= min_clv_pp


def gate_kelly(kelly, clv_stats: dict, sport: str, market_type: str):
    """Zero out a computed kelly fraction if its (sport, market_type) bucket is
    CLV-suppressed. One-liner used at each router's kelly call site so the gate
    rolls out uniformly. No-op (returns kelly unchanged) until the bucket is
    well-sampled, so it changes nothing today."""
    if kelly is not None and not is_bucket_enabled(clv_stats, sport, market_type):
        return None
    return kelly


def bucket_report(session: Session, min_sample: int = DEFAULT_MIN_SAMPLE, min_clv_pp: float = DEFAULT_MIN_CLV_PP) -> list[dict]:
    """Human-readable per-bucket status, most-sampled first -- so you can watch
    which buckets are earning their place as CLV accrues."""
    stats = bucket_clv_stats(session)
    rows = [
        {
            "sport": sport, "market_type": mt, "n": b["n"], "avg_clv_pp": b["avg_clv_pp"],
            "enabled": is_bucket_enabled(stats, sport, mt, min_sample, min_clv_pp),
            "status": ("suppressed (negative CLV)" if not is_bucket_enabled(stats, sport, mt, min_sample, min_clv_pp)
                       else "enabled (proven)" if b["n"] >= min_sample else "enabled (gathering data)"),
        }
        for (sport, mt), b in stats.items()
    ]
    rows.sort(key=lambda r: r["n"], reverse=True)
    return rows
