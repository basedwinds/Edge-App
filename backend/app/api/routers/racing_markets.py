"""Racing markets router (F1 / IndyCar / NASCAR). Reads the persisted Market
rows (poller_racing keeps them + their price snapshots fresh) so racing is a
first-class sport: each row carries a real Market id, so it rides the same
paper-log -> CLV -> recommendations machinery as every other sport.

Field per race = the drivers with markets under that race_event (Kalshi supplies
it). Prices: race_winner/top_n via racing_sim Monte Carlo (driver + constructor
Elo), pole via qualifying Elo. Grid IS wired (2026-08-02): the starting grid is
read from the cache poller_racing.refresh_racing_grids() fills once ESPN
publishes the field at the race weekend, so race-finish prices sharpen
automatically after qualifying and fall back to driver+constructor before it.
model_validated=False; forward CLV is the judge (racing can't be historically
backtested -- thin retention).
"""
import re
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _implied_prob
from app.api.routers.settings import get_racing_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import RacingMarketOut, ReasoningOut, ReasoningFactorOut
import logging

from app.db.database import get_session
from app.db.models import Market, RaceEvent
from app.models import racing_sim
from app.models.baseline import racing_ratings, racing_championship
from app.models.staking import (
    FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, size_stake_dollars,
)

router = APIRouter(prefix="/racing", tags=["racing"])

log = logging.getLogger("racing_markets")

RACING_SPORTS = ("f1", "irl", "nascar")

# Season-long titles. Everything else racing prices (race_winner, top_n, pole,
# h2h) settles on the day, so only these two belong to the futures side. Named
# once here because the same test is made in three places (sizing, the note
# chosen in _price_event, and the reasoning drawer).
CHAMPIONSHIP_MARKET_TYPES = ("drivers_champion", "constructors_champion")

# Minimum share of a race's entrants that must carry a rating before the field
# simulation is trusted to price it. See the gate in _price_event for the real
# case (NASCAR Xfinity/Truck arriving under the Cup series ticker) and why a
# coverage floor beats hard-coding which series exist.
MIN_FIELD_COVERAGE = 0.80
# The three NASCAR rating pools a sport="nascar" event may belong to. Order is
# irrelevant -- the winner is chosen by coverage, not by position.
NASCAR_RATING_SERIES = ("nascar", "nascar_xfinity", "nascar_truck")
# How far ahead of the runner-up pool the winner must be. See
# _resolve_rating_series for why a floor alone is not enough.
MIN_SERIES_MARGIN = 0.05
# Human labels for the resolved pool, surfaced in the reasoning drawer so it is
# never a mystery which series a price was built from.
RATING_SERIES_LABEL = {
    "nascar": "NASCAR Cup Series",
    "nascar_xfinity": "NASCAR Xfinity Series",
    "nascar_truck": "NASCAR Truck Series",
    "f1": "Formula 1",
    "irl": "IndyCar",
}
TRACKING_NOTE = (
    "Priced by the grid+constructor racing model (race finish) / qualifying Elo "
    "(pole). Pre-qualifying prices use driver+constructor (no grid yet); they "
    "sharpen at the race weekend. Validated forward by CLV, not backtested."
)
PRE_QUALIFYING_NOTE = (
    "Priced but not staked: qualifying hasn't run, so there is no starting grid and the model is "
    "pricing on driver and constructor form alone. Measured across every series' race history, the "
    "grid is the single biggest input -- without it the model is flat, which reads as an edge on "
    "every backmarker. It sharpens and becomes stakeable once the grid is published."
)
# LIFTED 2026-08-07, on the condition this comment set when the gate went in:
# finishing-order parameters fitted and wired (racing_ratings.TOPN_PARAMS), and
# the calibration measured on races the fit never saw. Hold-out result with the
# exact shipped values -- nascar 0.1180 -> 0.0277 (-77%), f1 0.0883 -> 0.0280
# (-68%), irl 0.0806 -> 0.0192 (-76%), xfinity 0.0904 -> 0.0179 (-80%), truck
# 0.0995 -> 0.0321 (-68%).
#
# Set back to False to hold top_n off staking again; it is the single switch.
STAKE_TOP_N = True
TOPN_CALIBRATION_NOTE = (
    "Priced and tracked, not staked: measured walk-forward over 142 real races, the finishing-order "
    "model is badly calibrated away from the front. It said 92% for a top-20 finish that really "
    "happens 69% of the time, and the error grows with N. The cause is that its strength spread was "
    "fitted on who WINS, so nothing ever constrained the rest of the order -- and a top-20 market is "
    "mostly a bet on whether the car survives, which it never simulated. The bias runs one way "
    "(favourites too high), so an apparent edge here can be entirely model error. Race winner, pole "
    "and head-to-head are unaffected and still staked."
)
CHAMP_NOTE = (
    "Season-title price: a standings-aware Monte Carlo simulates the remaining "
    "races from current championship points and driver strength. model_validated: "
    "false; forward CLV is the judge."
)


