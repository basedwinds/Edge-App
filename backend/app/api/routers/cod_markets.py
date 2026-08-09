"""Call of Duty pricing route. Fifth esports title, and the smallest one --
Kalshi lists match_winner and nothing else for CoD (no spread, no totals, no
per-map, no futures), so this router prices exactly one market type.

MODEL: team Elo over 3,615 real matches (2020-2026) from breakingpoint.gg.
Walk-forward accuracy 0.6479 over 2,508 scored predictions, z = 14.8, which
sits between CS2 (0.6075) and LoL (0.6713). See
scripts/check_cod_walkforward.py.

model_validated is FALSE and stays false. Beating a coin flip is not beating a
market; no backtest against real CoD odds has been run.

=========================================================================
THE LIVE GUARD IS THE PART TO READ. This router was written the same day two
separate live matches were found being recommended as bets:

  * soccer, Nuremberg vs Dresden -- Kalshi's occurrence_datetime said 14:30Z
    against a real 11:30Z kickoff, so a match live at 1-0 down was offered;
  * Call of Duty, found while wiring THIS router -- Kalshi's
    occurrence_datetime said 17:00Z for Team Heretics vs Team Falcons, which
    was already live at 13:00Z with Heretics up 2-0. Our model said Falcons
    0.68 (a pre-match number); the market said 0.37 (a live one). The 31pp
    "edge" between them was entirely artifact.

So CoD does not rely on a clock it cannot trust. breakingpoint reports match
status directly, CodMatch.is_live stores it, and this router refuses to price
a live match on that flag FIRST. The start-time comparison is kept as a
backstop for the window before a poll observes the change, and the Kalshi
client already prefers the ticker's own clock over occurrence_datetime.

Three independent things must all agree a match has not started. That is
deliberate for a market type where being wrong means betting into a known
result.
=========================================================================
"""
from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routers.settings import (
    get_cod_pool_dollars, get_flat_params, get_unit_dollars,
)
from app.db.database import get_session
from app.db.models import CodMatch, Market, MarketSnapshot
from app.models.baseline import elo_service_cod
from app.models.clv_selection import bucket_clv_stats, gate_kelly
from app.models.staking import has_real_trading, kelly_fraction, size_stake_dollars

log = logging.getLogger("cod_markets")

router = APIRouter(prefix="/cod", tags=["cod"])

LIVE_REASON = (
    "Not priced: this match is already in progress. The model produces a "
    "PRE-match probability, so comparing it to a live price measures the "
    "score, not an edge."
)
# _already_started covers finished matches too, and telling a user a decided
# match is "in progress" is just wrong -- the two states are distinguished so
# the row explains itself accurately.
DECIDED_REASON = (
    "Not priced: this match has finished. Any remaining price is the market "
    "settling, not an opportunity."
)
NO_BASELINE_REASON = (
    "Not priced: at least one team has fewer than the minimum real settled "
    "series needed for a trustworthy rating."
)
UNBOUND_REASON = (
    "Not priced: this market is not yet bound to a known fixture."
)


class CodMarketOut(BaseModel):
    # `model_validated` collides with pydantic's protected `model_` namespace.
    # Same opt-out every other sport's row model uses -- the field name is part
    # of the frontend contract across all of them, so it is the namespace that
    # gives way, not the name.
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str
    source: str
    team: str | None
    match_label: str | None
    cod_match_id: int | None
    event_name: str | None
    match_date: str | None
    estimated_start_time: str | None
    best_of: int | None
    is_live: bool
    implied_prob: float | None
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    volume: float | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    no_baseline_reason: str | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


def _implied_prob(snap: MarketSnapshot | None) -> float | None:
    """Mid of the book when both sides quote, else the last trade.

    A market with NO book and a seeded last_price is deliberately NOT priced
    here -- that is the phantom-0.500 pattern that produced fake edges across
    every sport once already."""
    if snap is None:
        return None
    if snap.yes_bid is not None and snap.yes_ask is not None:
        return (snap.yes_bid + snap.yes_ask) / 2.0
    return snap.last_price


