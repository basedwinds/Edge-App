"""Racing markets router (F1 / IndyCar / NASCAR). Reads the persisted Market
rows (poller_racing keeps them + their price snapshots fresh) so racing is a
first-class sport: each row carries a real Market id, so it rides the same
paper-log -> CLV -> recommendations machinery as every other sport.

Field per race = the drivers with markets under that race_event (Kalshi supplies
it). Prices: race_winner/top_n via racing_sim Monte Carlo (driver + constructor
Elo), pole via qualifying Elo. Grid isn't wired yet (ESPN publishes it only at
the race weekend) so race-finish prices are driver+constructor for now, sharper
later. model_validated=False; forward CLV is the judge (racing can't be
historically backtested -- thin retention).
"""
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _implied_prob
from app.api.routers.settings import get_racing_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import RacingMarketOut, ReasoningOut, ReasoningFactorOut
from app.db.database import get_session
from app.db.models import Market
from app.models import racing_sim
from app.models.baseline import racing_ratings
from app.models.staking import has_real_trading, kelly_fraction, size_stake_dollars

router = APIRouter(prefix="/racing", tags=["racing"])

RACING_SPORTS = ("f1", "irl", "nascar")
TRACKING_NOTE = (
    "Priced by the grid+constructor racing model (race finish) / qualifying Elo "
    "(pole). Pre-qualifying prices use driver+constructor (no grid yet); they "
    "sharpen at the race weekend. Validated forward by CLV, not backtested."
)


def _price_event(series: str, markets: list[Market], implied_by_id: dict[int, float | None]) -> list[RacingMarketOut]:
    st = racing_ratings._series_state(series)
    cc = st.get("current_constructor", {})

    def did(name: str):
        return racing_ratings.resolve_driver_id(series, name)

    race_field: dict[str, float] = {}
    for m in markets:
        if m.market_type in ("race_winner", "top_n"):
            d = did(m.team or "")
            if d and d not in race_field:
                s = racing_ratings.strength(series, d, cc.get(d), None)
                if s is not None:
                    race_field[d] = s
    sim = racing_sim.simulate(race_field, trials=20000) if len(race_field) >= 2 else {}

    pole_field: dict[str, float] = {}
    for m in markets:
        if m.market_type == "pole":
            d = did(m.team or "")
            if d and d not in pole_field:
                q = racing_ratings.quali_strength(series, d)
                if q is not None:
                    pole_field[d] = q
    pole_p: dict[str, float] = {}
    if len(pole_field) >= 2:
        vs = {d: 10 ** (s / 400.0) for d, s in pole_field.items()}
        tot = sum(vs.values())
        pole_p = {d: v / tot for d, v in vs.items()}

    out: list[RacingMarketOut] = []
    for m in markets:
        d = did(m.team or "")
        mp = None
        if d:
            if m.market_type == "race_winner":
                mp = sim.get(d, {}).get("win")
            elif m.market_type == "top_n" and m.line is not None:
                mp = sim.get(d, {}).get(f"top{int(m.line)}")
            elif m.market_type == "pole":
                mp = pole_p.get(d)
        imp = implied_by_id.get(m.id)
        edge = round(mp - imp, 4) if (mp is not None and imp is not None) else None
        snap = None  # volume pulled from implied path below
        out.append(RacingMarketOut(
            id=m.id, series=series, source=m.source, event=m.source_event_id, market_type=m.market_type,
            line=int(m.line) if m.line is not None else None, driver=m.team or "",
            implied_prob=imp, model_prob=mp, model_validated=False, edge=edge,
            volume=None, close_time=None,
            model_note=TRACKING_NOTE if mp is not None else None,
        ))
    return out