def _norm_con(name: str) -> str:
    """Fold Polymarket constructor labels onto the ratings labels
    ('Red Bull Racing'->'red bull', 'Audi Revolut'->'audi')."""
    return (name or "").lower().replace("racing", "").replace("revolut", "").strip()


def _h2h_model_prob(series: str, label: str, cc: dict) -> "float | None":
    """'A vs B' -> P(A finishes ahead of B) from race (driver+constructor)
    strength, closed-form Bradley-Terry. Surname-only labels resolve via
    resolve_driver_loose; an unknown driver leaves it unpriced."""
    pair = racing_ratings.split_h2h_label(label)
    if pair is None:
        return None
    a = racing_ratings.resolve_driver_loose(series, pair[0])
    b = racing_ratings.resolve_driver_loose(series, pair[1])
    if not a or not b:
        return None
    sa = racing_ratings.strength(series, a, cc.get(a), None)
    sb = racing_ratings.strength(series, b, cc.get(b), None)
    if sa is None or sb is None:
        return None
    return racing_sim.h2h_prob(sa, sb)



def _load_grid_cache() -> dict:
    """{race_event_id: {espn_driver_id: start_pos}} from the grid cache written by
    poller_racing.refresh_racing_grids(). Read from disk (not the network) so
    pricing stays fast; missing/unreadable cache just means no grid, which prices
    exactly as before."""
    try:
        import json
        from pathlib import Path
        from app.config import settings
        from app.ingestion.poller_racing import RACING_GRID_CACHE
        path = Path(settings.data_dir) / RACING_GRID_CACHE
        if not path.exists():
            return {}
        raw = json.loads(path.read_text())
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return {}


def _build_field(series: str, markets: list[Market],
                 grid_by_event: dict | None) -> tuple[dict[str, float], set[str], float]:
    """(rated field, entrant names, coverage) for ONE rating-series key.

    Split out of _price_event so the same field build can be run against several
    candidate series and the best one chosen -- see _resolve_rating_series.
    """
    st = racing_ratings._series_state(series)
    cc = st.get("current_constructor", {})
    race_field: dict[str, float] = {}
    entrants: set[str] = set()
    for m in markets:
        if m.market_type in ("race_winner", "top_n"):
            entrants.add((m.team or "").strip().lower())
            d = racing_ratings.resolve_driver_id(series, m.team or "")
            if d and d not in race_field:
                # Grid was hardcoded None here, so every race priced in
                # pre-qualifying mode FOREVER and never sharpened once the grid
                # was known -- even though grid is the model's strongest input
                # (f1 grid_pts=130; winner-hit 45%->62% in backtest). Now read
                # from the cache poller_racing.refresh_racing_grids() fills after
                # qualifying; still None (and so still pre-quali pricing) until
                # ESPN publishes the field.
                g = (grid_by_event or {}).get(m.race_event_id) or {}
                s = racing_ratings.strength(series, d, cc.get(d), g.get(d))
                if s is not None:
                    race_field[d] = s
    coverage = (len(race_field) / len(entrants)) if entrants else 1.0
    return race_field, entrants, coverage


