"""Bankroll exposure caps: how much REAL money may be outstanding at once, split
between the game side and the futures side.

WHY. Until now nothing capped either side. Two independent reasons, both
verified 2026-08-07: the staking mode defaults to "flat", and flat sizing is
explicitly independent of the per-sport pool; and every get_*_pool_dollars
computes bankroll x pct x scale without ever subtracting a placed bet. So the
"$28.16 futures sub-pool" shown in Settings was decorative -- CFB was carrying
$78 of futures against an $18 pool. Each individual bet was sized correctly;
there was simply no ceiling, and futures are the slowest positions to release
capital.

THE RULE IS ON OUTSTANDING EXPOSURE, NOT ON A RECOMMENDATION POOL. That is what
gives headroom back automatically: game bets settle in days and recycle their
room constantly, futures settle at season's end and hold it. A busy week
therefore has capacity without anyone re-tuning a percentage, and the futures
side self-limits because it is slow to free up.

REAL BETS ONLY -- paper is excluded on purpose. Paper is the measurement harness
(see paper_logger); letting it consume real capacity would mean the app stops
recommending real bets because it has been busy simulating. In practice
"outstanding real exposure" is the set of bets marked placed by hand.
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import PlacedBet

log = logging.getLogger("exposure")

# Fractions of bankroll. 15/45 leaves 40% of the bankroll genuinely
# uncommitted. Futures get the smaller share deliberately: they ship
# model_validated: false, this app's own backtests found the market beats the
# model on them, and they lock capital for a whole season -- so the
# least-validated corner of the app should not be able to become the largest
# position. 15% is roughly double the exposure carried when this was written.
DEFAULT_FUTURES_EXPOSURE_CAP_PCT = 0.15
DEFAULT_GAME_EXPOSURE_CAP_PCT = 0.45

FUTURES_POOL = "futures"
GAME_POOL = "weekly"  # the stored enum value for per-game bets; see staking.py


def outstanding_real_exposure(session: Session) -> dict[str, float]:
    """{pool: dollars} of REAL, still-undecided placed bets.

    Pending only: a settled bet has already returned its capital (win or lose)
    and must not keep occupying the cap forever. `paper == False` is the
    "actually placed by the user" filter.
    """
    rows = (
        session.query(PlacedBet.stake_pool, func.coalesce(func.sum(PlacedBet.stake_dollars), 0.0))
        .filter(PlacedBet.paper == False, PlacedBet.status == "pending")  # noqa: E712 -- SQLAlchemy needs ==
        .group_by(PlacedBet.stake_pool)
        .all()
    )
    return {(pool or GAME_POOL): float(total or 0.0) for pool, total in rows}


def capacity(session: Session, bankroll: float, futures_pct: float, game_pct: float) -> dict[str, float]:
    """{pool: dollars still available} -- never negative.

    Passed into staking.size_stake_dollars, which refuses to size a bet whose
    side has no room left. Enforced there rather than in each router for the
    same reason FUTURES_MIN_MARKET_PRICE is: the cross-sport lists filter on
    `suggested_stake_dollars != null`, so a rule living in the one function
    every sizing path already calls cannot drift out of step with the view.
    """
    used = outstanding_real_exposure(session)
    return {
        FUTURES_POOL: max(0.0, bankroll * futures_pct - used.get(FUTURES_POOL, 0.0)),
        GAME_POOL: max(0.0, bankroll * game_pct - used.get(GAME_POOL, 0.0)),
    }


# ---- process-level snapshot ------------------------------------------------
# Threading a capacity argument through all 22 size_stake_dollars call sites
# would work, and would also be exactly the shape of bug this codebase keeps
# hitting: a rule added to every caller except the one somebody forgets (the
# CLV gate, the soccer prefix map, the esports MIN_GAMES rollout). Instead the
# snapshot is refreshed at the ONE place every sizing router already passes
# through -- settings.get_staking_params -- and read inside size_stake_dollars.
# A router cannot opt out, because it cannot size a bet without first asking for
# the staking params.
#
# Concurrency: read-mostly, refreshed per request, and a cap is advisory sizing
# rather than an accounting ledger, so a snapshot that is one request stale is
# fine. It is NOT a substitute for checking real exposure before actually
# placing money.
_snapshot: dict[str, float] = {}
_lock = threading.Lock()


def refresh_snapshot(session: Session, bankroll: float, futures_pct: float, game_pct: float) -> dict[str, float]:
    caps = capacity(session, bankroll, futures_pct, game_pct)
    with _lock:
        _snapshot.clear()
        _snapshot.update(caps)
    return caps


def remaining_for_unit_scale(unit_scale: float) -> float | None:
    """Capacity for the side this bet belongs to, or None if no snapshot has
    been taken yet (= uncapped, the safe default).

    The side is read off `unit_scale`, which is the existing, consistently
    applied marker for a season-long market: staking.FUTURES_UNIT_SCALE is
    documented as being passed by exactly the futures sizing paths and nothing
    else. If a non-futures market ever needs a scale other than 1.0, this
    inference breaks and the side must become an explicit argument.
    """
    with _lock:
        if not _snapshot:
            return None
        return _snapshot.get(FUTURES_POOL if unit_scale != 1.0 else GAME_POOL)


CAP_REACHED_REASON = (
    "Bankroll cap reached for this side -- real outstanding exposure is already at its limit, so "
    "this is shown unsized rather than stacked on top. Room frees up as placed bets settle."
)
