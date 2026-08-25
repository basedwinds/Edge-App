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
import time

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import PlacedBet

log = logging.getLogger("exposure")

# Fractions of bankroll. Futures get the smaller share deliberately: they ship
# model_validated: false, this app's own backtests found the market beats the
# model on them, and they lock capital for a whole season -- so the
# least-validated corner of the app should not be able to become the largest
# position.
#
# REBALANCED 2026-08-07, 15/45 -> 20/40. THE TOTAL IS HELD AT 60% ON PURPOSE:
# these two are one bankroll, so raising futures without lowering games would
# just raise total exposure, which is the thing the caps exist to bound. 40% of
# the bankroll stays genuinely uncommitted either way.
#
# Why futures needed the room: 15% of a $2,000 bankroll is $300, and the live
# board was already suggesting $252.50 of futures (101 bets) with CFB and NFL
# season inventory still arriving -- 84% subscribed before the two biggest
# futures sports switch on. The game side was the place to take it from: it was
# carrying $210 against a $900 ceiling, 23% utilised, and game bets settle in
# days so they recycle their room constantly.
TOTAL_EXPOSURE_CAP_PCT = 0.60  # invariant: futures_pct + game_pct must equal this
DEFAULT_FUTURES_EXPOSURE_CAP_PCT = 0.20
DEFAULT_GAME_EXPOSURE_CAP_PCT = 0.40

# THESE NUMBERS ARE NOT THE ONES THE APP ACTUALLY REACHES. The cross-sport
# recommendation page (frontend/src/pages/Combined.tsx, GLOBAL_CAP_PCT) applies
# its OWN ceiling to the same quantity -- 30% game / 10% futures, also net of
# outstanding bets -- and being stricter, it is what binds in practice. A slate
# can never be funded past 30% even though this file would allow 40%.
#
# That layering is intentional: a recommendation list should sit inside the hard
# safety stop, not on top of it. But it means the 40/20 here is a BACKSTOP, not
# the working limit, and quoting it as "the cap" is wrong -- the user sees 30/10.
# Verified 2026-08-11: $170 outstanding, so neither ceiling is close to binding.
#
# CHANGE BOTH OR NEITHER. Raising this alone does nothing; lowering the frontend
# alone silently tightens the app with no trace in this file.

# No single sport may hold more than this share of the FUTURES side.
#
# The obvious alternative -- rank every futures candidate by edge and fill from
# the top -- is actively harmful here, and the live board says why. Ranked
# globally by edge the order is CFB (+54pp, model 73.1% vs market 19.0%, a 3.8x
# ratio, with one team taking 3 of the top 4 slots), then soccer (+37pp), NFL
# (+33pp), MLB (+21pp). That is not CFB having the best opportunities; it is
# elo_cfb having the widest rating spread in the app (its own playoff comment
# records the top team at 40.5% to win the title where books top out near
# 15-20%). Sorting by raw edge sorts by MODEL AGGRESSIVENESS, so a global
# ranking hands the whole futures budget to whichever model is worst calibrated
# -- the same "staking your own biggest errors" failure that flat-staked ~$3,790
# across ~200 player-stat futures on implausible +50-60pp edges.
#
# A per-sport ceiling is the defence: edges are comparable WITHIN a sport (same
# model, same calibration) and not across them. 0.25 is deliberately loose --
# with four or more sports active nobody can run away, and with only one or two
# in season nobody is artificially starved. It is not an allocation: a sport
# only ever holds what it actually earns, this just bounds the top.
DEFAULT_FUTURES_PER_SPORT_CAP_FRACTION = 0.25

# THE GAME SIDE NEEDS THIS TOO (2026-08-09, user's stated goal: "each sport has
# opportunity in the bankroll to make bets on both games and futures").
#
# The futures side has had a per-sport ceiling since it was built. The game side
# had NONE -- only the global 40%. So one sport could consume the entire game
# pool and starve every other, which is not hypothetical: MLB alone was carrying
# $477 of suggested game exposure against an $800 side.
#
# Same 0.25 as futures, deliberately: it needs no separate justification, and it
# means at least four sports can always be fully funded on each side. An equal
# 1/13 share would be $61 and too tight to place a real bet in -- most sports
# are not live simultaneously, so the ceiling exists to stop monopolisation, not
# to pre-divide the pool.
DEFAULT_GAME_PER_SPORT_CAP_FRACTION = 0.25