def _latest_snapshots(session: Session, market_ids: list[int]) -> dict[int, MarketSnapshot]:
    out: dict[int, MarketSnapshot] = {}
    if not market_ids:
        return out
    rows = (session.query(MarketSnapshot)
            .filter(MarketSnapshot.market_id.in_(market_ids))
            .order_by(MarketSnapshot.ts.asc()).all())
    for r in rows:
        out[r.market_id] = r  # ascending, so the last write per id is the newest
    return out


def _already_started(match: CodMatch | None, now: datetime.datetime) -> bool:
    """True if the match is live or finished. Flag first, clock second."""
    if match is None:
        return False
    if match.is_live:
        return True          # the SOURCE says so -- no inference involved
    if match.winner is not None:
        return True
    if not match.estimated_start_time:
        return False
    try:
        start = datetime.datetime.fromisoformat(
            match.estimated_start_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime.timezone.utc)
    return start < now       # backstop for the gap before a poll sees it


@router.get("/markets", response_model=list[CodMarketOut])
def list_cod_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "cod").all()
    match_ids = [m.cod_match_id for m in markets if m.cod_match_id]
    matches_by_id = {m.id: m for m in session.query(CodMatch).filter(CodMatch.id.in_(match_ids)).all()} if match_ids else {}
    snapshots = _latest_snapshots(session, [m.id for m in markets])

    weekly_pool, _futures_pool = get_cod_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    from app.models.staking import FRACTIONAL_KELLY, MAX_STAKE_FRACTION, MIN_EDGE_TO_BET
    clv_stats = bucket_clv_stats(session)
    now = datetime.datetime.now(datetime.timezone.utc)

    out: list[CodMarketOut] = []
    for m in markets:
        match = matches_by_id.get(m.cod_match_id) if m.cod_match_id else None
        snap = snapshots.get(m.id)
        implied = _implied_prob(snap)

        model_prob = None
        reason = None
        if match is None:
            reason = UNBOUND_REASON
        elif match.winner is not None:
            reason = DECIDED_REASON
        elif _already_started(match, now):
            reason = LIVE_REASON
        else:
            dist = elo_service_cod.get_series_distribution(
                match.team_a, match.team_b, match.best_of or 5, match.match_date)
            if dist is None:
                reason = NO_BASELINE_REASON
            else:
                p_a = dist.prob_series_win_a()
                # `team` is whichever side this YES market pays on.
                model_prob = p_a if m.team == match.team_a else (1.0 - p_a)

        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None,
                                      snap.last_price if snap else None)
        kelly = gate_kelly(
            kelly_fraction(model_prob, implied, FRACTIONAL_KELLY, MAX_STAKE_FRACTION,
                           MIN_EDGE_TO_BET, has_traded, snap.yes_ask if snap else None),
            clv_stats, "cod", m.market_type)
        stake_dollars = size_stake_dollars(
            staking_mode, kelly, weekly_pool, model_prob, implied, unit_dollars,
            flat_marginal, flat_full, sport="cod", team=m.team)

        out.append(CodMarketOut(
            id=m.id, market_type=m.market_type, source=m.source, team=m.team,
            match_label=f"{match.team_a} vs {match.team_b}" if match else None,
            cod_match_id=m.cod_match_id,
            event_name=match.event_name if match else None,
            match_date=match.match_date if match else None,
            estimated_start_time=match.estimated_start_time if match else None,
            best_of=match.best_of if match else None,
            is_live=bool(match.is_live) if match else False,
            implied_prob=implied,
            yes_bid=snap.yes_bid if snap else None,
            yes_ask=snap.yes_ask if snap else None,
            last_price=snap.last_price if snap else None,
            volume=snap.volume if snap else None,
            model_prob=model_prob,
            model_validated=False,
            edge=edge,
            no_baseline_reason=reason,
            kelly_fraction=kelly,
            suggested_stake_dollars=stake_dollars,
            suggested_stake_units=(round(stake_dollars / unit_dollars, 3)
                                   if (stake_dollars is not None and unit_dollars > 0) else None),
            stake_pool="weekly" if kelly is not None else None,
        ))

    out.sort(key=lambda r: (r.match_date or "9999", r.match_label or "", r.team or ""))
    return out