@router.get("/markets", response_model=list[RacingMarketOut])
def list_racing_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport.in_(RACING_SPORTS), Market.status == "active").all()
    snaps = _batch_latest_snapshots(session, [m.id for m in markets])
    implied_by_id = {m.id: _implied_prob(snaps.get(m.id)) for m in markets}
    vol_by_id = {m.id: (snaps.get(m.id).volume if snaps.get(m.id) else None) for m in markets}
    src_by_id = {m.id: m.source for m in markets}

    # Racing is now STAKED (paper) like every other sport -- size each edged
    # market off the racing pool via the shared staking layer. model_validated
    # is still False, so this is a paper stake for CLV, same as everything here.
    pool = get_racing_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)

    by_event: dict[tuple[str, str], list[Market]] = defaultdict(list)
    for m in markets:
        by_event[(m.sport, m.source_event_id or "")].append(m)

    out: list[RacingMarketOut] = []
    for (series, _event), evmarkets in by_event.items():
        for row in _price_event(series, evmarkets, implied_by_id):
            row.volume = vol_by_id.get(row.id)
            snap = snaps.get(row.id)
            has_traded = has_real_trading(src_by_id.get(row.id), snap.volume if snap else None, snap.last_price if snap else None)
            kelly = kelly_fraction(row.model_prob, row.implied_prob, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded)
            stake = size_stake_dollars(staking_mode, kelly, pool, row.model_prob, row.implied_prob, unit_dollars, flat_marginal, flat_full)
            row.kelly_fraction = kelly
            row.suggested_stake_dollars = stake
            row.suggested_stake_units = round(stake / unit_dollars, 2) if (stake is not None and unit_dollars) else None
            row.stake_pool = "weekly" if stake is not None else None
            out.append(row)
    out.sort(key=lambda r: (r.series, r.event or "", r.market_type, -(r.model_prob or -1)))
    return out


_MT_LABEL = {"race_winner": "Race Winner", "pole": "Pole Position", "top_n": "Top-N Finish"}


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_racing_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport not in RACING_SPORTS:
        raise HTTPException(404, "market not found")
    series = m.sport
    driver = m.team or "this driver"
    mt_label = "Top " + str(int(m.line)) if (m.market_type == "top_n" and m.line is not None) else _MT_LABEL.get(m.market_type, m.market_type)
    label = f"{driver} — {mt_label}"
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    did = racing_ratings.resolve_driver_id(series, driver)
    st = racing_ratings._series_state(series)
    cc = st.get("current_constructor", {})
    if did:
        if m.market_type == "pole":
            q = racing_ratings.quali_strength(series, did)
            if q is not None:
                factors.append(ReasoningFactorOut(label="Qualifying Elo", detail=f"{q:.0f}"))
        else:
            s = racing_ratings.strength(series, did, cc.get(did), None)
            if s is not None:
                factors.append(ReasoningFactorOut(label="Driver+constructor strength", detail=f"{s:.0f}"))
            if cc.get(did):
                factors.append(ReasoningFactorOut(label="Constructor", detail=str(cc.get(did))))
    if m.source:
        factors.append(ReasoningFactorOut(label="Market", detail=m.source.title()))

    if m.market_type == "pole":
        methodology = (
            "Qualifying-only Elo: each driver's one-lap pace rating is turned into a pole probability via a "
            "softmax over the whole qualifying field. Race-day car setup/grid isn't used (this is pure quali)."
        )
        insight = (
            f"{driver}'s pole price comes from a qualifying-Elo model: their one-lap rating is compared against "
            f"the rest of the field and normalised into a pole probability. Model says {(_pct(model_prob))}, the "
            f"market {(_pct(market_prob))}{_edge_phrase(edge)}."
        )
    else:
        methodology = (
            "Grid+constructor+driver Monte Carlo (racing_sim): every entered driver's driver+constructor Elo "
            "seeds a finishing-position simulation run ~20,000 times; the share of runs where the driver "
            f"{'wins' if m.market_type == 'race_winner' else 'finishes in the top N'} becomes the price. Grid "
            "position isn't wired yet (ESPN publishes it only at the race weekend), so pre-qualifying prices "
            "are driver+constructor and sharpen closer to the race."
        )
        insight = (
            f"{driver}'s {mt_label.lower()} price is read off a race simulation seeded by their driver + "
            f"constructor strength, run tens of thousands of times. Model says {(_pct(model_prob))} vs the "
            f"market's {(_pct(market_prob))}{_edge_phrase(edge)}."
        )

    caveats = [
        "model_validated: false -- racing can't be historically backtested (thin retention), so forward CLV is the only judge.",
        "Pre-qualifying: no grid yet, so race-finish prices use driver+constructor and sharpen at the race weekend.",
    ]
    return ReasoningOut(
        market_id=m.id, market_type=m.market_type, label=label,
        model_prob=model_prob, market_prob=market_prob, edge=edge,
        insight=insight, methodology=methodology, factors=factors, caveats=caveats,
    )


def _pct(v):
    return "—" if v is None else f"{v * 100:.0f}%"


def _edge_phrase(edge):
    if edge is None:
        return "."
    if edge > 0:
        return f", a {edge * 100:.1f}pp edge toward this pick."
    return f", so the market already rates this pick at least as high ({edge * 100:.1f}pp)."
