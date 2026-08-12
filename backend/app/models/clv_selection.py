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
import statistics
import threading
import time
from collections import defaultdict

from sqlalchemy.orm import Session

from app.db.models import PlacedBet, TennisMatch
from app.models.clv import compute_bet_clv

DEFAULT_MIN_SAMPLE = 20   # closed bets before a bucket's CLV is trusted
DEFAULT_MIN_CLV_PP = 0.0  # require non-negative median CLV to stay enabled

# |CLV| above this is almost never a real closing-line move -- it's an in-play
# price leak (a "closing" snapshot captured AFTER the match started, when the
# winning side already trades near 100%). Real CLV is single-digit pp. These
# are excluded from the aggregate (but counted) so a handful of contaminated
# bets can't fabricate a +22pp bucket average. The median is also reported as
# the primary robust stat -- see bucket_clv_stats.
_CONTAMINATION_PP = 0.20

# bucket_clv_stats recomputes CLV for EVERY placed bet, and is called on every
# sport list request (for gate_kelly). With paper-logging (paper_logger.py) the
# PlacedBet table grows into the thousands, so recomputing per request would add
# real latency. CLV changes only as games close (~minutes), so a short in-process
# TTL cache is safe and keeps the gate cheap. Invalidate implicitly by TTL.
_STATS_TTL_SECONDS = 300
_stats_cache: dict = {"at": 0.0, "value": None}

# SINGLE-FLIGHT + SERVE-STALE. Measured 2026-08-11: one cold call costs 21
# SECONDS -- it recomputes CLV for all 26,905 placed bets, and compute_bet_clv
# does a per-bet game lookup against a 34.5M-row MarketSnapshot table.
#
# The TTL alone was not enough, and py-spy showed why: with no lock, EVERY
# request that arrives after the TTL expires starts its own recompute. Two
# request threads were caught in the identical
# _get_game <- compute_bet_clv <- bucket_clv_stats stack at the same moment,
# and /mlb/markets, /wnba/markets, /soccer/markets and /nba/futures were all
# timing out at 120s while /settings answered in 1.9s. That is a thundering
# herd, not slow code.
#
# So: at most ONE thread ever recomputes. Everyone else immediately gets the
# previous value, even if slightly stale -- CLV moves as games close (minutes to
# hours), so a few seconds of staleness cannot change a gating decision.
_refresh_lock = threading.Lock()


def bucket_clv_stats(session: Session) -> dict[tuple[str, str], dict]:
    """{(sport, market_type): {n, avg_clv_pp}} over bets with a real closing
    line (compute_bet_clv status == "closed").

    Never blocks on a refresh that another thread is already doing, and never
    blocks at all on a cold cache -- see _refresh_lock.
    """
    now = time.time()
    if _stats_cache["value"] is not None and now - _stats_cache["at"] < _STATS_TTL_SECONDS:
        return _stats_cache["value"]

    if not _refresh_lock.acquire(blocking=False):
        # Someone else is already paying the 21s. Serve what we have rather than
        # queue up behind them.
        #
        # An EMPTY dict on a cold cache is the correct fallback, not a stall:
        # is_bucket_enabled treats an unknown bucket as ENABLED (see its
        # docstring -- "not enough data to judge -> don't suppress"), so a
        # missing stats dict makes the gate permissive, which is exactly its
        # documented default posture. It can never silently SUPPRESS a bucket.
        return _stats_cache["value"] or {}
    try:
        # Re-check under the lock: another thread may have finished while this
        # one was acquiring.
        now = time.time()
        if _stats_cache["value"] is not None and now - _stats_cache["at"] < _STATS_TTL_SECONDS:
            return _stats_cache["value"]
        return _compute_bucket_clv_stats(session, now)
    finally:
        _refresh_lock.release()


