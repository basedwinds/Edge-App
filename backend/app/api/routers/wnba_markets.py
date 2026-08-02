"""WNBA markets API -- parallel to routers/nba_markets.py; moneyline, spread and
total are all priced.
Reuses the fully sport-agnostic `_batch_latest_snapshots`/`_implied_prob` from
routers/markets.py and the shared staking layer. model_validated is always
False (the WNBA Elo matches, doesn't beat, the market -- see elo_wnba.py).

Simpler than the NBA router in two real ways: (1) WNBA gametime is stored as a
UTC clock reading paired with the UTC date (both split from ESPN's single ISO
instant, see wnba_data.py), so the already-started guard combines them as UTC
directly -- no per-arena timezone round-trip like the NBA needs; (2) no
news/injury or futures layer is wired, so no news-blend handling here.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _implied_prob, _seeded_choice
from app.api.routers.settings import get_staking_params, get_flat_params, get_unit_dollars, get_wnba_pool_dollars
from app.api.schemas import ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.db.models import Market, WnbaGame
from app.models import calibration_temp
from app.models import game_lines_wnba
from app.models.baseline import elo_service_wnba, scoring_ratings_wnba
from app.models.baseline.elo import implied_elo_diff
from app.models.baseline.elo_wnba import HOME_COURT_ADV
from app.models.clv_selection import bucket_clv_stats, is_bucket_enabled
from app.models.staking import has_real_trading, is_weekly_market_type, kelly_fraction, suggested_stake_dollars, size_stake_dollars

router = APIRouter(prefix="/wnba", tags=["wnba"])

GAME_MARKET_TYPES = {"moneyline", "spread", "total"}
# Totals became priceable once scoring_ratings_wnba supplied real per-team points
# scored/allowed -- the old objection (a league-average model returns the same
# number for every game) no longer applies. That module was validated
# walk-forward against the naive league average before being wired in here.
NO_BASELINE_REASONS = {
    "PRE": "No baseline -- WNBA preseason lineups are a coaching decision, not a fair team-strength test.",
}


class WnbaMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str
    source: str
    team: str | None
    # Required once spread was priced (2026-08-02): the schema was moneyline-only,
    # which has no line, so a spread row reached the UI with no threshold and the
    # bet was unactionable -- you couldn't tell WHICH line you were backing.
    line: float | None
    # over/under for totals (null on moneyline/spread). Kalshi lists WNBA totals
    # only as "over" markets today, but carrying the field means an under listing
    # can't silently render as an over.
    side: str | None
    game_label: str | None
    wnba_game_id: str | None
    gameday: str | None
    gametime: str | None
    game_type: str | None
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


def _moneyline_model_prob(m: Market, game: WnbaGame) -> float | None:
    p_home = elo_service_wnba.get_home_win_prob(game.home_team, game.away_team, game.location)
    if p_home is None or m.team is None:
        return None
    # Temperature calibration (identity for WNBA -- T=1.0, measured
    # well-calibrated/noise; see calibration_temp.py). Applied on the
    # home-perspective prob before flipping to the market's team side.
    p_home = calibration_temp.apply("wnba", p_home)
    return round(p_home, 4) if m.team == game.home_team else round(1 - p_home, 4)


def _spread_model_prob(m: Market, game: WnbaGame) -> float | None:
    """P(this team wins by more than its line), from the WNBA Elo turned into an
    expected margin. Uses game_lines_wnba, NOT game_lines_nba: WNBA games average
    174 points to the NBA's 218 and carry a wider margin spread (14.3 vs 13.2), so
    the NBA constants would misprice every line."""
    if m.team is None or m.line is None:
        return None
    p_home = elo_service_wnba.get_home_win_prob(game.home_team, game.away_team, game.location)
    if p_home is None:
        return None
    p_home = calibration_temp.apply("wnba", p_home)
    elo_diff = implied_elo_diff(p_home)
    return round(game_lines_wnba.prob_team_covers(m.team == game.home_team, m.line, elo_diff), 4)


def _total_model_prob(m: Market, game: WnbaGame, scoring: dict) -> float | None:
    """P(combined score clears the line). Uses per-team scoring rates where both
    teams clear scoring_ratings_wnba.MIN_GAMES; below that expected_total falls
    back to the league average, which the walk-forward validation showed is the
    BETTER prior early in a season, not merely a safe one."""
    if m.line is None:
        return None
    p = game_lines_wnba.prob_over(m.line, scoring.get(game.home_team), scoring.get(game.away_team))
    # Kalshi lists WNBA totals as "over" markets; honour an "under" if one appears
    # rather than pricing it as its own opposite.
    if (m.side or "over").lower() == "under":
        p = 1.0 - p
    return round(p, 4)


@router.get("/markets", response_model=list[WnbaMarketOut])
def list_wnba_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "wnba", Market.market_type.in_(GAME_MARKET_TYPES)).all()
    # Computed ONCE per request, not per market: there are ~60 total markets per
    # slate and this walks every finished game in the season.
    scoring = scoring_ratings_wnba.compute_current_scoring_ratings()
    game_ids = {m.wnba_game_id for m in markets if m.wnba_game_id}
    games_by_id = {g.id: g for g in session.query(WnbaGame).filter(WnbaGame.id.in_(game_ids)).all()} if game_ids else {}
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _game_already_final(m: Market) -> bool:
        game = games_by_id.get(m.wnba_game_id) if m.wnba_game_id else None
        return game is not None and game.home_score is not None

    def _game_already_started(m: Market) -> bool:
        # gameday + gametime are BOTH the UTC date/time from ESPN's single ISO
        # instant, so combine directly as UTC (no arena-timezone round-trip).
        game = games_by_id.get(m.wnba_game_id) if m.wnba_game_id else None
        if game is None or not game.gameday or not game.gametime:
            return False
        try:
            kickoff = datetime.datetime.combine(
                datetime.date.fromisoformat(game.gameday),
                datetime.time.fromisoformat(game.gametime),
                tzinfo=datetime.timezone.utc,
            )
        except ValueError:
            return False
        return now_utc >= kickoff

    markets = [m for m in markets if not _game_already_final(m) and not _game_already_started(m)]
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    weekly_pool, _futures_pool = get_wnba_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    # CLV-driven selection (reference wiring; other sports follow the same
    # one-call pattern). Inert until the "wnba"/"moneyline" bucket reaches the
    # min sample -- until then every row stays enabled. See clv_selection.py.
    clv_stats = bucket_clv_stats(session)

    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        game = games_by_id.get(m.wnba_game_id)
        implied = _implied_prob(snap)

        model_prob = None
        no_baseline_reason = None
        if game is not None:
            no_baseline_reason = NO_BASELINE_REASONS.get(game.game_type)
            if no_baseline_reason is None and m.market_type == "moneyline":
                model_prob = _moneyline_model_prob(m, game)
            elif no_baseline_reason is None and m.market_type == "spread":
                model_prob = _spread_model_prob(m, game)
            elif no_baseline_reason is None and m.market_type == "total":
                model_prob = _total_model_prob(m, game, scoring)

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded)
        # Suppress the whole bucket if forward CLV says it doesn't work (no-op
        # until the bucket is well-sampled -- see clv_selection.py).
        if kelly is not None and not is_bucket_enabled(clv_stats, "wnba", m.market_type):
            kelly = None
        stake_dollars = size_stake_dollars(staking_mode, kelly, weekly_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full)

        out.append(
            WnbaMarketOut(
                id=m.id,
                market_type=m.market_type,
                line=m.line,
                side=m.side,
                source=m.source,
                team=m.team,
                game_label=f"{game.away_team} @ {game.home_team}" if game else None,
                wnba_game_id=m.wnba_game_id,
                gameday=game.gameday if game else None,
                gametime=game.gametime if game else None,
                game_type=game.game_type if game else None,
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
            )
        )
    out.sort(key=lambda m: (m.gameday or "9999", m.game_label or ""))
    return out


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_wnba_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport != "wnba":
        raise HTTPException(status_code=404, detail="WNBA market not found")
    game = session.get(WnbaGame, m.wnba_game_id) if m.wnba_game_id else None
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    if game is not None:
        hr = elo_service_wnba.get_team_rating(game.home_team)
        ar = elo_service_wnba.get_team_rating(game.away_team)
        if hr is not None and ar is not None:
            factors.append(ReasoningFactorOut(
                label="Team Elo",
                detail=f"{game.home_team} {hr:.0f} vs {game.away_team} {ar:.0f} (gap {hr - ar:+.0f})."))
        factors.append(ReasoningFactorOut(
            label="Home court",
            detail=("Neutral site — no home-court credit." if game.location == "Neutral"
                    else f"{game.home_team} gets a +{HOME_COURT_ADV:.0f} Elo home-court boost (measured from a 54.3% home win rate).")))

    insight = ""
    if game is not None:
        hr = elo_service_wnba.get_team_rating(game.home_team)
        ar = elo_service_wnba.get_team_rating(game.away_team)
        if hr is not None and ar is not None:
            gap = hr - ar
            wseed = f"{game.home_team}|{game.away_team}|{hr}|{ar}"
            hca_note = ("and on a neutral floor home court is off the table"
                        if game.location == "Neutral"
                        else f"with {game.home_team} adding a {HOME_COURT_ADV:.0f}-point home-court bump")
            if abs(gap) < 25:
                insight = _seeded_choice(wseed, [
                    f"This one projects tight -- team Elo has {game.home_team} and {game.away_team} nearly level ({hr:.0f} to {ar:.0f}), {hca_note}. ",
                    f"There's little between these two on the ratings ({hr:.0f} to {ar:.0f}), {hca_note}. ",
                    f"About as even as it gets: {game.home_team} and {game.away_team} sit close ({hr:.0f} to {ar:.0f}), {hca_note}. ",
                ])
            else:
                stronger, s_r, weaker, w_r = (game.home_team, hr, game.away_team, ar) if gap > 0 else (game.away_team, ar, game.home_team, hr)
                insight = _seeded_choice(wseed, [
                    f"{stronger} comes in as the side team Elo prefers, clear of {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}. ",
                    f"The ratings lean {stronger} here, well ahead of {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}. ",
                    f"Team Elo gives the edge to {stronger}, above {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}. ",
                ])
    insight += (
        "It's a pure Elo read with no situational layer wired for WNBA yet, so the rating is the whole story. "
        + ("From there the model parts ways with the market by "
           f"{abs(edge) * 100:.1f}pp -- a candidate to look at, not a validated edge." if edge is not None
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
            "Walk-forward team Elo (K=32, home-court +30, 1/3 season regression), fit on 1,540 ESPN "
            "games 2021-2026. Brier 0.222 / 65.4% in backtest; the market beats it by ~0.008 Brier "
            "(model_validated: false). No injury/rest/news layer is wired for WNBA yet."
        ),
        factors=factors,
        caveats=[
            "model_validated: false — the WNBA Elo matches, it does not beat, the market on average.",
            "Moneyline + spread (Elo-implied margin, WNBA-specific constants); totals/futures not modeled.",
        ],
    )
