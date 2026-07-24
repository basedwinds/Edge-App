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

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _implied_prob
from app.api.schemas import RacingMarketOut
from app.db.database import get_session
from app.db.models import Market
from app.models import racing_sim
from app.models.baseline import racing_ratings

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
            id=m.id, series=series, event=m.source_event_id, market_type=m.market_type,
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

    by_event: dict[tuple[str, str], list[Market]] = defaultdict(list)
    for m in markets:
        by_event[(m.sport, m.source_event_id or "")].append(m)

    out: list[RacingMarketOut] = []
    for (series, _event), evmarkets in by_event.items():
        for row in _price_event(series, evmarkets, implied_by_id):
            row.volume = vol_by_id.get(row.id)
            out.append(row)
    out.sort(key=lambda r: (r.series, r.event or "", r.market_type, -(r.model_prob or -1)))
    return out