def _resolve_rating_series(series: str, markets: list[Market],
                           grid_by_event: dict | None) -> tuple[str, dict[str, float], set[str], float]:
    """Which rating pool does this race belong to? Decided by the ENTRANT LIST.

    Kalshi files NASCAR Cup, Xfinity ("O'Reilly Auto Parts") and Truck under one
    series ticker, KXNASCARRACE, with no field saying which is which, so all
    three arrive as sport="nascar". Every other sport maps 1:1 and short-circuits.

    Scoring the field against each pool and taking the best is used INSTEAD of
    the two signals that look easier and are not, both checked live 2026-08-07:

      * Kalshi's title. Cup says "NASCAR Cup Series ..." and Xfinity says
        "NASCAR O'Reilly Auto Parts Series ...", but "Pennzoil 250" (Xfinity)
        and "TSport 200" (Truck) name no series at all.
      * ESPN's calendar. ESPN names races by VENUE, Kalshi by SPONSOR, Cup and
        Xfinity race at the same venue on adjacent days, and Kalshi's own date
        is unreliable (it had the HyVee Perks 250 on Aug 23 for an Aug 8 race).

    TWO CONDITIONS, not one. The winner must clear MIN_FIELD_COVERAGE *and* beat
    the runner-up by MIN_SERIES_MARGIN. The margin is the part that matters and
    it is not obvious: on 139 held-out races (scripts/check_nascar_series_
    separation.py) the Xfinity pool explained 85.6% of a typical CUP field --
    above the floor by itself -- because it is the larger pool (187 drivers vs
    103) and full of Cup regulars moonlighting. So the floor alone cannot catch
    a Cup-to-Xfinity misroute; only the margin can. The tightest real gap was
    4pp (Talladega: cup 92%, xfinity 88%), which under this rule goes unpriced
    rather than being guessed.

    That test misrouted 0 of 139, so the classifier itself is sound; this is
    about the failure mode it does not cover. Returns the ORIGINAL series with
    its own field when nothing qualifies, which reproduces the previous
    behaviour: the caller's coverage gate then leaves the race unpriced.
    """
    candidates = NASCAR_RATING_SERIES if series == "nascar" else (series,)
    if len(candidates) == 1:
        field, entrants, cov = _build_field(series, markets, grid_by_event)
        return series, field, entrants, cov

    # No race_winner/top_n rows means no entrant list to classify on -- a
    # championship-only event, which is priced from standings and never touches
    # the race field. Short-circuit BEFORE the margin test: every pool trivially
    # scores 1.0 on an empty field, so the margin is 0 and the "could not
    # identify the series" path would fire on every title event and log that it
    # was being left unpriced, which was false.
    if not any(m.market_type in ("race_winner", "top_n") for m in markets):
        field, entrants, cov = _build_field(series, markets, grid_by_event)
        return series, field, entrants, cov

    scored = []
    for cand in candidates:
        field, entrants, cov = _build_field(cand, markets, grid_by_event)
        scored.append((cov, cand, field, entrants))
    scored.sort(key=lambda x: -x[0])
    best_cov, best, best_field, best_entrants = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if best_cov >= MIN_FIELD_COVERAGE and (best_cov - runner_up) >= MIN_SERIES_MARGIN:
        if best != series:
            log.info("racing: routed a %s event to the %s pool (%.0f%% vs %.0f%% runner-up)",
                     series, best, best_cov * 100, runner_up * 100)
        return best, best_field, best_entrants, best_cov

    log.info("racing: could not identify the NASCAR series for this event -- best %s at %.0f%%, "
             "runner-up %.0f%% (need %.0f%% and a %.0fpp margin); leaving it unpriced",
             best, best_cov * 100, runner_up * 100, MIN_FIELD_COVERAGE * 100, MIN_SERIES_MARGIN * 100)
    field, entrants, cov = _build_field(series, markets, grid_by_event)
    return series, field, entrants, min(cov, MIN_FIELD_COVERAGE - 0.01) if entrants else cov


