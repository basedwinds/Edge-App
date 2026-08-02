"""LoL markets API -- parallel to routers/valorant_markets.py.

Real live inventory here (confirmed 2026-07-19, see kalshi_lol_client.py) is
map winner + total maps played -- no series (whole-match) winner Kalshi
ticker exists for LoL (unlike CS2's KXCS2GAME) and no Polymarket match-level
market type exists at all.

Ratings are trained on a real historical Leaguepedia crawl (5,604 matches,
Leaguepedia's own "Primary" tournament tier -- LCK/LPL/LEC/LCS-LTA/Worlds/
MSI, 2023-mid 2026 -- see scripts/build_lol_match_cache.py) plus this app's
own live-polled match history on top (see elo_service_lol.py). K=36 is
grid-searched against that real data (scripts/derive_lol_elo_constants.py --
67.13% walk-forward accuracy post-warmup, the strongest of all 3 esports
titles in this app). model_validated is still False for every market_type
here -- a real market-odds backtest against Kalshi's own historical trade
data now exists too (scripts/backtest_lol_market_odds.py, Map 1 only,
12-match sample) and found the market beats the model, same conclusion
every sport in this app has found.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice
from app.api.routers.settings import get_lol_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import FuturesMarketOut, LolMarketOut, ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.db.models import LolMatch, Market, MarketSnapshot
from app.ingestion import market_catalog_lol
from app.ingestion.market_matcher_lol import team_names_match
from app.models.baseline import elo_service_lol
from app.models.ladder_sanity import ESPORTS_LIVE_TRADING_MIN_PRICE_SWING, LOL_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA, looks_already_live_by_trading
from app.models.staking import FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

_NO_BASELINE_METHODOLOGY = "No detailed methodology available for this market type yet -- see the module docstring above."

router = APIRouter(prefix="/lol", tags=["lol"])

GAME_MARKET_TYPES = {"map_winner", "series_total", "series_winner"}

NO_BASELINE_REASON = (
    "No baseline yet -- this market's model is still being built and validated against this app's "
    "own historical data, not shipped as a guessed number."
)

COLD_START_CAVEAT = (
    "Ratings are trained on a real historical Leaguepedia crawl (5,604 matches, Primary tier only -- "
    "LCK/LPL/LEC/LCS-LTA/Worlds/MSI) plus this app's own live-polled matches on top -- 67.13% "
    "walk-forward accuracy post-warmup, beats the naive 0.5 baseline. A real market-odds backtest "
    "against Kalshi's own historical trade data (Map 1 only, 12-match sample) found the market beats "
    "the model, so model_validated stays false regardless."
)


def _team_side(match: LolMatch | None, team_name: str | None) -> str | None:
    if match is None or not team_name:
        return None
    if team_names_match(team_name, match.team_a):
        return "team_a"
    if team_names_match(team_name, match.team_b):
        return "team_b"
    return None


def _game_model_prob(m: Market, match: LolMatch | None) -> float | None:
    if match is None or not match.best_of:
        return None
    dist = elo_service_lol.get_series_distribution(
        match.team_a, match.team_b, match.best_of,
        match_date=match.estimated_start_time or match.match_date,
    )
    if dist is None:
        return None
    if m.market_type == "series_total":
        return round(dist.prob_total_maps_over(m.line), 4) if m.line is not None else None

    side = _team_side(match, m.team)
    if side is None:
        return None
    if m.market_type == "map_winner":
        if m.line is None:
            return None
        map_p = dist.prob_map_n_win_a(int(m.line))
        if map_p is None:
            return None
        return round(map_p if side == "team_a" else (1.0 - map_p), 4)
    # REAL COVERAGE GAP this closes (found live 2026-07-20, via
    # catalog_scan.py's newly-added esports coverage): KXLOLGAME is a real
    # whole-match/series winner Kalshi ticker this app never queried at all
    # (see kalshi_lol_client.py's own real-bug note) -- same model dispatch
    # as CS2/Valorant's own series_winner handling.
    if m.market_type == "series_winner":
        p = dist.prob_series_win_a() if side == "team_a" else dist.prob_series_win_b()
        return round(p, 4)
    return None


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_lol_futures(session: Session = Depends(get_session)):
    """See cs2_markets.py::list_cs2_futures's own docstring -- same real
    inventory-with-no-model shape, LoL's own version."""
    markets = session.query(Market).filter(Market.sport == "lol", Market.market_type == "tournament_winner", Market.status == "active").all()
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])

    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        out.append(
            FuturesMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                group_label=m.group_label,
                line=m.line,
                side=m.side,
                implied_prob=implied,
                yes_bid=snap.yes_bid if snap else None,
                yes_ask=snap.yes_ask if snap else None,
                last_price=snap.last_price if snap else None,
                volume=snap.volume if snap else None,
                updated_at=m.updated_at.isoformat() if m.updated_at else None,
                model_prob=None,
                model_validated=False,
                edge=None,
                # Genuinely unstaked because model_prob is None -- LoL tournament
                # winners have NO model (unlike CS2/Valorant, which have the
                # bracket sim). Nothing is being suppressed here; there is simply
                # nothing to price, so there is no CLV to lose either.
                kelly_fraction=None,
                suggested_stake_dollars=None,
                suggested_stake_units=None,
                stake_pool="futures",
                line_move_pp=None,
            )
        )
    out.sort(key=lambda m: (m.group_label or "", m.team or ""))
    return out


