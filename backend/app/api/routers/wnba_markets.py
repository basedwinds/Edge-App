"""WNBA markets API -- parallel to routers/nba_markets.py; moneyline, spread and
total are all priced.
Reuses the fully sport-agnostic `_batch_latest_snapshots`/`_implied_prob` from
routers/markets.py and the shared staking layer. model_validated is always
False (the WNBA Elo matches, doesn't beat, the market -- see elo_wnba.py).

Simpler than the NBA router in two real ways: (1) WNBA gametime is stored as a
UTC clock reading paired with the UTC date (both split from ESPN's single ISO
instant, see wnba_data.py), so the already-started guard combines them as UTC
directly -- no per-arena timezone round-trip like the NBA needs; (2) no
futures layer is wired. An INJURY/availability layer now is (see
injury_rules_wnba.py, calibrated on 602 real box scores); the rest/schedule-spot
half was measured and rejected -- scripts/backtest_wnba_rest.py.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _implied_prob, _seeded_choice
from app.api.routers.settings import get_staking_params, get_flat_params, get_unit_dollars, get_wnba_pool_dollars
from app.api.schemas import FuturesMarketOut, ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.db.models import Market, WnbaGame
from app.ingestion.market_catalog_wnba import wnba_news_cache_to_pydantic
from app.models import calibration_temp
from app.models.combine import combine_probability
from app.models.news_adjustment.schema import NewsAdjustment
from app.models import game_lines_wnba
from app.models.baseline import elo_service_wnba, scoring_ratings_wnba
from app.models.baseline.elo import implied_elo_diff
from app.models import season_sim_wnba
from app.models.baseline.elo_wnba import HOME_COURT_ADV
from app.models.clv_selection import bucket_clv_stats, gate_kelly, is_bucket_enabled
from app.models.staking import FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, is_weekly_market_type, kelly_fraction, suggested_stake_dollars, size_stake_dollars

router = APIRouter(prefix="/wnba", tags=["wnba"])

GAME_MARKET_TYPES = {
    "moneyline", "spread", "total",
    # Halves are namespaced by half rather than sharing the game types: a 1H
    # spread is a different distribution from a game spread (margin std 10.39 vs
    # 14.21), and the CLV gate + calibration report both bucket by market_type,
    # so lumping them would average two things that measurably differ.
    "first_half_winner", "first_half_spread", "first_half_total",
    "second_half_winner", "second_half_spread", "second_half_total",
}
# Season-long ladders (KXWNBAWINS). These have NO wnba_game_id, so they must
# bypass the per-game final/started guards below -- running them through those
# would silently drop every season row (the trap that would have dropped all of
# CFB's season markets). They also draw on the futures sub-pool, not the weekly
# one, at the reduced futures unit size.
SEASON_MARKET_TYPES = {"win_total", "one_seed", "playoff_qualifier"}
ALL_MARKET_TYPES = GAME_MARKET_TYPES | SEASON_MARKET_TYPES
_HALF_OF = {"first": 1, "second": 2}
# Kalshi outcome labels that are NOT a team. A half can end level, so the winner
# markets carry a TIE leg -- see _half_model_prob for the bug this prevents.
_NON_TEAM_OUTCOMES = {"TIE", "DRAW"}
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


def _batch_news_adjustments(session: Session, game_ids: set[str]) -> dict[str, NewsAdjustment]:
    """Cached availability adjustments for a slate, one query for the lot."""
    if not game_ids:
        return {}
    from app.db.models import WnbaNewsAdjustmentCache

    rows = (
        session.query(WnbaNewsAdjustmentCache)
        .filter(WnbaNewsAdjustmentCache.wnba_game_id.in_(game_ids))
        .all()
    )
    return {r.wnba_game_id: wnba_news_cache_to_pydantic(r) for r in rows}


def _moneyline_model_prob(m: Market, game: WnbaGame, news: NewsAdjustment | None = None) -> float | None:
    p_home = elo_service_wnba.get_home_win_prob(game.home_team, game.away_team, game.location)
    if p_home is None or m.team is None:
        return None
    # Temperature calibration (identity for WNBA -- T=1.0, measured
    # well-calibrated/noise; see calibration_temp.py). Applied on the
    # home-perspective prob before flipping to the market's team side.
    p_home = calibration_temp.apply("wnba", p_home)
    # Availability blend, applied on the HOME-perspective probability before
    # the side flip -- same order every other sport uses, so an away-team row
    # gets the exact mirror rather than a separately-derived number.
    # is_divisional is False: the WNBA has no divisional-squeeze effect
    # measured for it, same posture as the NBA router.
    p_home = combine_probability(p_home, news, is_divisional=False)
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


def _half_model_prob(m: Market, game: WnbaGame, scoring: dict) -> float | None:
    """1H/2H winner, spread and total. The half constants are MEASURED (see
    game_lines_wnba) -- in particular the second half carries NO home-court
    edge, which the game model cannot express and which scaling the game model
    by a share would have got wrong in the home team's favour."""
    prefix, kind = m.market_type.split("_half_")
    half = _HALF_OF[prefix]
    if kind == "total":
        if m.line is None:
            return None
        p = game_lines_wnba.prob_over_half(m.line, scoring.get(game.home_team),
                                           scoring.get(game.away_team), half)
        if (m.side or "over").lower() == "under":
            p = 1.0 - p
        return round(p, 4)
    if m.team is None:
        return None
    # A HALF CAN END TIED, and Kalshi lists TIE as its own outcome on the winner
    # markets. Left unhandled this was a real, money-losing bug: "TIE" is not the
    # home team, so it fell through to the AWAY side's win probability -- 20.1%
    # against a market at 3.5%, a fake +16.5pp edge that staked a full unit.
    # The margin model is continuous and has no point mass at exactly zero, so it
    # genuinely cannot price a tie; these are left unpriced rather than
    # approximated.
    if m.team.upper() in _NON_TEAM_OUTCOMES:
        return None
    p_home = elo_service_wnba.get_home_win_prob(game.home_team, game.away_team, game.location)
    if p_home is None:
        return None
    p_home = calibration_temp.apply("wnba", p_home)
    elo_diff = implied_elo_diff(p_home)
    is_home = m.team == game.home_team
    # Guard against any other non-team label reaching here: a row whose team
    # matches NEITHER side must not be silently priced as the away team.
    if not is_home and m.team != game.away_team:
        return None
    if kind == "winner":
        return round(game_lines_wnba.prob_team_wins_half(is_home, elo_diff, half), 4)
    if kind == "spread" and m.line is not None:
        return round(game_lines_wnba.prob_team_covers_half(is_home, m.line, elo_diff, half), 4)
    return None