# No single TEAM may hold more than this share of the futures side. A BACKSTOP,
# deliberately loose: 0.075 is $30 at the current bankroll, which is 12 futures
# bets at the flat $2.50 rung, so it bites only on genuine runaway
# concentration and not on ordinary multi-market exposure to a team.
#
# It is a backstop because it is the WRONG instrument for the more common
# problem. Measured on the live board 2026-08-07: Milwaukee was 35.3% of the
# MLB futures book across best_record / conference_champion x2 /
# division_winner x2 / win_total, and Manchester City 27.3% of soccer's across
# league_winner + top2 + top4. Those two cases are not the same. Milwaukee's
# six are genuinely different outcomes that happen to share one team-strength
# input -- real, if correlated, diversification, and a dollar ceiling is the
# right tool. City's three are STRICTLY NESTED (league_winner implies top2
# implies top4): one view staked three times at different thresholds, which no
# dollar cap makes sensible. That case is fixed where it belongs, in the
# recommended-bet ladder collapse, by treating nested threshold families as the
# ladders they are.
DEFAULT_FUTURES_PER_TEAM_CAP_FRACTION = 0.075

FUTURES_POOL = "futures"
GAME_POOL = "weekly"  # the stored enum value for per-game bets; see staking.py
# Snapshot key holding the per-sport futures ceiling itself, so a sport with NO
# outstanding futures (which therefore has no "futures:<sport>" row of its own)
# still resolves to the ceiling rather than to "uncapped".
FUTURES_PER_SPORT_CEILING_KEY = "futures:_ceiling"
GAME_PER_SPORT_CEILING_KEY = "weekly:_ceiling"
FUTURES_PER_TEAM_CEILING_KEY = "futures:_team_ceiling"


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


def enforce_total(futures_pct: float, game_pct: float) -> tuple[float, float]:
    """Hold futures_pct + game_pct == TOTAL_EXPOSURE_CAP_PCT, adjusting the GAME
    side to absorb any difference.

    The two caps are two halves of ONE bankroll, so they are not independently
    settable: raising futures without lowering games raises total exposure,
    which is precisely what the caps exist to bound. Settings exposes them as
    two numbers, so without this a user (or a future default change) can raise
    one and silently over-expose the bankroll.

    Games absorb the adjustment rather than futures because game bets settle in
    days and recycle their capital constantly, while a futures position holds
    its room for a whole season -- so the game side is where a few points of
    ceiling costs the least. Futures is also the side under an explicit request
    to grow.

    Clamped so neither side can go negative or exceed the total.
    """
    futures = min(max(futures_pct, 0.0), TOTAL_EXPOSURE_CAP_PCT)
    game = TOTAL_EXPOSURE_CAP_PCT - futures
    if abs(game - game_pct) > 1e-9:
        log.info("exposure caps: game %.3f -> %.3f to hold futures+game at %.2f",
                 game_pct, game, TOTAL_EXPOSURE_CAP_PCT)
    return futures, game


def outstanding_futures_by_sport(session: Session) -> dict[str, float]:
    """{sport: dollars} of REAL, still-pending placed bets on the FUTURES side.

    Same pending/real filter as outstanding_real_exposure -- see it for why.
    PlacedBet.sport is the stored sport at placement, which is what we want: a
    market being re-typed later must not silently move an existing bet between
    sports' ceilings.
    """
    rows = (
        session.query(PlacedBet.sport, func.coalesce(func.sum(PlacedBet.stake_dollars), 0.0))
        .filter(PlacedBet.paper == False, PlacedBet.status == "pending",  # noqa: E712
                PlacedBet.stake_pool == FUTURES_POOL)
        .group_by(PlacedBet.sport)
        .all()
    )
    return {(sport or "unknown"): float(total or 0.0) for sport, total in rows}


