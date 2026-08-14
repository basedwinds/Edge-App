"""College-football markets API -- parallel to routers/wnba_markets.py.

Moneyline and SPREAD. The spread was added 2026-08-06 once a CFB margin model
existed to price it: game_lines_cfb, fitted on 4,836 games (2021-25) with a
slope 3.3x NFL's and a 47% wider spread, stable across held-out seasons. Before
that this file said "moneyline only, on purpose" because adding a spread would
have meant shipping constants nothing measured -- which was right at the time.

TOTALS ARE STILL ABSENT, and now for a measured reason rather than an absent
one: a per-team CFB scoring model was walk-forward tested and beat the running
league average by only 4.8% (13.01 vs 13.66 mean absolute error). Too thin to
stake, so KXNCAAFTOTAL/KXNCAAFTEAMTOTAL stay unpriced.

model_validated is always False, and for CFB that matters more than usual: the
Elo's ~0.186 Brier / 71% accuracy looks far stronger than WNBA's 0.222 / 65.4%,
but that reflects college football's enormous talent gaps making many games
near-certain -- NOT an edge over the market. Kalshi has no settled KXNCAAFGAME
markets, so there are no closing prices to backtest edge against at all.
"""
import datetime

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _implied_prob
from app.api.routers.settings import get_staking_params, get_flat_params, get_unit_dollars, get_cfb_pool_dollars
from app.api.schemas import FuturesMarketOut, ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.db.models import CfbGame, Market
from app.models import calibration_temp
from app.models import game_lines_cfb, playoff_sim_cfb, season_sim_cfb
from app.models.baseline import elo_service_cfb
from app.models.clv_selection import bucket_clv_stats, gate_kelly, is_bucket_enabled
from app.models.staking import FUTURES_MAX_SPREAD, FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, apply_nested_futures_cap, has_real_trading, kelly_fraction, size_stake_dollars

router = APIRouter(prefix="/cfb", tags=["cfb"])

log = logging.getLogger("cfb_markets")

# Shown on rows whose team's Elo was earned outside the FBS pool -- see
# elo_service_cfb.MIN_FBS_CONNECTIVITY for why the rating is not comparable and
# why no numerical correction is applied.
WEAK_POOL_NOTE = "rating built outside FBS play - tracking only"

# Shown on every playoff/bracket futures row. Deliberately states the direction
# of the error, not just that one exists: the bias is systematic and always the
# same way, so "approximate" alone would understate it.
BRACKET_APPROXIMATE_NOTE = (
    "Approximate. The playoff field is seeded by a PROXY for the selection committee "
    "(simulated wins, Elo breaking ties), and each further round compounds this model's "
    "deliberately wide rating spread -- so the strongest team is over-concentrated and its "
    "apparent edge is largest exactly where the model is least trustworthy. Directionally "
    "useful; not a validated price."
)



# Bracket rounds Polymarket lists and Kalshi does not (added 2026-08-07 with the
# Polymarket CFB pipeline). All four read straight off playoff_sim_cfb, which
# already simulated the bracket to a champion -- "title" in particular was being
# computed and discarded every run because no Kalshi market asked for it.
POLYMARKET_BRACKET_MARKET_TYPES = {
    "cfb_national_champion", "cfb_finalist", "cfb_semifinal", "cfb_top4_seed",
}
_BRACKET_SIM_KEY = {
    "cfb_playoff": "playoff",
    "cfb_quarterfinal": "quarterfinal",
    "cfb_semifinal": "semifinal",
    "cfb_finalist": "finalist",
    "cfb_top4_seed": "top4_seed",
    "cfb_national_champion": "title",
}

GAME_MARKET_TYPES = {"moneyline", "spread"}
# Season-long ladders -- no cfb_game_id, priced from the season Monte Carlo
# rather than a single game's Elo.
SEASON_MARKET_TYPES = {
    "win_total", "conference_champion", "conference_qualifier", "conference_regtop",
    "cfb_playoff", "cfb_quarterfinal", "cfb_title_conference",
    *POLYMARKET_BRACKET_MARKET_TYPES,
}
# Kalshi's short conference codes -> the ESPN conference names playoff_sim_cfb
# keys on. "OTHER" is everything else and is computed as the remainder.
# Types whose model rests on a PROXY rather than on schedule/standings: the
# playoff markets seed off a stand-in for the selection committee, and a
# four-round bracket compounds elo_cfb's wide rating spread (the top team came
# out at 40.5% to win the title where books top out near 15-20%).
#
# These are BADGED, NOT SUPPRESSED -- and that reversal is deliberate. Making
# them tracking-only looked prudent, but the paper logger only records rows the
# app actually staked (paper_logger gates on suggested_stake_dollars, and
# recommended.py's pass 1 is "rows the app actually staked"). So suppressing them
# meant they would never become paper bets, never accrue forward CLV, and never
# be evaluated -- guaranteeing we could never learn whether the approximation
# works. That directly contradicts this app's whole premise, which is that no
# model is trusted on theory and only forward CLV decides.
#
# The honest design is: stake them like anything else, flag them clearly in the
# UI, and let the CLV-selection gate retire them if the data says so. That gate
# exists precisely for this.
# The new Polymarket bracket rounds come from the SAME simulation and inherit
# the same proxy, so they carry the same badge -- not badging them would imply
# the national-champion number is better founded than the quarterfinal one when
# it is strictly more compounded.
APPROXIMATE_MARKET_TYPES = {"cfb_playoff", "cfb_quarterfinal", "cfb_title_conference",
                            *POLYMARKET_BRACKET_MARKET_TYPES}