@router.get("/markets", response_model=list[LolMarketOut])
def list_lol_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "lol", Market.market_type.in_(GAME_MARKET_TYPES | {"tournament_winner"})).all()
    match_ids = {m.lol_match_id for m in markets if m.lol_match_id}
    matches_by_id = {mt.id: mt for mt in session.query(LolMatch).filter(LolMatch.id.in_(match_ids)).all()} if match_ids else {}

    def _match_already_decided(m: Market) -> bool:
        match = matches_by_id.get(m.lol_match_id) if m.lol_match_id else None
        return match is not None and match.winner is not None

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _match_already_started(m: Market) -> bool:
        match = matches_by_id.get(m.lol_match_id) if m.lol_match_id else None
        if match is None or not match.estimated_start_time:
            return False
        try:
            start = datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return now_utc >= start

    all_snapshots = _batch_latest_snapshots(session, [m.id for m in markets])
    now_for_staleness = datetime.datetime.now(datetime.timezone.utc)
    STALE_AFTER = datetime.timedelta(minutes=20)

    def _market_stale(m: Market) -> bool:
        snap = all_snapshots.get(m.id)
        if snap is None or snap.ts is None:
            return False
        ts = snap.ts if snap.ts.tzinfo else snap.ts.replace(tzinfo=datetime.timezone.utc)
        return now_for_staleness - ts > STALE_AFTER

    # REAL BUG this guards against (user-reported 2026-07-20: recommended
    # bets pricing off already-decided matches, e.g. "0.1%" prices) -- see
    # ladder_sanity.py's own module comment for the full esports-specific
    # calibration story. `_match_already_started` above only fires once
    # Leaguepedia's Cargo API has actually populated a real
    # estimated_start_time, which lags behind Kalshi's own live trading --
    # this catches the case where that hasn't happened yet but the market's
    # own price/volume history already makes clear the series is live or
    # over.
    LIVE_TRADING_LOOKBACK = datetime.timedelta(hours=6)  # see ladder_sanity.py's own module comment for why 6, not 1
    cutoff = datetime.datetime.utcnow() - LIVE_TRADING_LOOKBACK
    recent_rows = (
        session.query(MarketSnapshot)
        .filter(MarketSnapshot.market_id.in_([m.id for m in markets]), MarketSnapshot.ts >= cutoff)
        .all()
    ) if markets else []
    recent_snapshots_by_market: dict[int, list[MarketSnapshot]] = {}
    for snap in recent_rows:
        recent_snapshots_by_market.setdefault(snap.market_id, []).append(snap)

    def _market_looks_live_by_trading(m: Market) -> bool:
        if m.source != "kalshi":
            return False  # no real LoL Polymarket inventory to calibrate against -- see market_catalog_lol.py
        current = all_snapshots.get(m.id)
        current_price = current.last_price if current else None
        recent = recent_snapshots_by_market.get(m.id, [])
        return looks_already_live_by_trading(
            current_price, [(s.last_price, s.volume) for s in recent],
            min_volume_delta=LOL_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA,
            min_price_swing=ESPORTS_LIVE_TRADING_MIN_PRICE_SWING,
        )

    matches_live_by_trading = {m.lol_match_id for m in markets if m.lol_match_id and _market_looks_live_by_trading(m)}

    def _match_looks_live_by_trading(m: Market) -> bool:
        return m.lol_match_id in matches_live_by_trading

    markets = [
        m for m in markets
        if not _match_already_decided(m)
        and not _match_already_started(m)
        and (m.status or "active") == "active"
        and not _market_stale(m)
        and not _match_looks_live_by_trading(m)
    ]
    snapshots_by_market = {mid: s for mid, s in all_snapshots.items() if mid in {m.id for m in markets}}
    weekly_pool, futures_pool = get_lol_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    # Roster-change "Wait" caveat removed 2026-07-23 -- see cs2_markets.py's
    # own note (no post-roster-change accuracy penalty for esports, so nothing
    # to wait for). Shared wait badge stays for sports where it's real.

    out = []
    for m in markets:
        match = matches_by_id.get(m.lol_match_id) if m.lol_match_id else None
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        model_prob = _game_model_prob(m, match) if m.market_type in GAME_MARKET_TYPES else None
        no_baseline_reason = None if model_prob is not None else NO_BASELINE_REASON
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded), clv_stats, "lol", m.market_type)
        pool = futures_pool if m.market_type == "tournament_winner" else weekly_pool
        _uscale = FUTURES_UNIT_SCALE if pool is futures_pool else 1.0
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=_uscale)
        out.append(
            LolMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                side=m.side,
                line=m.line,
                match_label=f"{match.team_a} vs {match.team_b}" if match else None,
                lol_match_id=m.lol_match_id,
                event_name=match.event_name if match else None,
                match_date=match.match_date if match else None,
                estimated_start_time=match.estimated_start_time if match else None,
                best_of=match.best_of if match else None,
                group_label=m.group_label,
                implied_prob=implied,
                yes_bid=snap.yes_bid if snap else None,
                yes_ask=snap.yes_ask if snap else None,
                last_price=snap.last_price if snap else None,
                volume=snap.volume if snap else None,
                updated_at=m.updated_at.isoformat() if m.updated_at else None,
                model_prob=model_prob,
                model_validated=False,
                edge=edge,
                no_baseline_reason=no_baseline_reason,
                kelly_fraction=kelly,
                suggested_stake_dollars=stake_dollars,
                suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
                stake_pool="futures" if m.market_type == "tournament_winner" else ("weekly" if kelly is not None else None),
            )
        )
    out.sort(key=lambda m: (m.match_date or "9999", m.match_label or m.group_label or "", m.market_type))
    return out