def _compute_bucket_clv_stats(session: Session, now: float) -> dict[tuple[str, str], dict]:
    """The real work. Only ever called with _refresh_lock held."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    rec_buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for bet in session.query(PlacedBet).all():
        clv = compute_bet_clv(session, bet)
        if clv["status"] != "closed" or clv["clv_pp"] is None:
            continue
        # An entry price off an unquoted market isn't a price, so nothing
        # measured against it belongs in a bucket -- see _entry_was_unquoted.
        # This path feeds both /clv-buckets and gate_kelly, so the exclusion
        # has to happen HERE, not only in the conditional slices.
        # Checked AFTER the CLV test on purpose: it costs a snapshot lookup,
        # and most rows are already dropped for having no closing line.
        if _entry_was_unquoted(session, bet):
            continue
        buckets[(bet.sport, bet.market_type)].append(clv["clv_pp"])
        # Track how much of each bucket the app would ACTUALLY have bet.
        #
        # This population is 81% paper rows that were never on the recommended
        # tab (measured 2026-08-10: 8 of 43 staked paper bets that day), because
        # paper deliberately logs below the bet gate to gather coverage. So the
        # gate that retires market types is learning largely from bets this app
        # would not have placed.
        #
        # DELIBERATELY NOT FILTERED YET. was_recommended only started being
        # recorded on 2026-08-10, so every historical row is NULL -- switching
        # the gate to recommended-only today would empty nearly every bucket and
        # change staking behaviour overnight on no evidence. Counting it first
        # makes the switchover a measured decision instead of a guess: when
        # n_recommended reaches a usable size, compare the two populations'
        # CLV before changing what gate_kelly consumes.
        if bet.was_recommended:
            rec_buckets[(bet.sport, bet.market_type)].append(clv["clv_pp"])
    value = {}
    for key, vals in buckets.items():
        clean = [v for v in vals if abs(v) <= _CONTAMINATION_PP]
        used = clean or vals  # if EVERY bet is extreme, fall back so n>0 stays honest
        value[key] = {
            "n": len(clean),                       # trustworthy sample size (contaminated excluded)
            "n_raw": len(vals),
            "n_contaminated": len(vals) - len(clean),
            "avg_clv_pp": round(sum(used) / len(used), 4),      # clean mean
            "median_clv_pp": round(statistics.median(used), 4),  # robust primary stat
            # Subset that was on the recommended tab when logged. NOT used by
            # gate_kelly -- see the note above. Reported so the switchover can
            # be made on evidence.
            "n_recommended": len(rec_buckets.get(key, [])),
            "avg_clv_pp_recommended": (
                round(sum(rec_buckets[key]) / len(rec_buckets[key]), 4)
                if rec_buckets.get(key) else None
            ),
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
    # Gate on the MEDIAN (robust) rather than the mean, so a couple of residual
    # in-play outliers can't keep a truly-negative bucket alive (or vice-versa).
    return b.get("median_clv_pp", b["avg_clv_pp"]) >= min_clv_pp


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
            "sport": sport, "market_type": mt, "n": b["n"],
            # median is the headline (robust); mean kept for reference; contaminated
            # count surfaces how many in-play-leak bets were excluded.
            "median_clv_pp": b.get("median_clv_pp", b["avg_clv_pp"]),
            "avg_clv_pp": b["avg_clv_pp"],
            "n_contaminated": b.get("n_contaminated", 0),
            "enabled": is_bucket_enabled(stats, sport, mt, min_sample, min_clv_pp),
            "status": ("suppressed (negative CLV)" if not is_bucket_enabled(stats, sport, mt, min_sample, min_clv_pp)
                       else "enabled (proven)" if b["n"] >= min_sample else "enabled (gathering data)"),
        }
        for (sport, mt), b in stats.items()
    ]
    rows.sort(key=lambda r: r["n"], reverse=True)
    return rows


# ---- CONDITIONAL CLV: does the edge live in a SUBSET, not the average? -------
# "No average edge" doesn't mean "no edge anywhere" -- these pre-registered
# slices let a real edge in a corner of the data surface (or not). Discipline:
# only a-priori-sensible slices, and a slice needs real per-slice sample before
# it means anything (same min_sample logic as the buckets). NOT a fishing licence.
_EDGE_BANDS = [(0.03, 0.05, "3-5pp"), (0.05, 0.08, "5-8pp"), (0.08, 0.12, "8-12pp"), (0.12, 1.0, "12pp+")]

_COND_TTL_SECONDS = 300
_cond_cache: dict = {"at": 0.0, "value": None}


def _entry_was_unquoted(session, bet) -> bool:
    """True when this bet's ENTRY price came off a market nobody had quoted.

    Such a `market_prob_at_placement` is a placeholder, not a price, so the CLV
    measured against it is meaningless in either direction. Excluded rather than
    counted as contaminated, because the defect is at the ENTRY end -- the
    closing snapshot on these is fine.

    Rows from before paper_logger started requiring a real quote. Measured: of
    400 sampled tennis bets logged at <=0.5%, 400 of 400 had no bid, no ask and
    zero volume against a median model probability of 0.489 -- a ~48pp "edge"
    over a number nobody offered. 978 of 5,327 tennis paper bets are this shape,
    and they are why tennis reads 76% contaminated while MLB, soccer, NFL, WNBA,
    MMA and CFB read ~0%.

    Tested EXACTLY (no quote and no volume at placement) rather than by a price
    threshold: `EXTREME_MARKET_PRICE` is 0.10, and excluding everything under
    10% would throw away every legitimate longshot along with the junk.
    """
    from app.db.models import MarketSnapshot

    snap = (
        session.query(MarketSnapshot)
        .filter(MarketSnapshot.market_id == bet.market_id, MarketSnapshot.ts <= bet.placed_at)
        .order_by(MarketSnapshot.ts.desc())
        .first()
    )
    if snap is None:
        return False   # can't tell -- fail open, keep the row
    return snap.yes_bid is None and snap.yes_ask is None and not (snap.volume or 0) > 0


def _clean_closed_clvs(session: Session):
    """[(bet, clv_pp)] for closed, non-contaminated bets -- the shared input to
    every conditional slice (contamination excluded, same |clv|<=0.20 rule)."""
    out = []
    for bet in session.query(PlacedBet).all():
        clv = compute_bet_clv(session, bet)
        v = clv.get("clv_pp")
        if clv["status"] != "closed" or v is None or abs(v) > _CONTAMINATION_PP:
            continue
        if _entry_was_unquoted(session, bet):   # same order-of-checks reason
            continue
        out.append((bet, v))
    return out


def _slice_row(label_key: str, label: str, vals: list[float]) -> dict:
    return {
        label_key: label, "n": len(vals),
        "median_clv_pp": round(statistics.median(vals), 4) if vals else 0.0,
        "mean_clv_pp": round(statistics.mean(vals), 4) if vals else 0.0,
    }


def conditional_clv_report(session: Session) -> dict:
    """Pre-registered conditional-CLV slices (TTL-cached). Currently:
      * by_edge_band -- does a BIGGER model edge earn better CLV? (the skill test)
      * by_tennis_tier -- is the edge in thinner/softer markets (challenger/itf)?
    Both report median (robust) + mean + n, contamination excluded."""
    now = time.time()
    if _cond_cache["value"] is not None and now - _cond_cache["at"] < _COND_TTL_SECONDS:
        return _cond_cache["value"]

    rows = _clean_closed_clvs(session)

    by_band: dict[str, list[float]] = {lbl: [] for _lo, _hi, lbl in _EDGE_BANDS}
    for bet, v in rows:
        e = bet.edge_at_placement
        if e is None:
            continue
        for lo, hi, lbl in _EDGE_BANDS:
            if lo <= e < hi:
                by_band[lbl].append(v)
                break

    tennis_tier: dict[str, list[float]] = defaultdict(list)
    for bet, v in rows:
        if bet.sport == "tennis" and bet.tennis_match_id:
            m = session.get(TennisMatch, bet.tennis_match_id)
            if m and m.tier:
                tennis_tier[m.tier].append(v)

    value = {
        "by_edge_band": [_slice_row("band", lbl, by_band[lbl]) for _lo, _hi, lbl in _EDGE_BANDS if by_band[lbl]],
        "by_tennis_tier": [_slice_row("tier", t, vs) for t, vs in sorted(tennis_tier.items())],
    }
    _cond_cache.update(at=now, value=value)
    return value