# Season-long types draw from the futures sub-pool; moneyline from the weekly one.
FUTURES_MARKET_TYPES = {
    "win_total", "conference_champion", "conference_qualifier", "conference_regtop",
    "cfb_playoff", "cfb_quarterfinal", "cfb_title_conference",
    *POLYMARKET_BRACKET_MARKET_TYPES,
}

_KALSHI_CONF_CODE = {
    "SEC": "Southeastern Conference",
    "B12": "Big 12 Conference",
    "B10": "Big Ten Conference",
    "ACC": "Atlantic Coast Conference",
}
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
    # True where the model rests on a proxy (committee seeding). These ARE
    # staked -- the badge is a warning, not a suppression; see
    # APPROXIMATE_MARKET_TYPES for why suppressing them was wrong.
    model_approximate: bool
    edge: float | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


def _spread_model_prob(m: Market, game: CfbGame) -> float | None:
    """P(this team wins by MORE than m.line), from game_lines_cfb.

    Unrated teams are refused for the same reason the moneyline refuses them --
    an unrated side would otherwise be priced at the base rating, fabricating a
    line-cover probability out of a team we know nothing about.

    NO TEMPERATURE HERE, deliberately. calibration_temp's T=1.26 was fitted on
    CFB moneyline WIN probabilities; it is a correction to that model's
    over-confidence, not a general CFB constant. The margin model has its own
    fitted spread (MARGIN_STD 19.82, stable out of sample), so applying a
    temperature fitted for a different quantity would be double-correcting with
    a number that was never measured against margins.
    """
    if m.team is None or m.line is None:
        return None
    if not (elo_service_cfb.is_rated(game.home_team) and elo_service_cfb.is_rated(game.away_team)):
        return None
    home_r = elo_service_cfb.rating(game.home_team)
    away_r = elo_service_cfb.rating(game.away_team)
    if home_r is None or away_r is None:
        return None
    elo_diff = game_lines_cfb.elo_diff_for(home_r, away_r, neutral=bool(game.neutral))
    return round(game_lines_cfb.prob_team_covers(m.team == game.home_team, m.line, elo_diff), 4)


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
    # (T=1.26, measured ECE 0.033 -- real OVER-confidence from those same talent
    # gaps). Applied on the home-perspective prob before flipping to the market's
    # side, same order as every other sport.
    p_home = calibration_temp.apply("cfb", p_home)
    return round(p_home, 4) if m.team == game.home_team else round(1 - p_home, 4)


def _cfb_season_model_prob(m, win_dist, sim_trials, po_sim, conf_sim):
    """Model probability for any season-long CFB market -- win totals, the
    playoff family, and the conference ladders. Shared by /markets and
    /futures so the two can never disagree about the same row."""
    model_prob = None
    no_baseline_reason = None
    if m.market_type == "win_total":
        if not win_dist:
            no_baseline_reason = "Season simulation not warm yet."
        elif m.line is None or m.team not in win_dist:
            no_baseline_reason = "No season projection for this team."
        else:
            model_prob = season_sim_cfb.prob_wins_at_least(win_dist[m.team], m.line, sim_trials)
            if model_prob is not None:
                model_prob = round(model_prob, 4)
    elif m.market_type in _BRACKET_SIM_KEY or m.market_type == "cfb_title_conference":
        if not po_sim:
            no_baseline_reason = "Playoff simulation not warm yet."
        elif m.market_type == "cfb_title_conference":
            tbc = po_sim.get("title_by_conference") or {}
            name = _KALSHI_CONF_CODE.get((m.team or "").upper())
            if name is not None:
                model_prob = round(tbc.get(name, 0.0), 4)
            elif (m.team or "").upper() == "OTHER":
                # Everything outside the four named conferences, including
                # independents -- the remainder, so the five markets sum to 1.
                named = sum(tbc.get(v, 0.0) for v in _KALSHI_CONF_CODE.values())
                model_prob = round(max(0.0, 1.0 - named), 4)
            else:
                no_baseline_reason = "Unmapped conference code."
        else:
            key = _BRACKET_SIM_KEY[m.market_type]
            src = po_sim.get(key) or {}
            if m.team in src:
                model_prob = round(src[m.team], 4)
            else:
                no_baseline_reason = "No playoff projection for this team."
    elif m.market_type in SEASON_MARKET_TYPES:
        if not conf_sim:
            no_baseline_reason = "Conference simulation not warm yet."
        else:
            if m.market_type == "conference_champion":
                src = conf_sim.get("champion") or {}
            elif m.market_type == "conference_qualifier":
                # Reaching a conference title game IS finishing top two.
                src = (conf_sim.get("top_n") or {}).get(2) or {}
            else:
                # Regular-season "top N", depth carried on m.line.
                src = (conf_sim.get("top_n") or {}).get(int(m.line)) if m.line else {}
                src = src or {}
            if m.team in src:
                model_prob = round(src[m.team], 4)
            else:
                no_baseline_reason = "No conference projection for this team."
    return model_prob, no_baseline_reason


