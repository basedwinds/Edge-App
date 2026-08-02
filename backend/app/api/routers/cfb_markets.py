"""College-football markets API -- parallel to routers/wnba_markets.py,
moneyline scope.

Moneyline only, on purpose: KXNCAAFSPREAD and KXNCAAFTOTAL exist as series but
had zero open markets as of 2026-08-02, and elo_cfb is a win-probability model
with no validated margin or totals layer. Adding either would mean shipping
constants nothing measured.

model_validated is always False, and for CFB that matters more than usual: the
Elo's ~0.186 Brier / 71% accuracy looks far stronger than WNBA's 0.222 / 65.4%,
but that reflects college football's enormous talent gaps making many games
near-certain -- NOT an edge over the market. Kalshi has no settled KXNCAAFGAME
markets, so there are no closing prices to backtest edge against at all.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _implied_prob
from app.api.routers.settings import get_staking_params, get_flat_params, get_unit_dollars, get_cfb_pool_dollars
from app.api.schemas import ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.db.models import CfbGame, Market
from app.models import calibration_temp
from app.models import season_sim_cfb
from app.models.baseline import elo_service_cfb
from app.models.clv_selection import bucket_clv_stats, is_bucket_enabled
from app.models.staking import has_real_trading, kelly_fraction, size_stake_dollars

router = APIRouter(prefix="/cfb", tags=["cfb"])

GAME_MARKET_TYPES = {"moneyline"}
# Season-long ladders -- no cfb_game_id, priced from the season Monte Carlo
# rather than a single game's Elo.
SEASON_MARKET_TYPES = {"win_total"}
ALL_MARKET_TYPES = GAME_MARKET_TYPES | SEASON_MARKET_TYPES


class CfbMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str
    source: str
    team: str | None
    game_label: str | None
    line: float | None
    cfb_game_id: str | None
    gameday: str | None
    gametime: str | None
    game_type: str | None
    neutral: bool
    no_baseline_reason: str | None
    implied_prob: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


def _moneyline_model_prob(m: Market, game: CfbGame) -> float | None:
    """P(this team wins). Returns None when either side is unrated rather than
    letting EloState's BASE_RATING silently price an unknown team as exactly
    league-average -- ESPN's FBS filter is not perfect and an FCS opponent would
    otherwise get a fabricated 50/50."""
    if m.team is None:
        return None
    if not (elo_service_cfb.is_rated(game.home_team) and elo_service_cfb.is_rated(game.away_team)):
        return None
    p_home = elo_service_cfb.get_home_win_prob(game.home_team, game.away_team, bool(game.neutral))
    if p_home is None:
        return None
    # CFB is the ONLY sport in this app carrying a non-identity temperature
    # (T=0.83, measured ECE 0.033 -- real under-confidence from those same talent
    # gaps). Applied on the home-perspective prob before flipping to the market's
    # side, same order as every other sport.
    p_home = calibration_temp.apply("cfb", p_home)
    return round(p_home, 4) if m.team == game.home_team else round(1 - p_home, 4)


@router.get("/markets", response_model=list[CfbMarketOut])
def list_cfb_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(
        Market.sport == "cfb", Market.market_type.in_(ALL_MARKET_TYPES)
    ).all()
    win_dist, sim_trials = season_sim_cfb.get()
    game_ids = {m.cfb_game_id for m in markets if m.cfb_game_id}
    games_by_id = {
        g.id: g for g in session.query(CfbGame).filter(CfbGame.id.in_(game_ids)).all()
    } if game_ids else {}
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _already_final(m: Market) -> bool:
        g = games_by_id.get(m.cfb_game_id) if m.cfb_game_id else None
        return g is not None and g.home_score is not None

    def _already_started(m: Market) -> bool:
        # gameday + gametime are both UTC, split from ESPN's single ISO instant,
        # so they combine directly -- no stadium-timezone round-trip.
        g = games_by_id.get(m.cfb_game_id) if m.cfb_game_id else None
        if g is None or not g.gameday or not g.gametime:
            return False
        try:
            kickoff = datetime.datetime.combine(
                datetime.date.fromisoformat(g.gameday),
                datetime.time.fromisoformat(g.gametime),
                tzinfo=datetime.timezone.utc,
            )
        except ValueError:
            return False
        return now_utc >= kickoff

    # Season ladders have no game to be final or started, so the per-game guards
    # only apply to game markets -- filtering season rows through them would drop
    # every one of them (m.cfb_game_id is always None there).
    markets = [
        m for m in markets
        if m.market_type in SEASON_MARKET_TYPES
        or (not _already_final(m) and not _already_started(m))
    ]
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    pool = get_cfb_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    out: list[CfbMarketOut] = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        game = games_by_id.get(m.cfb_game_id)
        implied = _implied_prob(snap)

        model_prob = None
        no_baseline_reason = None
        if m.market_type in SEASON_MARKET_TYPES:
            if not win_dist:
                no_baseline_reason = "Season simulation not warm yet."
            elif m.line is None or m.team not in win_dist:
                no_baseline_reason = "No season projection for this team."
            else:
                model_prob = season_sim_cfb.prob_wins_at_least(win_dist[m.team], m.line, sim_trials)
                if model_prob is not None:
                    model_prob = round(model_prob, 4)
        elif game is None:
            no_baseline_reason = "Not linked to a scheduled game yet."
        else:
            model_prob = _moneyline_model_prob(m, game)
            if model_prob is None:
                no_baseline_reason = "No baseline -- at least one team has no rating history (likely a non-FBS opponent)."

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded)
        if kelly is not None and not is_bucket_enabled(clv_stats, "cfb", m.market_type):
            kelly = None
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied,
                                           unit_dollars, flat_marginal, flat_full)

        out.append(CfbMarketOut(
            id=m.id,
            market_type=m.market_type,
            source=m.source,
            team=m.team,
            line=m.line,
            game_label=(f"{game.away_team} @ {game.home_team}" if game
                        else (f"{m.team} season wins" if m.market_type == "win_total" else None)),
            cfb_game_id=m.cfb_game_id,
            gameday=game.gameday if game else None,
            gametime=game.gametime if game else None,
            game_type=game.game_type if game else None,
            neutral=bool(game.neutral) if game else False,
            no_baseline_reason=no_baseline_reason,
            implied_prob=implied,
            last_price=snap.last_price if snap else None,
            volume=snap.volume if snap else None,
            updated_at=m.updated_at.isoformat() if m.updated_at else None,
            model_prob=model_prob,
            model_validated=False,
            edge=round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None,
            kelly_fraction=kelly,
            suggested_stake_dollars=stake_dollars,
            suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
            stake_pool="weekly" if kelly is not None else None,
        ))
    out.sort(key=lambda m: (m.gameday or "9999", m.game_label or ""))
    return out


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_cfb_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport != "cfb":
        raise HTTPException(status_code=404, detail="CFB market not found")
    game = session.get(CfbGame, m.cfb_game_id) if m.cfb_game_id else None
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    if game is not None:
        hr = elo_service_cfb.rating(game.home_team)
        ar = elo_service_cfb.rating(game.away_team)
        if hr is not None and ar is not None:
            stronger, weaker = (game.home_team, game.away_team) if hr >= ar else (game.away_team, game.home_team)
            s_r, w_r = (hr, ar) if hr >= ar else (ar, hr)
            factors.append(ReasoningFactorOut(
                label="Team strength (Elo)",
                detail=(f"{stronger} rates ahead of {weaker}, {s_r:.0f} to {w_r:.0f}. "
                        + ("Neutral site, so no home-field credit is applied."
                           if game.neutral else
                           f"{game.home_team} adds an {80:.0f}-point home-field edge.")),
            ))
        factors.append(ReasoningFactorOut(
            label="Ratings carry across seasons",
            detail=("College ratings are not regressed toward average each August -- measured, "
                    "blue-blood strength persists year to year, so last season's form still counts."),
        ))
    factors.append(ReasoningFactorOut(
        label="Not validated against the market",
        detail=("Kalshi has no settled college-football game markets, so this model has never been "
                "scored against real closing prices. Its high raw accuracy comes from lopsided "
                "matchups being easy to call, not from an edge."),
    ))
    if game is not None:
        hr = elo_service_cfb.rating(game.home_team)
        ar = elo_service_cfb.rating(game.away_team)
        if hr is not None and ar is not None:
            stronger, s_r, weaker, w_r = (
                (game.home_team, hr, game.away_team, ar) if hr >= ar
                else (game.away_team, ar, game.home_team, hr)
            )
            site = ("at a neutral site, so no home-field credit applies"
                    if game.neutral else f"with {game.home_team} adding an 80-point home-field edge")
            insight = (f"Team Elo prefers {stronger} over {weaker} ({s_r:.0f} to {w_r:.0f}), {site}. ")
        else:
            insight = "At least one side has no rating history here, so no baseline is offered. "
    else:
        insight = "This market isn't linked to a scheduled game yet. "
    insight += (
        "College ratings carry across seasons rather than being regressed each August, which is measured, "
        "not assumed. "
        + (f"From there the model parts ways with the market by {abs(edge) * 100:.1f}pp -- a candidate to "
           "look at, not a validated edge." if edge is not None
           else "There's no market price to compare it against yet.")
    )
    return ReasoningOut(
        market_id=market_id,
        market_type=m.market_type,
        label=(f"{game.away_team} @ {game.home_team}" if game else m.market_type),
        model_prob=model_prob,
        market_prob=market_prob,
        edge=edge,
        insight=insight,
        methodology=(
            "Walk-forward team Elo (K=100, home-field +80, NO season regression), derived from 4,836 FBS "
            "games 2021-2025 and confirmed on held-out 2024 and 2025 seasons: Brier 0.186 / 71.4%. K is "
            "high by pro-league standards because ~130 teams play only ~12 games each across a huge talent "
            "range. A measured temperature (T=0.83) corrects real under-confidence -- CFB is the only sport "
            "here needing one. No injury/news layer is wired."
        ),
        factors=factors,
        caveats=[
            "model_validated: false — and unlike other sports this has NEVER been scored against the "
            "market, because Kalshi has no settled college-football game markets to backtest against.",
            "The high raw accuracy reflects lopsided matchups being easy to call, not an edge.",
            "Moneyline only; spread/total series exist on Kalshi but list no markets yet.",
        ],
    )