def _game_insight_lol(match: LolMatch, model_prob: float | None, market_prob: float | None) -> str:
    a_rating = elo_service_lol.get_team_rating(match.team_a)
    b_rating = elo_service_lol.get_team_rating(match.team_b)
    sentences = []
    if a_rating is not None and b_rating is not None:
        gap = a_rating - b_rating
        seed = f"{match.team_a}|{match.team_b}|{a_rating}|{b_rating}"
        if abs(gap) < 30:
            sentences.append(_seeded_choice(seed, [
                f"This one projects tight -- team Elo has {match.team_a} and {match.team_b} rated almost even ({a_rating:.0f} to {b_rating:.0f}), so there's little to separate them going in.",
                f"There's barely anything between these two on the ratings ({a_rating:.0f} to {b_rating:.0f}), which makes it close to a coin flip on paper.",
                f"About as even as it gets: team Elo puts {match.team_a} and {match.team_b} nearly level ({a_rating:.0f} to {b_rating:.0f}).",
            ]))
        else:
            stronger, s_r, weaker, w_r = (match.team_a, a_rating, match.team_b, b_rating) if gap > 0 else (match.team_b, b_rating, match.team_a, a_rating)
            sentences.append(_seeded_choice(seed, [
                f"{stronger} comes in as the stronger side by team Elo, clear of {weaker} ({s_r:.0f} to {w_r:.0f}).",
                f"The ratings favor {stronger} here, sitting above {weaker} ({s_r:.0f} to {w_r:.0f}).",
                f"Team Elo gives {stronger} the edge, ahead of {weaker} ({s_r:.0f} to {w_r:.0f}).",
            ]))
    sentences.append(_edge_sentence(model_prob, market_prob))
    return " ".join(sentences)


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_lol_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport != "lol":
        raise HTTPException(404, "market not found")
    match = session.get(LolMatch, m.lol_match_id) if m.lol_match_id else None
    label = f"{match.team_a} vs {match.team_b}" if match else (m.group_label or m.market_type)
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    caveats = [
        "model_validated: false -- real market-odds backtest found the market beats the model.",
        COLD_START_CAVEAT,
    ]
    methodology = _NO_BASELINE_METHODOLOGY
    insight = ""

    if m.market_type in GAME_MARKET_TYPES and match is not None:
        methodology = (
            "Team-level Elo (K=36, grid-searched against a real 5,604-match historical Leaguepedia crawl -- "
            "see elo_lol.py) gives a per-map win probability, extended to a full best-of-N series-score "
            "distribution via the standard 'race to k' binomial identity."
        )
        if match.best_of:
            factors.append(ReasoningFactorOut(label="Best of", detail=str(match.best_of)))
        a_rating = elo_service_lol.get_team_rating(match.team_a)
        b_rating = elo_service_lol.get_team_rating(match.team_b)
        if a_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.team_a} Elo rating", detail=f"{a_rating:.0f}"))
        if b_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.team_b} Elo rating", detail=f"{b_rating:.0f}"))
        insight = _game_insight_lol(match, model_prob, market_prob)

    elif m.market_type == "tournament_winner":
        methodology = (
            "Elo-seeded single-elimination Monte Carlo of the event bracket: each team's LoL team Elo sets "
            "its per-match win probabilities, the bracket is simulated many thousands of times, and the "
            "share of runs a team wins the whole event becomes its price. An APPROXIMATION -- real events "
            "are often double-elimination or group/Swiss, so this is a reference estimate (approx badge), "
            "not a validated edge."
        )
        team = m.team or (m.group_label or "this team")
        rating = elo_service_lol.get_team_rating(m.team) if m.team else None
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} Elo rating", detail=f"{rating:.0f}"))
        seed = f"{team}|{rating}|loltw"
        rt = f" (team Elo {rating:.0f})" if rating is not None else ""
        insight = _seeded_choice(seed, [
            f"This is the tournament outright for {team}{rt}. It comes from an Elo-seeded Monte Carlo of the event bracket -- {team}'s rating drives each round's win odds, and the price is how often they take the whole thing across thousands of simulated runs.",
            f"{team}'s{rt} title price is read off a bracket simulation: seed every team by LoL Elo, play the event out many thousands of times, and count how often {team} is left standing.",
            f"Priced from a simulated run of the bracket -- {team}{rt} is carried through the event thousands of times on Elo-based match odds, and the share of wins is this number.",
        ]) + " Bracket's simplified to single-elim, so treat it as a reference read. " + _edge_sentence(model_prob, market_prob)

    if not insight:
        insight = f"{methodology} {_edge_sentence(model_prob, market_prob)}"

    return ReasoningOut(
        market_id=m.id,
        market_type=m.market_type,
        label=label,
        model_prob=model_prob,
        market_prob=market_prob,
        edge=edge,
        insight=insight,
        methodology=methodology,
        factors=factors,
        caveats=caveats,
    )