@router.get("/markets", response_model=list[CfbMarketOut])
def list_cfb_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(
        Market.sport == "cfb", Market.market_type.in_(ALL_MARKET_TYPES), Market.status == "active"
    ).all()
    win_dist, sim_trials = season_sim_cfb.get()
    from app.ingestion.poller_cfb import _CONF_SIM
    conf_sim = _CONF_SIM.get("data") or {}
    po_sim = playoff_sim_cfb.get() or {}
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
    weekly_pool, futures_pool = get_cfb_pool_dollars(session)
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
        if m.market_type in SEASON_MARKET_TYPES or m.market_type in ("cfb_playoff", "cfb_quarterfinal", "cfb_title_conference"):
            model_prob, no_baseline_reason = _cfb_season_model_prob(m, win_dist, sim_trials, po_sim, conf_sim)
        elif game is None:
            no_baseline_reason = "Not linked to a scheduled game yet."
        elif m.market_type == "spread":
            model_prob = _spread_model_prob(m, game)
            if model_prob is None:
                no_baseline_reason = "No baseline -- at least one team has no rating history (likely a non-FBS opponent)."
        else:
            model_prob = _moneyline_model_prob(m, game)
            if model_prob is None:
                no_baseline_reason = "No baseline -- at least one team has no rating history (likely a non-FBS opponent)."

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None)
        if kelly is not None and not is_bucket_enabled(clv_stats, "cfb", m.market_type):
            kelly = None
        pool = futures_pool if m.market_type in FUTURES_MARKET_TYPES else weekly_pool
        _uscale = FUTURES_UNIT_SCALE if pool is futures_pool else 1.0
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied,
                                           unit_dollars, flat_marginal, flat_full, unit_scale=_uscale, sport="cfb", team=m.team)
        # SAME GATE THE FUTURES BLOCK ALREADY APPLIES, and it belonged here just
        # as much. A team whose rating was built almost entirely outside the FBS
        # pool is measured on a different scale from the opponent it is priced
        # against, so the elo_diff -- and every spread probability derived from
        # it -- is not meaningful.
        #
        # It was wired for futures only, which is the smaller half of the
        # exposure: futures stake $2 a leg, game markets stake $10. Found by
        # auditing the top of the board by EDGE rather than by count, which is
        # how this app is actually bet. NDSU (FBS connectivity 0.081) is rated
        # 1850 -- level with TCU's 1852 -- because it earns that rating against
        # FCS opposition, and it was carrying three of the highest edges on the
        # entire board: spread +49.4pp, +42.1pp and +24.8pp, at $10 each.
        # Sacramento State (0.097) added a fourth at +23.0pp.
        #
        # For scale on how wrong the derived line gets: TCU 1852.6 vs UNC 1452.3
        # is a 400-point gap, which MARGIN_SLOPE turns into an expected margin of
        # ~34 points and P(cover 7.5) = 0.92. The market has that game near a
        # touchdown.
        #
        # Zeroed AFTER sizing, deliberately, so the model number and its edge
        # still surface for tracking -- same posture as the futures block and the
        # player-stat projections. Shown, never staked.
        if m.team is not None and elo_service_cfb.is_weakly_connected(m.team):
            kelly = None
            stake_dollars = None

        out.append(CfbMarketOut(
            id=m.id,
            market_type=m.market_type,
            source=m.source,
            team=m.team,
            line=m.line,
            game_label=(f"{game.away_team} @ {game.home_team}" if game
                        else (f"{m.team} season wins" if m.market_type == "win_total"
                              else (f"{m.team} {m.market_type.replace('conference_', 'conf ')}"
                                    if m.market_type in SEASON_MARKET_TYPES else None))),
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
            stake_pool=(("futures" if m.market_type in FUTURES_MARKET_TYPES else "weekly")
                        if kelly is not None else None),
            model_approximate=m.market_type in APPROXIMATE_MARKET_TYPES,
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
            "range. A measured temperature (T=1.26) corrects real OVER-confidence -- CFB is the only sport "
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

@router.get("/futures", response_model=list[FuturesMarketOut])
def list_cfb_futures(session: Session = Depends(get_session)):
    """CFB's season-long markets, in the same shape every other sport serves.

    CFB and the other one of this pair were the ONLY sports without a
    /futures route, so their win-total and playoff/conference ladders had nowhere to go and
    rode along in the GAME feed -- 78 such rows, showing up beside tonight's
    fixtures. Prices come from the same helper /markets uses, so the two views
    cannot disagree about a row.
    """
    markets = (
        session.query(Market)
        .filter(Market.sport == "cfb", Market.market_type.in_(SEASON_MARKET_TYPES | {"cfb_playoff", "cfb_quarterfinal", "cfb_title_conference"}), Market.status == "active")
        .all()
    )
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    # Exactly the sources /markets uses -- conf_sim comes from the module-level
    # _CONF_SIM cache the warmer fills, NOT from a season_sim_cfb function (I
    # reached for a get_conference_sim() that does not exist and it 500'd).
    from app.ingestion.poller_cfb import _CONF_SIM   # local import, exactly as /markets does

    win_dist, sim_trials = season_sim_cfb.get()
    po_sim = playoff_sim_cfb.get() or {}
    conf_sim = _CONF_SIM.get("data") or {}
    _weekly_pool, futures_pool = get_cfb_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)
    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        model_prob, _reason = _cfb_season_model_prob(m, win_dist, sim_trials, po_sim, conf_sim)
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(
            kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction,
                           min_edge_to_bet, has_traded, snap.yes_ask if snap else None),
            clv_stats, "cfb", m.market_type,
        )
        stake_dollars = size_stake_dollars(staking_mode, kelly, futures_pool, model_prob, implied,
                                           unit_dollars, flat_marginal, flat_full,
                                           unit_scale=FUTURES_UNIT_SCALE, min_market_price=FUTURES_MIN_MARKET_PRICE, max_spread=FUTURES_MAX_SPREAD, yes_bid=snap.yes_bid if snap else None, yes_ask=snap.yes_ask if snap else None, sport="cfb", team=m.team)
        # A team whose rating was built almost entirely outside the FBS pool is
        # priced on a scale the rest of this market is not on. Shown with its
        # model number so it can be tracked, never staked -- same posture as the
        # player-stat projections. Zeroed AFTER sizing so the edge still surfaces.
        weak_pool = m.team is not None and elo_service_cfb.is_weakly_connected(m.team)
        if weak_pool:
            kelly = None
            stake_dollars = None
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
            # THE APPROXIMATE BADGE HAS TO BE ON THE FUTURES PAYLOAD TOO.
            #
            # /cfb/markets sets model_approximate for these same market types,
            # but this route only ever set the weak-pool note -- so on the
            # Futures pages and in the cross-sport list every bracket row looked
            # like an ordinary, fully-founded model number. They are not: the
            # bracket seeds off a PROXY for the selection committee, and four
            # rounds compound elo_cfb's deliberately wide K=100 spread (see
            # playoff_sim_cfb's docstring). That badge is the entire reason
            # these are allowed to be staked rather than suppressed, and it was
            # missing exactly where the staking decision is displayed.
            #
            # Live when this was found: Indiana staked on FOUR nested bracket
            # markets at +21.8 to +44.8pp, each showing no caveat at all.
            model_note=(
                WEAK_POOL_NOTE if weak_pool
                else BRACKET_APPROXIMATE_NOTE if m.market_type in APPROXIMATE_MARKET_TYPES
                else None
            ),
        ))
    # Collapse nested bracket legs to a single staked position per team. Done
    # HERE, in the backend, rather than in the cross-sport list: the paper
    # logger gates on suggested_stake_dollars, so a frontend-only fix would
    # still have logged four correlated Indiana bets as four independent paper
    # trades and quietly corrupted the very forward-CLV record we use to judge
    # this model.
    nested_zeroed = apply_nested_futures_cap(out, "cfb")
    if nested_zeroed:
        log.info("cfb futures: collapsed %d nested bracket legs to one stake per team",
                 nested_zeroed)
    out.sort(key=lambda r: (r.market_type, -(r.implied_prob or 0)))
    return out