def _price_event(series: str, markets: list[Market], implied_by_id: dict[int, float | None],
                 race_start_by_event: dict | None = None,
                 grid_by_event: dict | None = None) -> list[RacingMarketOut]:
    rating_series, race_field, entrants, coverage = _resolve_rating_series(series, markets, grid_by_event)
    st = racing_ratings._series_state(rating_series)
    cc = st.get("current_constructor", {})

    def did(name: str):
        return racing_ratings.resolve_driver_id(rating_series, name)

    # FIELD COVERAGE GATE. racing_sim normalises win probability across the
    # drivers it was GIVEN, so an under-covered field does not merely lose the
    # missing drivers -- it hands their share to the ones we did rate, inflating
    # every price by roughly (entrants / rated).
    #
    # REAL CASE (found 2026-08-05, and the reason this exists). Kalshi files the
    # NASCAR Cup, Xfinity ("O'Reilly Auto Parts") and Truck series under ONE
    # series ticker, KXNASCARRACE, so all three arrive as sport="nascar". Our
    # ratings and results come from ESPN's nascar-premier feed, which is CUP
    # ONLY. Measured on the live board: the Cup race had 34 of 36 entrants rated
    # and summed to 1.00 correctly, while the Xfinity race had 13 of 37 -- those
    # 13 absorbed the entire 1.00, pricing Ryan Ellis at 8.5% against a 0.5%
    # market, a 17x overstatement. implausible_disagreement caught the extremes,
    # but 6 moderately-inflated Xfinity bets were staked anyway.
    #
    # A coverage floor is the right shape because it is series-agnostic: it fires
    # on ANY race we cannot rate (a new series, a one-off exhibition, an
    # unresolved-name spike), rather than hard-coding which NASCAR series exist.
    # 0.80 separates the real cases cleanly -- Cup 94%, F1/IndyCar effectively
    # 100%, and (before the lower-series pools existed) Xfinity 35%.
    #
    # This gate is STILL the backstop after _resolve_rating_series: it fires on
    # any race no pool can rate -- a new series, a one-off exhibition, an
    # unresolved-name spike -- and on Truck races, which average only 79.5% own
    # coverage, so roughly 44% of them stay unpriced by design.
    if entrants and coverage < MIN_FIELD_COVERAGE:
        log.info("racing: skipping %s field pricing -- only %d of %d entrants rated (%.0f%%)",
                 rating_series, len(race_field), len(entrants), coverage * 100)
        sim = {}
        topn_sim = {}
    else:
        sim = racing_sim.simulate(race_field, trials=20000) if len(race_field) >= 2 else {}
        # SECOND simulation for finishing-order markets only. top_n needs its own
        # grid weight and an attrition rate -- see racing_ratings.TOPN_PARAMS for
        # the fitted values and why the two questions cannot share parameters.
        # race_winner/pole/h2h keep reading `sim` above, which is unchanged.
        topn_field = {}
        for mk in markets:
            if mk.market_type not in ("race_winner", "top_n"):
                continue
            dd = racing_ratings.resolve_driver_id(rating_series, mk.team or "")
            if dd and dd not in topn_field:
                g = (grid_by_event or {}).get(mk.race_event_id) or {}
                s = racing_ratings.topn_strength(rating_series, dd, cc.get(dd), g.get(dd))
                if s is not None:
                    topn_field[dd] = s
        topn_sim = (racing_sim.simulate(
            topn_field, trials=20000,
            attrition=racing_ratings.TOPN_PARAMS.get(rating_series, {}).get("attrition", 0.0),
        ) if len(topn_field) >= 2 else {})

    # Pole probabilities over the FULL current grid (series-wide, from ratings),
    # not just this event's pole markets -- so constructor-pole markets, which
    # Polymarket lists in a SEPARATE event from the driver-pole markets, can still
    # be priced (they'd otherwise see an empty pole field). It's also the correct
    # softmax denominator: pole is contested by the whole grid.
    pole_field: dict[str, float] = {}
    if any(m.market_type in ("pole", "constructor_pole") for m in markets):
        for dr in cc:
            q = racing_ratings.quali_strength(rating_series, dr)
            if q is not None:
                pole_field[dr] = q
    pole_p: dict[str, float] = {}
    if len(pole_field) >= 2:
        vs = {d: 10 ** (s / 400.0) for d, s in pole_field.items()}
        tot = sum(vs.values())
        pole_p = {d: v / tot for d, v in vs.items()}

    # Constructor pole = P(EITHER of a team's drivers takes pole). Poles are
    # mutually exclusive, so the team probability is exactly the sum of its two
    # drivers' pole probabilities. Keyed by normalised constructor name so
    # Polymarket's labels ("Red Bull Racing") match the ratings' ("Red Bull").
    constructor_pole_p: dict[str, float] = {}
    for dr, p in pole_p.items():
        c = cc.get(dr)
        if c:
            constructor_pole_p[_norm_con(c)] = constructor_pole_p.get(_norm_con(c), 0.0) + p

    out: list[RacingMarketOut] = []
    for m in markets:
        d = did(m.team or "")
        mp = None
        if m.market_type == "drivers_champion":
            # Season title -> standings-aware championship sim (not racing_sim),
            # matched by driver NAME (m.team) rather than the race-field id.
            # Championships stay on the BASE series, not the resolved pool:
            # racing_championship.PRICED_SERIES is (f1, irl, nascar) only, and
            # Kalshi's NASCAR title market IS the Cup title. A championship-only
            # event also has no race_winner/top_n rows, so it has no entrants to
            # classify on and _resolve_rating_series returns the base series
            # anyway -- this is belt and braces.
            mp = racing_championship.driver_championship_prob(series, m.team or "")
        elif m.market_type == "constructors_champion":
            mp = racing_championship.constructor_championship_prob(series, m.team or "")
        elif m.market_type == "h2h":
            # "A vs B" -> P(A finishes ahead of B), closed-form from race strength.
            mp = _h2h_model_prob(rating_series, m.team or "", cc)
        elif m.market_type == "constructor_pole":
            mp = constructor_pole_p.get(_norm_con(m.team or ""))
        elif d:
            if m.market_type == "race_winner":
                mp = sim.get(d, {}).get("win")
            elif m.market_type == "top_n" and m.line is not None:
                # Finishing-order sim, not the winner sim -- see TOPN_PARAMS.
                mp = topn_sim.get(d, {}).get(f"top{int(m.line)}")
            elif m.market_type == "pole":
                mp = pole_p.get(d)
        imp = implied_by_id.get(m.id)
        edge = round(mp - imp, 4) if (mp is not None and imp is not None) else None
        is_champ = m.market_type in CHAMPIONSHIP_MARKET_TYPES
        note = CHAMP_NOTE if is_champ else TRACKING_NOTE
        # Say WHICH pool built this price when it isn't the obvious one. Kalshi
        # shows Cup, Xfinity and Truck under one ticker, so without this a
        # lower-series price is indistinguishable from a Cup price in the UI.
        if not is_champ and rating_series != series:
            note = f"{note} Rated against the {RATING_SERIES_LABEL.get(rating_series, rating_series)} pool."
        out.append(RacingMarketOut(
            id=m.id, series=series,
            series_label=RATING_SERIES_LABEL.get(rating_series if not is_champ else series),
            source=m.source, race_event_id=m.race_event_id, event=m.source_event_id, market_type=m.market_type,
            line=int(m.line) if m.line is not None else None, driver=m.team or "",
            implied_prob=imp, model_prob=mp, model_validated=False, edge=edge,
            volume=None,
            # close_time drives the DATE the UI shows for a racing bet. It used to
            # be hardcoded None, so every racing row had no date at all and the
            # frontend's formatGameDate fell through to its literal "Season-long"
            # label -- making per-race h2h/pole/podium bets look like season
            # futures in Recommended (user-reported 2026-08-02). The real race
            # start already exists on the linked RaceEvent, so use it.
            close_time=((race_start_by_event or {}).get(m.race_event_id).isoformat() + "Z")
            if (race_start_by_event or {}).get(m.race_event_id) else None,
            model_note=note if mp is not None else None,
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

    # Real race start per RaceEvent -> becomes each row's close_time (see
    # _price_event) so racing bets show a DATE instead of reading "Season-long".
    grid_by_event = _load_grid_cache()
    event_ids = {m.race_event_id for m in markets if m.race_event_id is not None}
    race_start_by_event = {
        e.id: e.start_time
        for e in session.query(RaceEvent).filter(RaceEvent.id.in_(event_ids)).all()
    } if event_ids else {}

    out: list[RacingMarketOut] = []
    for (series, _event), evmarkets in by_event.items():
        for row in _price_event(series, evmarkets, implied_by_id, race_start_by_event, grid_by_event):
            row.volume = vol_by_id.get(row.id)
            snap = snaps.get(row.id)
            has_traded = has_real_trading(src_by_id.get(row.id), snap.volume if snap else None, snap.last_price if snap else None)
            kelly = kelly_fraction(row.model_prob, row.implied_prob, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None)
            # A season title is a FUTURES position and has to be sized like one.
            # This whole block used to size every racing row identically and
            # then hardcode stake_pool="weekly", so a drivers'/constructors'
            # champion bet -- capital locked for an entire season -- was sized
            # at the full $10 unit instead of $2.50, skipped
            # FUTURES_MIN_MARKET_PRICE (so a 2% longshot title could be staked),
            # counted against the GAME cap, and was invisible to the futures cap
            # and its per-sport ceiling. Latent rather than live when found on
            # 2026-08-07 (no championship edge was clearing the gate), but it
            # would have booked silently the first time one did.
            _is_champ = row.market_type in CHAMPIONSHIP_MARKET_TYPES
            stake = size_stake_dollars(
                staking_mode, kelly, pool, row.model_prob, row.implied_prob,
                unit_dollars, flat_marginal, flat_full,
                unit_scale=FUTURES_UNIT_SCALE if _is_champ else 1.0,
                min_market_price=FUTURES_MIN_MARKET_PRICE if _is_champ else 0.0,
                sport=series if _is_champ else None,
                # The "team" for racing is the DRIVER (or constructor on a
                # constructors' title), which is what row.driver holds.
                team=row.driver if _is_champ else None,
            )
            # PRE-QUALIFYING FIELD MARKETS ARE PRICED BUT NOT STAKED.
            #
            # strength() carries a grid term, and before qualifying there is no
            # grid, so the model prices the race on driver+constructor alone and
            # comes out FLAT. Measured over each series' full race history
            # (scripts/fit_racing_params_per_series.py), with grid against
            # without:
            #
            #     series    Brier            winner-hit
            #     cup       .02574 -> .02510   7.0% -> 14.8%
            #     xfinity   .02509 -> .02387  10.3% -> 19.1%
            #     truck     .02695 -> .02517  21.5% -> 22.6%
            #     f1        .04113 -> .02806  45.0% -> 57.8%
            #     irl       .03486 -> .03186  31.0% -> 25.9%
            #
            # CALIBRATION IMPROVES IN ALL FIVE, which is the metric that governs
            # here for the same reason it governed the grid_pts refit: this model
            # never has to pick one driver, it bets wherever model_prob beats the
            # price, so profit turns on the probability being right. IndyCar is
            # the one series whose winner-hit gets WORSE with the grid, and an
            # earlier reading of this table called the grid "contraindicated" for
            # it on that basis -- that was reading off winner-hit, the metric
            # explicitly rejected as the objective. Applying one metric to the
            # parameter choice and another to this gate would be incoherent.
            #
            # WHAT IT PREVENTS, observed live: with no grid the field flattens
            # and every backmarker inherits a plausible-looking probability, so
            # top_n starts staking drivers the market prices as no-hopers --
            # Cody Ware at model 31.0% against a 3.5% market, an 8.9x ratio that
            # slips under implausible_disagreement's 10x threshold.
            #
            # SCOPE. Only race_winner and top_n, the two that consume the grid
            # term. Pole is exempt because quali_strength is a different model
            # AND a pole market resolves AT qualifying -- gating it on
            # qualifying would mean never pricing it. h2h is exempt because
            # _h2h_model_prob passes grid=None unconditionally, so it is always
            # in this state and gating it would block it permanently.
            # Championships have no grid concept at all.
            #
            # Priced, visible, tracked, not staked -- the same posture as map
            # markets. The row keeps its model number and edge so it still
            # accrues forward CLV, which is how this gate gets judged.
            if (row.market_type in ("race_winner", "top_n")
                    and not (grid_by_event or {}).get(row.race_event_id)):
                stake = None
                kelly = None
                row.model_note = f"{row.model_note or ''} {PRE_QUALIFYING_NOTE}".strip()

            # TOP-N HELD OFF STAKING, 2026-08-07. Separate from the gate above,
            # and it fires even WITH a grid -- which is the whole point. The
            # pre-qualifying gate was added because a flat field inflates
            # backmarkers; measuring whether that survived qualifying
            # (scripts/check_racing_topn_calibration.py, walk-forward over 142
            # NASCAR races priced off their own real grids) found the opposite
            # and worse problem underneath.
            #
            # The strength spread was fitted on P(1st) only, so nothing ever
            # constrained the deep tail of the finishing order. Measured, with a
            # grid known: top-20 said 92% where the truth was 69%; top-10 said 3%
            # where the truth was 15%; error grows monotonically with N. Even
            # top-3 runs +9.8pp (20-35% band) and +14.4pp (35-60%) on favourites.
            # Against a 10pp staking gate, an apparent edge on a top-N favourite
            # can be entirely model bias -- and since the app only stakes where
            # model > market, the bias and the bet point the same way every time.
            #
            # NOT a permanent judgement on top_n. racing_sim now models attrition
            # (a per-race chance of a race-ruining event, which is the missing
            # mechanism -- 24.3% of NASCAR drivers starting top-5 finish in the
            # back 40% of the field, and the sim had no way to produce that).
            # Fitting that rate per series cuts calibration error 48-78% at
            # essentially no cost to win Brier. LIFT THIS once the fitted rates
            # are wired into the pricing path and check_racing_topn_calibration
            # confirms the bands line up.
            #
            # race_winner/pole/h2h are deliberately untouched: race_winner IS the
            # model that was fitted, pole is a different one (quali Elo), and h2h
            # is closed-form with no deep tail.
            if STAKE_TOP_N is False and row.market_type == "top_n":
                stake = None
                kelly = None
                row.model_note = f"{row.model_note or ''} {TOPN_CALIBRATION_NOTE}".strip()
            row.kelly_fraction = kelly
            row.suggested_stake_dollars = stake
            row.suggested_stake_units = round(stake / unit_dollars, 2) if (stake is not None and unit_dollars) else None
            row.stake_pool = (("futures" if _is_champ else "weekly") if stake is not None else None)
            out.append(row)
    out.sort(key=lambda r: (r.series, r.event or "", r.market_type, -(r.model_prob or -1)))
    return out


_MT_LABEL = {
    "race_winner": "Race Winner", "pole": "Pole Position", "top_n": "Top-N Finish",
    "drivers_champion": "Drivers' Champion", "constructors_champion": "Constructors' Champion",
    "h2h": "Head-to-Head", "constructor_pole": "Constructor Pole",
}


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

    is_champ = m.market_type in CHAMPIONSHIP_MARKET_TYPES
    factors: list[ReasoningFactorOut] = []
    did = racing_ratings.resolve_driver_id(series, driver)
    st = racing_ratings._series_state(series)
    cc = st.get("current_constructor", {})
    if is_champ:
        meta = racing_championship.championship_meta(series)
        rem = meta.get("remaining_races")
        if rem is not None:
            factors.append(ReasoningFactorOut(label="Races remaining", detail=str(rem)))
        pts = (meta.get("points") or {}).get(driver)
        if pts is not None:
            factors.append(ReasoningFactorOut(label="Current points", detail=f"{pts:.0f}"))
    elif did:
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

    if is_champ:
        who = "constructor" if m.market_type == "constructors_champion" else "driver"
        methodology = (
            "Standings-aware season Monte Carlo (racing_championship_sim): starting from the real current "
            "championship points, each of thousands of simulated seasons plays out the remaining races -- "
            "sampling a full finishing order from driver strength (Plackett-Luce) and awarding F1 points -- "
            f"and the {who} leading at the end is champion. The share of seasons won is the price. This is why "
            "a fast car far back in the points is still a long shot: pace alone can't price a title."
        )
        insight = (
            f"{driver}'s title price comes from simulating the rest of the season off the current standings, "
            f"not raw pace. Model says {(_pct(model_prob))} vs the market's {(_pct(market_prob))}{_edge_phrase(edge)}."
        )
    elif m.market_type == "h2h":
        methodology = (
            "Head-to-head: P(the first-named driver finishes ahead of the second) is the closed-form "
            "Bradley-Terry value of their race (driver+constructor) strength ratings — no simulation needed "
            "for a clean two-way. Grid isn't used pre-qualifying, so it sharpens at the weekend like race finish."
        )
        insight = (
            f"{driver} — this price compares the two drivers' race-strength ratings head-to-head. Model says "
            f"{(_pct(model_prob))} for the first-named driver vs the market's {(_pct(market_prob))}{_edge_phrase(edge)}."
        )
    elif m.market_type == "constructor_pole":
        methodology = (
            "Constructor pole = P(EITHER of the team's two drivers takes pole) = the sum of their individual "
            "qualifying-Elo pole probabilities (poles are mutually exclusive, so summing is exact)."
        )
        insight = (
            f"{driver}'s pole price is the combined qualifying-Elo pole probability of its two drivers. Model "
            f"says {(_pct(model_prob))} vs the market's {(_pct(market_prob))}{_edge_phrase(edge)}."
        )
    elif m.market_type == "pole":
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
    ]
    if is_champ:
        caveats.append("Title odds assume the current standings and each driver's season-long pace hold; a mid-season form swing or DNF streak isn't modelled.")
    else:
        caveats.append("Pre-qualifying: no grid yet, so race-finish prices use driver+constructor and sharpen at the race weekend.")
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