def outstanding_game_by_sport(session: Session) -> dict[str, float]:
    """{sport: dollars} of REAL, still-pending placed bets on the GAME side.
    Mirror of outstanding_futures_by_sport."""
    rows = (
        session.query(PlacedBet.sport, func.coalesce(func.sum(PlacedBet.stake_dollars), 0.0))
        .filter(PlacedBet.paper == False,  # noqa: E712
                PlacedBet.status == "pending",
                func.coalesce(PlacedBet.stake_pool, GAME_POOL) == GAME_POOL)
        .group_by(PlacedBet.sport)
        .all()
    )
    return {(sport or "unknown"): float(total or 0.0) for sport, total in rows}


def outstanding_futures_by_team(session: Session) -> dict[tuple[str, str], float]:
    """{(sport, team): dollars} of REAL, still-pending placed FUTURES bets.

    Keyed by (sport, team) rather than team alone because team codes collide
    across sports -- "ATL" is both the Braves and the Falcons, and both are on
    the live board.
    """
    rows = (
        session.query(PlacedBet.sport, PlacedBet.team,
                      func.coalesce(func.sum(PlacedBet.stake_dollars), 0.0))
        .filter(PlacedBet.paper == False, PlacedBet.status == "pending",  # noqa: E712
                PlacedBet.stake_pool == FUTURES_POOL, PlacedBet.team.isnot(None))
        .group_by(PlacedBet.sport, PlacedBet.team)
        .all()
    )
    return {((sport or "unknown"), team): float(total or 0.0) for sport, team, total in rows}


def capacity(session: Session, bankroll: float, futures_pct: float, game_pct: float) -> dict[str, float]:
    """{pool: dollars still available} -- never negative.

    Passed into staking.size_stake_dollars, which refuses to size a bet whose
    side has no room left. Enforced there rather than in each router for the
    same reason FUTURES_MIN_MARKET_PRICE is: the cross-sport lists filter on
    `suggested_stake_dollars != null`, so a rule living in the one function
    every sizing path already calls cannot drift out of step with the view.
    """
    used = outstanding_real_exposure(session)
    caps = {
        FUTURES_POOL: max(0.0, bankroll * futures_pct - used.get(FUTURES_POOL, 0.0)),
        GAME_POOL: max(0.0, bankroll * game_pct - used.get(GAME_POOL, 0.0)),
    }
    # Per-sport futures headroom, keyed "futures:<sport>". Stored alongside the
    # global numbers rather than in a second structure so one snapshot refresh
    # still covers everything a sizing call needs.
    per_sport_ceiling = bankroll * futures_pct * DEFAULT_FUTURES_PER_SPORT_CAP_FRACTION
    caps[FUTURES_PER_SPORT_CEILING_KEY] = per_sport_ceiling
    for sport, spent in outstanding_futures_by_sport(session).items():
        caps[f"{FUTURES_POOL}:{sport}"] = max(0.0, per_sport_ceiling - spent)
    # Per-TEAM headroom, keyed "futures:<sport>:<team>". Scoped by sport as well
    # as team so two different sports' "ATL" (the Braves and the Falcons, both
    # live on the board right now) do not share one ceiling.
    # Per-sport GAME headroom, keyed "weekly:<sport>" -- the mirror of the
    # futures ceiling above, so neither side can be monopolised by one sport.
    game_per_sport_ceiling = bankroll * game_pct * DEFAULT_GAME_PER_SPORT_CAP_FRACTION
    caps[GAME_PER_SPORT_CEILING_KEY] = game_per_sport_ceiling
    for sport, spent in outstanding_game_by_sport(session).items():
        caps[f"{GAME_POOL}:{sport}"] = max(0.0, game_per_sport_ceiling - spent)

    per_team_ceiling = bankroll * futures_pct * DEFAULT_FUTURES_PER_TEAM_CAP_FRACTION
    caps[FUTURES_PER_TEAM_CEILING_KEY] = per_team_ceiling
    for (sport, team), spent in outstanding_futures_by_team(session).items():
        caps[f"{FUTURES_POOL}:{sport}:{team}"] = max(0.0, per_team_ceiling - spent)
    return caps


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
# WHEN the snapshot was last refreshed, so its absence or staleness can be
# ALARMED ON rather than silently tolerated. remaining_for_unit_scale returns
# None (= uncapped) with no snapshot, which is the right default for a cold
# process but means the hard cap goes quiet if refresh ever stops running, with
# nothing to notice. See health.py::_check_exposure_snapshot.
_snapshot_at: float | None = None
_lock = threading.Lock()