def _wnba_season_model_prob(m, win_dist, sim_trials):
    """Model probability for a season-long WNBA market. Shared by /markets and
    /futures so the two can never disagree about the same row."""
    # #1 seed / playoff qualifier resolve on the regular-season table, not a
    # bracket, so they come off the SAME simulation as the win ladders (see
    # season_sim_wnba.standings_probs). KXWNBACHAMP -- the market that would
    # genuinely need a playoff bracket -- has no open markets, so none is built.
    #
    # Checked BEFORE the win_dist guard and read from the cache, not recomputed:
    # standings_probs() would re-run the whole season sim on every request, and
    # gating on win_dist would leave these unpriced whenever the ladder cache is
    # cold even though the standings are right there.
    if m.market_type in ("one_seed", "playoff_qualifier"):
        standings = season_sim_wnba.get_standings()
        if not standings:
            return None, "Season simulation not warm yet."
        row = standings.get(m.team)
        if row is None:
            return None, "No season projection for this team."
        key = "one_seed" if m.market_type == "one_seed" else "playoff"
        return round(row[key], 4), None
    if not win_dist:
        return None, "Season simulation not warm yet."
    if m.line is None or m.team not in win_dist:
        return None, "No season projection for this team."
    p = season_sim_wnba.prob_wins_at_least(win_dist[m.team], m.line, sim_trials)
    return (round(p, 4) if p is not None else None), None