def snapshot_status() -> tuple[int, float | None]:
    """(number of cap entries, age in seconds) -- age is None if never refreshed.

    Read under the same lock the refresh writes under, so the count and the
    timestamp can never disagree.
    """
    with _lock:
        if _snapshot_at is None:
            return len(_snapshot), None
        return len(_snapshot), max(0.0, time.time() - _snapshot_at)


def refresh_snapshot(session: Session, bankroll: float, futures_pct: float, game_pct: float) -> dict[str, float]:
    # Normalised HERE rather than at the settings call site so the invariant
    # holds for every caller, including tests and any future refresh path.
    futures_pct, game_pct = enforce_total(futures_pct, game_pct)
    caps = capacity(session, bankroll, futures_pct, game_pct)
    global _snapshot_at
    with _lock:
        _snapshot.clear()
        _snapshot.update(caps)
        _snapshot_at = time.time()
    return caps


def remaining_for_unit_scale(unit_scale: float, sport: str | None = None,
                             team: str | None = None) -> float | None:
    """Capacity for the side this bet belongs to, or None if no snapshot has
    been taken yet (= uncapped, the safe default).

    The side is read off `unit_scale`, which is the existing, consistently
    applied marker for a season-long market: staking.FUTURES_UNIT_SCALE is
    documented as being passed by exactly the futures sizing paths and nothing
    else. If a non-futures market ever needs a scale other than 1.0, this
    inference breaks and the side must become an explicit argument.

    `sport` applies the per-sport futures ceiling ON TOP of the global futures
    number -- the binding constraint is whichever is tighter. It is OPTIONAL by
    design: a futures caller that doesn't pass it still gets the global cap, so
    a router nobody remembered to update degrades to the previous, safe
    behaviour instead of going uncapped. The GAME side now has its own per-sport
    ceiling as well -- see DEFAULT_GAME_PER_SPORT_CAP_FRACTION for why the
    earlier "game bets recycle, so concentration does not matter" reasoning was
    wrong in practice.
    """
    with _lock:
        if not _snapshot:
            return None
        if unit_scale == 1.0:
            overall_game = _snapshot.get(GAME_POOL)
            if sport is None:
                return overall_game
            limits = [x for x in (overall_game,) if x is not None]
            per_sport_game = _snapshot.get(f"{GAME_POOL}:{sport}",
                                           _snapshot.get(GAME_PER_SPORT_CEILING_KEY))
            if per_sport_game is not None:
                limits.append(per_sport_game)
            return min(limits) if limits else None
        overall = _snapshot.get(FUTURES_POOL)
        if sport is None:
            return overall
        # Tightest of the three ceilings binds: global futures, this sport's
        # share, and this team's share. Each is skipped when its key is absent
        # so a partially-populated snapshot never reads as "no room".
        limits = [x for x in (overall,) if x is not None]
        per_sport = _snapshot.get(f"{FUTURES_POOL}:{sport}", _snapshot.get(FUTURES_PER_SPORT_CEILING_KEY))
        if per_sport is not None:
            limits.append(per_sport)
        if team:
            per_team = _snapshot.get(f"{FUTURES_POOL}:{sport}:{team}",
                                     _snapshot.get(FUTURES_PER_TEAM_CEILING_KEY))
            if per_team is not None:
                limits.append(per_team)
        return min(limits) if limits else None


CAP_REACHED_REASON = (
    "Bankroll cap reached for this side -- real outstanding exposure is already at its limit, so "
    "this is shown unsized rather than stacked on top. Room frees up as placed bets settle."
)