@router.get("/markets", response_model=list[WnbaMarketOut])
def list_wnba_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "wnba", Market.market_type.in_(ALL_MARKET_TYPES), Market.status == "active").all()
    # Computed ONCE per request, not per market: there are ~60 total markets per
    # slate and this walks every finished game in the season.
    scoring = scoring_ratings_wnba.compute_current_scoring_ratings()
    game_ids = {m.wnba_game_id for m in markets if m.wnba_game_id}
    games_by_id = {g.id: g for g in session.query(WnbaGame).filter(WnbaGame.id.in_(game_ids)).all()} if game_ids else {}
    # One query for the whole slate's availability adjustments, not one per
    # market -- the same batching every other sport's router uses.
    news_by_game = _batch_news_adjustments(session, game_ids)
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

    markets = [
        m for m in markets
        if m.market_type in SEASON_MARKET_TYPES
        or (not _game_already_final(m) and not _game_already_started(m))
    ]
    win_dist, sim_trials = season_sim_wnba.get()
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    weekly_pool, futures_pool = get_wnba_pool_dollars(session)
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
        if m.market_type == "win_total":
            model_prob, no_baseline_reason = _wnba_season_model_prob(m, win_dist, sim_trials)
        elif game is not None:
            no_baseline_reason = NO_BASELINE_REASONS.get(game.game_type)
            if no_baseline_reason is None and m.market_type == "moneyline":
                model_prob = _moneyline_model_prob(m, game, news_by_game.get(m.wnba_game_id))
            elif no_baseline_reason is None and m.market_type == "spread":
                model_prob = _spread_model_prob(m, game)
            elif no_baseline_reason is None and m.market_type == "total":
                model_prob = _total_model_prob(m, game, scoring)
            elif no_baseline_reason is None and "_half_" in m.market_type:
                model_prob = _half_model_prob(m, game, scoring)

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None)
        # Suppress the whole bucket if forward CLV says it doesn't work (no-op
        # until the bucket is well-sampled -- see clv_selection.py).
        if kelly is not None and not is_bucket_enabled(clv_stats, "wnba", m.market_type):
            kelly = None
        _is_futures = m.market_type in SEASON_MARKET_TYPES
        stake_dollars = size_stake_dollars(
            staking_mode, kelly, futures_pool if _is_futures else weekly_pool,
            model_prob, implied, unit_dollars, flat_marginal, flat_full,
            FUTURES_UNIT_SCALE if _is_futures else 1.0,
        )

        out.append(
            WnbaMarketOut(
                id=m.id,
                market_type=m.market_type,
                line=m.line,
                side=m.side,
                source=m.source,
                team=m.team,
                game_label=(f"{game.away_team} @ {game.home_team}" if game
                            else (f"{m.team} season wins" if m.market_type == "win_total" else None)),
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
                stake_pool=(("futures" if _is_futures else "weekly") if kelly is not None else None),
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
            "(model_validated: false). Availability/injury adjustments are applied; a rest "
            "adjustment was measured for the WNBA and rejected as noise."
        ),
        factors=factors,
        caveats=[
            "model_validated: false — the WNBA Elo matches, it does not beat, the market on average.",
            "Moneyline + spread (Elo-implied margin, WNBA-specific constants); totals/futures not modeled.",
        ],
    )

@router.get("/futures", response_model=list[FuturesMarketOut])
def list_wnba_futures(session: Session = Depends(get_session)):
    """WNBA's season-long markets, in the same shape every other sport serves.

    WNBA and the other one of this pair were the ONLY sports without a
    /futures route, so their win-total and season ladders had nowhere to go and
    rode along in the GAME feed -- 78 such rows, showing up beside tonight's
    fixtures. Prices come from the same helper /markets uses, so the two views
    cannot disagree about a row.
    """
    markets = (
        session.query(Market)
        .filter(Market.sport == "wnba", Market.market_type.in_(SEASON_MARKET_TYPES), Market.status == "active")
        .all()
    )
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    win_dist, sim_trials = season_sim_wnba.get()
    _weekly_pool, futures_pool = get_wnba_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)
    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        model_prob, _reason = _wnba_season_model_prob(m, win_dist, sim_trials)
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(
            kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction,
                           min_edge_to_bet, has_traded, snap.yes_ask if snap else None),
            clv_stats, "wnba", m.market_type,
        )
        stake_dollars = size_stake_dollars(staking_mode, kelly, futures_pool, model_prob, implied,
                                           unit_dollars, flat_marginal, flat_full,
                                           unit_scale=FUTURES_UNIT_SCALE, min_market_price=FUTURES_MIN_MARKET_PRICE)
        out.append(FuturesMarketOut(
            id=m.id, market_type=m.market_type, source=m.source, team=m.team,
            group_label=m.group_label, line=m.line, side=m.side,
            implied_prob=implied,
            yes_bid=snap.yes_bid if snap else None,
            yes_ask=snap.yes_ask if snap else None,
            last_price=snap.last_price if snap else None,
            volume=snap.volume if snap else None,
            updated_at=m.updated_at.isoformat() if m.updated_at else None,
            model_prob=model_prob, model_validated=False,
            edge=round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None,
            kelly_fraction=kelly,
            suggested_stake_dollars=stake_dollars,
            suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
            stake_pool="futures" if kelly is not None else None,
            line_move_pp=None,
        ))
    out.sort(key=lambda r: (r.market_type, -(r.implied_prob or 0)))
    return out
