from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.db.models import Setting

router = APIRouter(prefix="/settings", tags=["settings"])

BANKROLL_KEY = "bankroll_dollars"
DEFAULT_BANKROLL = 1000.0

# Added 2026-07-16 -- see staking.py's WEEKLY_MARKET_TYPES docstring for why
# NFL now gets a sub-allocation of the total cross-sport bankroll instead of
# staking against the whole thing.
BANKROLL_UNITS_KEY = "bankroll_units"
DEFAULT_BANKROLL_UNITS = 200.0
NFL_ALLOCATION_PCT_KEY = "nfl_allocation_pct"
DEFAULT_NFL_ALLOCATION_PCT = 0.15
FUTURES_SUBPOOL_PCT_KEY = "futures_subpool_pct"
DEFAULT_FUTURES_SUBPOOL_PCT = 0.30

# NBA's own sub-allocation, added when NBA became the 2nd sport on this same
# shared cross-sport bankroll (2026-07-17) -- user confirmed same sizing as
# NFL (15% allocation, 30/70 futures/weekly split) via AskUserQuestion,
# rather than assuming parity.
NBA_ALLOCATION_PCT_KEY = "nba_allocation_pct"
DEFAULT_NBA_ALLOCATION_PCT = 0.15
NBA_FUTURES_SUBPOOL_PCT_KEY = "nba_futures_subpool_pct"
DEFAULT_NBA_FUTURES_SUBPOOL_PCT = 0.30

# WNBA's own sub-allocation, added when WNBA became the 10th sport on this
# shared cross-sport bankroll (2026-07-22). Moneyline-only integration, so
# there is NO futures sub-pool (default 0.0 -> whole allocation lands in the
# per-game "weekly" pool). Default allocation kept at parity (15%) on add;
# the real edge-driven per-sport re-weighting + the sum-over-100% fix are a
# separate bankroll pass (WNBA measured the same modest no-average-edge as the
# NBA, so it warrants NBA-style, not zero, treatment).
WNBA_ALLOCATION_PCT_KEY = "wnba_allocation_pct"
DEFAULT_WNBA_ALLOCATION_PCT = 0.15
# Racing (F1/NASCAR/IndyCar share one pool). Small + no futures split -- it's
# now staked (paper) like the others, but its models are unbacktested so it
# defaults lighter. 2026-07-24.
RACING_ALLOCATION_PCT_KEY = "racing_allocation_pct"
DEFAULT_RACING_ALLOCATION_PCT = 0.04
WNBA_FUTURES_SUBPOOL_PCT_KEY = "wnba_futures_subpool_pct"
DEFAULT_WNBA_FUTURES_SUBPOOL_PCT = 0.0

# MLB's own sub-allocation, added when MLB became the 3rd sport on this same
# shared cross-sport bankroll (2026-07-17) -- user confirmed same sizing as
# NFL/NBA (15% allocation, 30/70 futures/weekly split) via AskUserQuestion,
# rather than assuming parity.
MLB_ALLOCATION_PCT_KEY = "mlb_allocation_pct"
DEFAULT_MLB_ALLOCATION_PCT = 0.15
MLB_FUTURES_SUBPOOL_PCT_KEY = "mlb_futures_subpool_pct"
DEFAULT_MLB_FUTURES_SUBPOOL_PCT = 0.30

# MMA's own sub-allocation, added when MMA became the 4th sport (2026-07-17).
# Same 15% total allocation as NFL/NBA/MLB, but a SMALLER futures sub-pool
# (15% vs the others' 30%) -- user-confirmed call: UFC's title/champion
# futures (KXUFCTITLE family) are real but thin/illiquid compared to NFL's
# season-long futures market, while a single UFC card generates ~70+
# per-fight markets across 6 market types at once, so nearly all the real
# opportunity here is per-fight, not futures.
MMA_ALLOCATION_PCT_KEY = "mma_allocation_pct"
DEFAULT_MMA_ALLOCATION_PCT = 0.15
MMA_FUTURES_SUBPOOL_PCT_KEY = "mma_futures_subpool_pct"
DEFAULT_MMA_FUTURES_SUBPOOL_PCT = 0.15

# Tennis's own sub-allocation, added when Tennis became the 5th sport
# (2026-07-18) -- user-confirmed 15% total allocation (same as every other
# sport). Futures sub-pool set to 15% (2026-07-19, once tournament-winner
# futures shipped -- see bracket_sim_tennis.py) -- same reasoning as MMA's
# own 15%, not NFL/NBA/MLB's 30%: real inventory exists but is thin relative
# to Tennis's own huge per-match market volume (many concurrent tournaments
# each generating dozens of moneyline/set/game markets, vs. a handful of
# tournament-winner futures rows), so nearly all the real opportunity here
# is still per-match, not futures.
TENNIS_ALLOCATION_PCT_KEY = "tennis_allocation_pct"
DEFAULT_TENNIS_ALLOCATION_PCT = 0.15
TENNIS_FUTURES_SUBPOOL_PCT_KEY = "tennis_futures_subpool_pct"
DEFAULT_TENNIS_FUTURES_SUBPOOL_PCT = 0.15

# Soccer's own sub-allocation, added when Soccer became the 6th sport
# (2026-07-19) -- user-confirmed 15% total allocation via AskUserQuestion
# (same as every other sport). Futures sub-pool defaults to 15% (matching
# Tennis's own default, the most recent precedent at build time) -- this
# app's Soccer build shipped moneyline-only in its first pass (no futures
# markets ingested yet, see soccer_markets.py), so the whole allocation
# currently lands in the per-match pool regardless of this default, same as
# Tennis's own initial state.
SOCCER_ALLOCATION_PCT_KEY = "soccer_allocation_pct"
DEFAULT_SOCCER_ALLOCATION_PCT = 0.15
SOCCER_FUTURES_SUBPOOL_PCT_KEY = "soccer_futures_subpool_pct"
DEFAULT_SOCCER_FUTURES_SUBPOOL_PCT = 0.15

# Esports originally shared ONE allocation across all 3 titles (CS2/LoL/
# Valorant), added when esports became the 7th sport (2026-07-19) -- user
# confirmed via AskUserQuestion that esports counted as a single "sport
# slot" like every other sport, so 3x the risk budget had no principled
# justification at the time.
#
# CHANGED 2026-07-20 (explicit user request): esports titles now get their
# OWN independent 15% allocation each, same as NFL/NBA/MLB/MMA/Tennis/
# Soccer -- "treat esports as different leagues like we do with other
# sports." This is a real, deliberate increase in esports' total risk
# budget (15% -> 45% of bankroll combined) -- the user explicitly
# acknowledged this and said the split may be adjusted again later, so
# don't read the 15%/15% numbers below as a settled, final call.
VALORANT_ALLOCATION_PCT_KEY = "valorant_allocation_pct"
DEFAULT_VALORANT_ALLOCATION_PCT = 0.15
VALORANT_FUTURES_SUBPOOL_PCT_KEY = "valorant_futures_subpool_pct"
DEFAULT_VALORANT_FUTURES_SUBPOOL_PCT = 0.15

CS2_ALLOCATION_PCT_KEY = "cs2_allocation_pct"
DEFAULT_CS2_ALLOCATION_PCT = 0.15
CS2_FUTURES_SUBPOOL_PCT_KEY = "cs2_futures_subpool_pct"
DEFAULT_CS2_FUTURES_SUBPOOL_PCT = 0.15

LOL_ALLOCATION_PCT_KEY = "lol_allocation_pct"
DEFAULT_LOL_ALLOCATION_PCT = 0.15
LOL_FUTURES_SUBPOOL_PCT_KEY = "lol_futures_subpool_pct"
DEFAULT_LOL_FUTURES_SUBPOOL_PCT = 0.15

# Added 2026-07-16: these were hardcoded constants in staking.py -- moved to
# user-editable settings so tuning them doesn't need a code round-trip.
# Defaults match staking.py's original constants exactly (FRACTIONAL_KELLY/
# MAX_STAKE_FRACTION/MIN_EDGE_TO_BET), imported lazily below to avoid a
# circular import (staking.py has no reason to import this router module).
FRACTIONAL_KELLY_KEY = "fractional_kelly"
MAX_STAKE_FRACTION_KEY = "max_stake_fraction"
MIN_EDGE_TO_BET_KEY = "min_edge_to_bet"

# Flat/scaled unit staking (2026-07-23). "flat" sizes each qualifying bet by
# unit tier (see staking.py::size_stake_dollars) instead of Kelly*pool -- the
# honest fit for an unvalidated model (Kelly on a bad model over-bets its own
# errors). "kelly" restores the classic per-sport-pool Kelly sizing. Stored as
# DB settings so the mode can be flipped without a code change; defaults to
# "flat" per the user's choice.
STAKING_MODE_KEY = "staking_mode"
DEFAULT_STAKING_MODE = "flat"
FLAT_MARGINAL_EDGE_KEY = "flat_marginal_edge"
FLAT_FULL_EDGE_KEY = "flat_full_edge"


class SettingsOut(BaseModel):
    bankroll_dollars: float
    bankroll_units: float
    nfl_allocation_pct: float
    futures_subpool_pct: float
    nba_allocation_pct: float
    nba_futures_subpool_pct: float
    wnba_allocation_pct: float
    wnba_futures_subpool_pct: float
    mlb_allocation_pct: float
    mlb_futures_subpool_pct: float
    mma_allocation_pct: float
    mma_futures_subpool_pct: float
    tennis_allocation_pct: float
    tennis_futures_subpool_pct: float
    soccer_allocation_pct: float
    soccer_futures_subpool_pct: float
    valorant_allocation_pct: float
    valorant_futures_subpool_pct: float
    cs2_allocation_pct: float
    cs2_futures_subpool_pct: float
    lol_allocation_pct: float
    lol_futures_subpool_pct: float
    fractional_kelly: float
    max_stake_fraction: float
    min_edge_to_bet: float
    # Derived, computed here so the frontend doesn't need to duplicate the
    # math -- see staking.py.
    unit_dollars: float
    nfl_pool_dollars: float
    futures_pool_dollars: float
    weekly_pool_dollars: float
    nba_pool_dollars: float
    nba_futures_pool_dollars: float
    nba_weekly_pool_dollars: float
    wnba_pool_dollars: float
    wnba_futures_pool_dollars: float
    wnba_weekly_pool_dollars: float
    mlb_pool_dollars: float
    mlb_futures_pool_dollars: float
    mlb_weekly_pool_dollars: float
    mma_pool_dollars: float
    mma_futures_pool_dollars: float
    mma_weekly_pool_dollars: float
    tennis_pool_dollars: float
    tennis_futures_pool_dollars: float
    tennis_weekly_pool_dollars: float
    soccer_pool_dollars: float
    soccer_futures_pool_dollars: float
    soccer_weekly_pool_dollars: float
    valorant_pool_dollars: float
    valorant_futures_pool_dollars: float
    valorant_weekly_pool_dollars: float
    cs2_pool_dollars: float
    cs2_futures_pool_dollars: float
    cs2_weekly_pool_dollars: float
    lol_pool_dollars: float
    lol_futures_pool_dollars: float
    lol_weekly_pool_dollars: float
    racing_weekly_pool_dollars: float  # f1/nascar/irl share one pool (no futures sub-pool)
    # Sum of every sport's allocation pct. >1.0 means the per-sport pools
    # over-commit the bankroll (if every sport maxed its bets at once you'd
    # stake more than 100%). Surfaced so the over-allocation is visible; the
    # actual per-sport re-weighting is a user decision (all sports measure the
    # same ~0 average edge, so there's no data-driven favorite to up-weight).
    total_allocation_pct: float


class SettingsIn(BaseModel):
    bankroll_dollars: float
    bankroll_units: float
    nfl_allocation_pct: float
    futures_subpool_pct: float
    # Defaulted rather than required so any OTHER future client of this same
    # PUT (e.g. a script) that doesn't care about NBA/MLB sizing can't 422 --
    # Settings.tsx itself always sends real values now.
    nba_allocation_pct: float = DEFAULT_NBA_ALLOCATION_PCT
    nba_futures_subpool_pct: float = DEFAULT_NBA_FUTURES_SUBPOOL_PCT
    wnba_allocation_pct: float = DEFAULT_WNBA_ALLOCATION_PCT
    wnba_futures_subpool_pct: float = DEFAULT_WNBA_FUTURES_SUBPOOL_PCT
    mlb_allocation_pct: float = DEFAULT_MLB_ALLOCATION_PCT
    mlb_futures_subpool_pct: float = DEFAULT_MLB_FUTURES_SUBPOOL_PCT
    mma_allocation_pct: float = DEFAULT_MMA_ALLOCATION_PCT
    mma_futures_subpool_pct: float = DEFAULT_MMA_FUTURES_SUBPOOL_PCT
    tennis_allocation_pct: float = DEFAULT_TENNIS_ALLOCATION_PCT
    tennis_futures_subpool_pct: float = DEFAULT_TENNIS_FUTURES_SUBPOOL_PCT
    soccer_allocation_pct: float = DEFAULT_SOCCER_ALLOCATION_PCT
    soccer_futures_subpool_pct: float = DEFAULT_SOCCER_FUTURES_SUBPOOL_PCT
    valorant_allocation_pct: float = DEFAULT_VALORANT_ALLOCATION_PCT
    valorant_futures_subpool_pct: float = DEFAULT_VALORANT_FUTURES_SUBPOOL_PCT
    cs2_allocation_pct: float = DEFAULT_CS2_ALLOCATION_PCT
    cs2_futures_subpool_pct: float = DEFAULT_CS2_FUTURES_SUBPOOL_PCT
    lol_allocation_pct: float = DEFAULT_LOL_ALLOCATION_PCT
    lol_futures_subpool_pct: float = DEFAULT_LOL_FUTURES_SUBPOOL_PCT
    # Racing was readable via SettingsOut but NOT settable here, so a racing
    # allocation could never be synced to another machine (found 2026-08-02 while
    # syncing the Vultr host, which was silently running default bankroll/pools).
    racing_allocation_pct: float = DEFAULT_RACING_ALLOCATION_PCT
    fractional_kelly: float
    max_stake_fraction: float
    min_edge_to_bet: float


def _get_float(session: Session, key: str, default: float) -> float:
    row = session.get(Setting, key)
    return float(row.value) if row else default


def _set_float(session: Session, key: str, value: float) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=str(value))
        session.add(row)
    else:
        row.value = str(value)


def _get_str(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if (row and row.value) else default


def _set_str(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


# ---- new-recommendation alerts (Discord webhook) ----------------------------
DISCORD_WEBHOOK_KEY = "discord_webhook_url"
ALERT_MIN_EDGE_KEY = "alert_min_edge_pp"
DEFAULT_ALERT_MIN_EDGE = 0.03  # match the recommend gate (MIN_EDGE_TO_BET = 0.03): alert on
# EVERY bet that enters the Recommended section. (Was 0.05 to suppress marginal
# 3-5pp bets; user wants a ping for all recommended bets, so it's coupled to the
# same 3pp floor the app uses to recommend in the first place.)


def get_alert_config(session: Session) -> dict:
    return {
        "webhook_url": _get_str(session, DISCORD_WEBHOOK_KEY),
        "min_edge": _get_float(session, ALERT_MIN_EDGE_KEY, DEFAULT_ALERT_MIN_EDGE),
    }


def _build_settings_out(
    bankroll_dollars: float,
    bankroll_units: float,
    nfl_allocation_pct: float,
    futures_subpool_pct: float,
    nba_allocation_pct: float,
    nba_futures_subpool_pct: float,
    wnba_allocation_pct: float,
    wnba_futures_subpool_pct: float,
    mlb_allocation_pct: float,
    mlb_futures_subpool_pct: float,
    mma_allocation_pct: float,
    mma_futures_subpool_pct: float,
    tennis_allocation_pct: float,
    tennis_futures_subpool_pct: float,
    soccer_allocation_pct: float,
    soccer_futures_subpool_pct: float,
    valorant_allocation_pct: float,
    valorant_futures_subpool_pct: float,
    cs2_allocation_pct: float,
    cs2_futures_subpool_pct: float,
    lol_allocation_pct: float,
    lol_futures_subpool_pct: float,
    fractional_kelly: float,
    max_stake_fraction: float,
    min_edge_to_bet: float,
    racing_weekly_pool_dollars: float,  # precomputed by the caller (needs the session)
) -> SettingsOut:
    unit_dollars = bankroll_dollars / bankroll_units if bankroll_units > 0 else 0.0
    _bs_scale = _scale_for_total(
        nfl_allocation_pct + nba_allocation_pct + wnba_allocation_pct + mlb_allocation_pct
        + mma_allocation_pct + tennis_allocation_pct + soccer_allocation_pct
        + valorant_allocation_pct + cs2_allocation_pct + lol_allocation_pct
    )
    nfl_pool_dollars = bankroll_dollars * nfl_allocation_pct * _bs_scale
    futures_pool_dollars = nfl_pool_dollars * futures_subpool_pct
    weekly_pool_dollars = nfl_pool_dollars * (1.0 - futures_subpool_pct)
    nba_pool_dollars = bankroll_dollars * nba_allocation_pct * _bs_scale
    nba_futures_pool_dollars = nba_pool_dollars * nba_futures_subpool_pct
    nba_weekly_pool_dollars = nba_pool_dollars * (1.0 - nba_futures_subpool_pct)
    wnba_pool_dollars = bankroll_dollars * wnba_allocation_pct * _bs_scale
    wnba_futures_pool_dollars = wnba_pool_dollars * wnba_futures_subpool_pct
    wnba_weekly_pool_dollars = wnba_pool_dollars * (1.0 - wnba_futures_subpool_pct)
    mlb_pool_dollars = bankroll_dollars * mlb_allocation_pct * _bs_scale
    mlb_futures_pool_dollars = mlb_pool_dollars * mlb_futures_subpool_pct
    mlb_weekly_pool_dollars = mlb_pool_dollars * (1.0 - mlb_futures_subpool_pct)
    mma_pool_dollars = bankroll_dollars * mma_allocation_pct * _bs_scale
    mma_futures_pool_dollars = mma_pool_dollars * mma_futures_subpool_pct
    mma_weekly_pool_dollars = mma_pool_dollars * (1.0 - mma_futures_subpool_pct)
    tennis_pool_dollars = bankroll_dollars * tennis_allocation_pct * _bs_scale
    tennis_futures_pool_dollars = tennis_pool_dollars * tennis_futures_subpool_pct
    tennis_weekly_pool_dollars = tennis_pool_dollars * (1.0 - tennis_futures_subpool_pct)
    soccer_pool_dollars = bankroll_dollars * soccer_allocation_pct * _bs_scale
    soccer_futures_pool_dollars = soccer_pool_dollars * soccer_futures_subpool_pct
    soccer_weekly_pool_dollars = soccer_pool_dollars * (1.0 - soccer_futures_subpool_pct)
    valorant_pool_dollars = bankroll_dollars * valorant_allocation_pct * _bs_scale
    valorant_futures_pool_dollars = valorant_pool_dollars * valorant_futures_subpool_pct
    valorant_weekly_pool_dollars = valorant_pool_dollars * (1.0 - valorant_futures_subpool_pct)
    cs2_pool_dollars = bankroll_dollars * cs2_allocation_pct * _bs_scale
    cs2_futures_pool_dollars = cs2_pool_dollars * cs2_futures_subpool_pct
    cs2_weekly_pool_dollars = cs2_pool_dollars * (1.0 - cs2_futures_subpool_pct)
    lol_pool_dollars = bankroll_dollars * lol_allocation_pct * _bs_scale
    lol_futures_pool_dollars = lol_pool_dollars * lol_futures_subpool_pct
    lol_weekly_pool_dollars = lol_pool_dollars * (1.0 - lol_futures_subpool_pct)
    return SettingsOut(
        bankroll_dollars=bankroll_dollars,
        bankroll_units=bankroll_units,
        nfl_allocation_pct=nfl_allocation_pct,
        futures_subpool_pct=futures_subpool_pct,
        nba_allocation_pct=nba_allocation_pct,
        nba_futures_subpool_pct=nba_futures_subpool_pct,
        wnba_allocation_pct=wnba_allocation_pct,
        wnba_futures_subpool_pct=wnba_futures_subpool_pct,
        mlb_allocation_pct=mlb_allocation_pct,
        mlb_futures_subpool_pct=mlb_futures_subpool_pct,
        mma_allocation_pct=mma_allocation_pct,
        mma_futures_subpool_pct=mma_futures_subpool_pct,
        tennis_allocation_pct=tennis_allocation_pct,
        tennis_futures_subpool_pct=tennis_futures_subpool_pct,
        soccer_allocation_pct=soccer_allocation_pct,
        soccer_futures_subpool_pct=soccer_futures_subpool_pct,
        valorant_allocation_pct=valorant_allocation_pct,
        valorant_futures_subpool_pct=valorant_futures_subpool_pct,
        cs2_allocation_pct=cs2_allocation_pct,
        cs2_futures_subpool_pct=cs2_futures_subpool_pct,
        lol_allocation_pct=lol_allocation_pct,
        lol_futures_subpool_pct=lol_futures_subpool_pct,
        fractional_kelly=fractional_kelly,
        max_stake_fraction=max_stake_fraction,
        min_edge_to_bet=min_edge_to_bet,
        unit_dollars=unit_dollars,
        nfl_pool_dollars=nfl_pool_dollars,
        futures_pool_dollars=futures_pool_dollars,
        weekly_pool_dollars=weekly_pool_dollars,
        nba_pool_dollars=nba_pool_dollars,
        nba_futures_pool_dollars=nba_futures_pool_dollars,
        nba_weekly_pool_dollars=nba_weekly_pool_dollars,
        wnba_pool_dollars=wnba_pool_dollars,
        wnba_futures_pool_dollars=wnba_futures_pool_dollars,
        wnba_weekly_pool_dollars=wnba_weekly_pool_dollars,
        mlb_pool_dollars=mlb_pool_dollars,
        mlb_futures_pool_dollars=mlb_futures_pool_dollars,
        mlb_weekly_pool_dollars=mlb_weekly_pool_dollars,
        mma_pool_dollars=mma_pool_dollars,
        mma_futures_pool_dollars=mma_futures_pool_dollars,
        mma_weekly_pool_dollars=mma_weekly_pool_dollars,
        tennis_pool_dollars=tennis_pool_dollars,
        tennis_futures_pool_dollars=tennis_futures_pool_dollars,
        tennis_weekly_pool_dollars=tennis_weekly_pool_dollars,
        soccer_pool_dollars=soccer_pool_dollars,
        soccer_futures_pool_dollars=soccer_futures_pool_dollars,
        soccer_weekly_pool_dollars=soccer_weekly_pool_dollars,
        valorant_pool_dollars=valorant_pool_dollars,
        valorant_futures_pool_dollars=valorant_futures_pool_dollars,
        valorant_weekly_pool_dollars=valorant_weekly_pool_dollars,
        cs2_pool_dollars=cs2_pool_dollars,
        cs2_futures_pool_dollars=cs2_futures_pool_dollars,
        cs2_weekly_pool_dollars=cs2_weekly_pool_dollars,
        lol_pool_dollars=lol_pool_dollars,
        lol_futures_pool_dollars=lol_futures_pool_dollars,
        lol_weekly_pool_dollars=lol_weekly_pool_dollars,
        racing_weekly_pool_dollars=racing_weekly_pool_dollars,
        total_allocation_pct=round(
            nfl_allocation_pct + nba_allocation_pct + wnba_allocation_pct + mlb_allocation_pct
            + mma_allocation_pct + tennis_allocation_pct + soccer_allocation_pct
            + valorant_allocation_pct + cs2_allocation_pct + lol_allocation_pct,
            4,
        ),
    )


def _read_all(session: Session) -> SettingsOut:
    from app.models.staking import FRACTIONAL_KELLY, MAX_STAKE_FRACTION, MIN_EDGE_TO_BET

    return _build_settings_out(
        _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL),
        _get_float(session, BANKROLL_UNITS_KEY, DEFAULT_BANKROLL_UNITS),
        _get_float(session, NFL_ALLOCATION_PCT_KEY, DEFAULT_NFL_ALLOCATION_PCT),
        _get_float(session, FUTURES_SUBPOOL_PCT_KEY, DEFAULT_FUTURES_SUBPOOL_PCT),
        _get_float(session, NBA_ALLOCATION_PCT_KEY, DEFAULT_NBA_ALLOCATION_PCT),
        _get_float(session, NBA_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_NBA_FUTURES_SUBPOOL_PCT),
        _get_float(session, WNBA_ALLOCATION_PCT_KEY, DEFAULT_WNBA_ALLOCATION_PCT),
        _get_float(session, WNBA_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_WNBA_FUTURES_SUBPOOL_PCT),
        _get_float(session, MLB_ALLOCATION_PCT_KEY, DEFAULT_MLB_ALLOCATION_PCT),
        _get_float(session, MLB_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_MLB_FUTURES_SUBPOOL_PCT),
        _get_float(session, MMA_ALLOCATION_PCT_KEY, DEFAULT_MMA_ALLOCATION_PCT),
        _get_float(session, MMA_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_MMA_FUTURES_SUBPOOL_PCT),
        _get_float(session, TENNIS_ALLOCATION_PCT_KEY, DEFAULT_TENNIS_ALLOCATION_PCT),
        _get_float(session, TENNIS_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_TENNIS_FUTURES_SUBPOOL_PCT),
        _get_float(session, SOCCER_ALLOCATION_PCT_KEY, DEFAULT_SOCCER_ALLOCATION_PCT),
        _get_float(session, SOCCER_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_SOCCER_FUTURES_SUBPOOL_PCT),
        _get_float(session, VALORANT_ALLOCATION_PCT_KEY, DEFAULT_VALORANT_ALLOCATION_PCT),
        _get_float(session, VALORANT_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_VALORANT_FUTURES_SUBPOOL_PCT),
        _get_float(session, CS2_ALLOCATION_PCT_KEY, DEFAULT_CS2_ALLOCATION_PCT),
        _get_float(session, CS2_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_CS2_FUTURES_SUBPOOL_PCT),
        _get_float(session, LOL_ALLOCATION_PCT_KEY, DEFAULT_LOL_ALLOCATION_PCT),
        _get_float(session, LOL_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_LOL_FUTURES_SUBPOOL_PCT),
        _get_float(session, FRACTIONAL_KELLY_KEY, FRACTIONAL_KELLY),
        _get_float(session, MAX_STAKE_FRACTION_KEY, MAX_STAKE_FRACTION),
        _get_float(session, MIN_EDGE_TO_BET_KEY, MIN_EDGE_TO_BET),
        get_racing_pool_dollars(session),
    )


class AlertConfigOut(BaseModel):
    webhook_configured: bool  # never echo the URL back (it's a secret)
    min_edge_pp: float


class AlertConfigIn(BaseModel):
    webhook_url: str | None = None  # None = leave unchanged; "" = clear it
    min_edge_pp: float | None = None


@router.get("/alerts", response_model=AlertConfigOut)
def get_alert_settings(session: Session = Depends(get_session)):
    cfg = get_alert_config(session)
    return AlertConfigOut(webhook_configured=bool(cfg["webhook_url"]), min_edge_pp=cfg["min_edge"])


@router.put("/alerts", response_model=AlertConfigOut)
def update_alert_settings(body: AlertConfigIn, session: Session = Depends(get_session)):
    if body.webhook_url is not None:
        _set_str(session, DISCORD_WEBHOOK_KEY, body.webhook_url.strip())
    if body.min_edge_pp is not None:
        _set_float(session, ALERT_MIN_EDGE_KEY, body.min_edge_pp)
    session.commit()
    cfg = get_alert_config(session)
    return AlertConfigOut(webhook_configured=bool(cfg["webhook_url"]), min_edge_pp=cfg["min_edge"])


@router.get("", response_model=SettingsOut)
def get_settings(session: Session = Depends(get_session)):
    return _read_all(session)


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsIn, session: Session = Depends(get_session)):
    _set_float(session, BANKROLL_KEY, body.bankroll_dollars)
    _set_float(session, BANKROLL_UNITS_KEY, body.bankroll_units)
    _set_float(session, NFL_ALLOCATION_PCT_KEY, body.nfl_allocation_pct)
    _set_float(session, FUTURES_SUBPOOL_PCT_KEY, body.futures_subpool_pct)
    _set_float(session, NBA_ALLOCATION_PCT_KEY, body.nba_allocation_pct)
    _set_float(session, NBA_FUTURES_SUBPOOL_PCT_KEY, body.nba_futures_subpool_pct)
    _set_float(session, WNBA_ALLOCATION_PCT_KEY, body.wnba_allocation_pct)
    _set_float(session, WNBA_FUTURES_SUBPOOL_PCT_KEY, body.wnba_futures_subpool_pct)
    _set_float(session, MLB_ALLOCATION_PCT_KEY, body.mlb_allocation_pct)
    _set_float(session, MLB_FUTURES_SUBPOOL_PCT_KEY, body.mlb_futures_subpool_pct)
    _set_float(session, MMA_ALLOCATION_PCT_KEY, body.mma_allocation_pct)
    _set_float(session, MMA_FUTURES_SUBPOOL_PCT_KEY, body.mma_futures_subpool_pct)
    _set_float(session, TENNIS_ALLOCATION_PCT_KEY, body.tennis_allocation_pct)
    _set_float(session, TENNIS_FUTURES_SUBPOOL_PCT_KEY, body.tennis_futures_subpool_pct)
    _set_float(session, SOCCER_ALLOCATION_PCT_KEY, body.soccer_allocation_pct)
    _set_float(session, SOCCER_FUTURES_SUBPOOL_PCT_KEY, body.soccer_futures_subpool_pct)
    _set_float(session, VALORANT_ALLOCATION_PCT_KEY, body.valorant_allocation_pct)
    _set_float(session, VALORANT_FUTURES_SUBPOOL_PCT_KEY, body.valorant_futures_subpool_pct)
    _set_float(session, CS2_ALLOCATION_PCT_KEY, body.cs2_allocation_pct)
    _set_float(session, CS2_FUTURES_SUBPOOL_PCT_KEY, body.cs2_futures_subpool_pct)
    _set_float(session, LOL_ALLOCATION_PCT_KEY, body.lol_allocation_pct)
    _set_float(session, LOL_FUTURES_SUBPOOL_PCT_KEY, body.lol_futures_subpool_pct)
    _set_float(session, RACING_ALLOCATION_PCT_KEY, body.racing_allocation_pct)
    _set_float(session, FRACTIONAL_KELLY_KEY, body.fractional_kelly)
    _set_float(session, MAX_STAKE_FRACTION_KEY, body.max_stake_fraction)
    _set_float(session, MIN_EDGE_TO_BET_KEY, body.min_edge_to_bet)
    session.commit()
    return _read_all(session)


_ALL_ALLOCATION_KEYS = [
    (NFL_ALLOCATION_PCT_KEY, DEFAULT_NFL_ALLOCATION_PCT),
    (NBA_ALLOCATION_PCT_KEY, DEFAULT_NBA_ALLOCATION_PCT),
    (WNBA_ALLOCATION_PCT_KEY, DEFAULT_WNBA_ALLOCATION_PCT),
    (MLB_ALLOCATION_PCT_KEY, DEFAULT_MLB_ALLOCATION_PCT),
    (MMA_ALLOCATION_PCT_KEY, DEFAULT_MMA_ALLOCATION_PCT),
    (TENNIS_ALLOCATION_PCT_KEY, DEFAULT_TENNIS_ALLOCATION_PCT),
    (SOCCER_ALLOCATION_PCT_KEY, DEFAULT_SOCCER_ALLOCATION_PCT),
    (VALORANT_ALLOCATION_PCT_KEY, DEFAULT_VALORANT_ALLOCATION_PCT),
    (CS2_ALLOCATION_PCT_KEY, DEFAULT_CS2_ALLOCATION_PCT),
    (LOL_ALLOCATION_PCT_KEY, DEFAULT_LOL_ALLOCATION_PCT),
]


def _scale_for_total(total_allocation_pct: float) -> float:
    """Proportional over-allocation guard (user-chosen 2026-07-22): if the
    per-sport allocations sum to >100% of the bankroll, every sport's pool is
    scaled down by 1/total so the pools can never collectively over-commit the
    bankroll -- while preserving each sport's RELATIVE weight (no data-driven
    favorite, since every sport measures the same ~0 average edge). A no-op
    (scale 1.0) whenever the allocations already sum to <=100%, so setting sane
    numbers in Settings disables it automatically. Kept separate from the
    quarter-Kelly / 5%-cap / 3pp-gate staking guards (staking.py) -- those cap a
    SINGLE bet; this caps the sum of all sports' POOLS."""
    return min(1.0, 1.0 / total_allocation_pct) if total_allocation_pct > 0 else 1.0


def _allocation_scale(session: Session) -> float:
    total = sum(_get_float(session, key, default) for key, default in _ALL_ALLOCATION_KEYS)
    return _scale_for_total(total)


def get_bankroll(session: Session) -> float:
    return _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)


def get_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) -- see
    staking.py's WEEKLY_MARKET_TYPES docstring."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    nfl_allocation_pct = _get_float(session, NFL_ALLOCATION_PCT_KEY, DEFAULT_NFL_ALLOCATION_PCT)
    futures_subpool_pct = _get_float(session, FUTURES_SUBPOOL_PCT_KEY, DEFAULT_FUTURES_SUBPOOL_PCT)
    nfl_pool = bankroll * nfl_allocation_pct * _allocation_scale(session)
    return nfl_pool * (1.0 - futures_subpool_pct), nfl_pool * futures_subpool_pct


def get_nba_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) for NBA's own
    sub-allocation -- parallel to get_pool_dollars (NFL)."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    nba_allocation_pct = _get_float(session, NBA_ALLOCATION_PCT_KEY, DEFAULT_NBA_ALLOCATION_PCT)
    nba_futures_subpool_pct = _get_float(session, NBA_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_NBA_FUTURES_SUBPOOL_PCT)
    nba_pool = bankroll * nba_allocation_pct * _allocation_scale(session)
    return nba_pool * (1.0 - nba_futures_subpool_pct), nba_pool * nba_futures_subpool_pct


def get_wnba_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) for WNBA's own
    sub-allocation -- parallel to get_nba_pool_dollars. "weekly" = per-game;
    no WNBA futures ingested (moneyline-only), so futures_subpool_pct defaults
    to 0.0 and the whole allocation lands in the per-game pool."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    wnba_allocation_pct = _get_float(session, WNBA_ALLOCATION_PCT_KEY, DEFAULT_WNBA_ALLOCATION_PCT)
    wnba_futures_subpool_pct = _get_float(session, WNBA_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_WNBA_FUTURES_SUBPOOL_PCT)
    wnba_pool = bankroll * wnba_allocation_pct * _allocation_scale(session)
    return wnba_pool * (1.0 - wnba_futures_subpool_pct), wnba_pool * wnba_futures_subpool_pct


def get_racing_pool_dollars(session: Session) -> float:
    """Single per-race pool for F1/NASCAR/IndyCar (no futures split -- racing
    futures aren't modeled). Racing is now staked (paper) like every other
    sport rather than tracking-only."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    racing_allocation_pct = _get_float(session, RACING_ALLOCATION_PCT_KEY, DEFAULT_RACING_ALLOCATION_PCT)
    return bankroll * racing_allocation_pct * _allocation_scale(session)


def get_mlb_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) for MLB's own
    sub-allocation -- parallel to get_pool_dollars (NFL)/get_nba_pool_dollars."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    mlb_allocation_pct = _get_float(session, MLB_ALLOCATION_PCT_KEY, DEFAULT_MLB_ALLOCATION_PCT)
    mlb_futures_subpool_pct = _get_float(session, MLB_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_MLB_FUTURES_SUBPOOL_PCT)
    mlb_pool = bankroll * mlb_allocation_pct * _allocation_scale(session)
    return mlb_pool * (1.0 - mlb_futures_subpool_pct), mlb_pool * mlb_futures_subpool_pct


def get_mma_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) for MMA's own
    sub-allocation -- parallel to get_pool_dollars (NFL)/get_mlb_pool_dollars.
    "weekly" here means "per-fight" (moneyline/distance/method/rounds/
    round_of_victory) -- reusing WEEKLY_MARKET_TYPES' pool naming rather than
    inventing a fourth pool name, since the underlying mechanic (capital
    frees up once the market settles, vs. locked up for futures) is
    identical even though UFC's cadence isn't literally weekly."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    mma_allocation_pct = _get_float(session, MMA_ALLOCATION_PCT_KEY, DEFAULT_MMA_ALLOCATION_PCT)
    mma_futures_subpool_pct = _get_float(session, MMA_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_MMA_FUTURES_SUBPOOL_PCT)
    mma_pool = bankroll * mma_allocation_pct * _allocation_scale(session)
    return mma_pool * (1.0 - mma_futures_subpool_pct), mma_pool * mma_futures_subpool_pct


def get_tennis_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) for Tennis's own
    sub-allocation -- parallel to get_pool_dollars (NFL)/get_mma_pool_dollars.
    "weekly" here means "per-match" -- reusing WEEKLY_MARKET_TYPES' pool
    naming rather than inventing a new one (moneyline is already in that
    set, shared across every sport). futures_subpool_pct defaults to 0.0
    (no Tennis futures built yet), so the whole allocation currently lands
    in the per-match pool."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    tennis_allocation_pct = _get_float(session, TENNIS_ALLOCATION_PCT_KEY, DEFAULT_TENNIS_ALLOCATION_PCT)
    tennis_futures_subpool_pct = _get_float(session, TENNIS_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_TENNIS_FUTURES_SUBPOOL_PCT)
    tennis_pool = bankroll * tennis_allocation_pct * _allocation_scale(session)
    return tennis_pool * (1.0 - tennis_futures_subpool_pct), tennis_pool * tennis_futures_subpool_pct


def get_soccer_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) for Soccer's own
    sub-allocation -- parallel to get_tennis_pool_dollars. "weekly" here
    means "per-match" (moneyline_3way is already in WEEKLY_MARKET_TYPES,
    shared across every sport). No Soccer futures markets ingested yet (see
    soccer_markets.py), so the whole allocation currently lands in the
    per-match pool regardless of soccer_futures_subpool_pct's default."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    soccer_allocation_pct = _get_float(session, SOCCER_ALLOCATION_PCT_KEY, DEFAULT_SOCCER_ALLOCATION_PCT)
    soccer_futures_subpool_pct = _get_float(session, SOCCER_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_SOCCER_FUTURES_SUBPOOL_PCT)
    soccer_pool = bankroll * soccer_allocation_pct * _allocation_scale(session)
    return soccer_pool * (1.0 - soccer_futures_subpool_pct), soccer_pool * soccer_futures_subpool_pct


def get_valorant_pool_dollars(session: Session) -> tuple[float, float]:
    """Returns (weekly_pool_dollars, futures_pool_dollars) for Valorant's own
    independent sub-allocation -- parallel to get_tennis_pool_dollars/
    get_soccer_pool_dollars. Each esports title gets its OWN 15% slot as of
    2026-07-20 (see VALORANT_ALLOCATION_PCT_KEY's own docstring above) --
    this used to be one pool shared across all 3 titles."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    valorant_allocation_pct = _get_float(session, VALORANT_ALLOCATION_PCT_KEY, DEFAULT_VALORANT_ALLOCATION_PCT)
    valorant_futures_subpool_pct = _get_float(session, VALORANT_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_VALORANT_FUTURES_SUBPOOL_PCT)
    valorant_pool = bankroll * valorant_allocation_pct * _allocation_scale(session)
    return valorant_pool * (1.0 - valorant_futures_subpool_pct), valorant_pool * valorant_futures_subpool_pct


def get_cs2_pool_dollars(session: Session) -> tuple[float, float]:
    """CS2's own independent sub-allocation -- parallel to
    get_valorant_pool_dollars."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    cs2_allocation_pct = _get_float(session, CS2_ALLOCATION_PCT_KEY, DEFAULT_CS2_ALLOCATION_PCT)
    cs2_futures_subpool_pct = _get_float(session, CS2_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_CS2_FUTURES_SUBPOOL_PCT)
    cs2_pool = bankroll * cs2_allocation_pct * _allocation_scale(session)
    return cs2_pool * (1.0 - cs2_futures_subpool_pct), cs2_pool * cs2_futures_subpool_pct


def get_lol_pool_dollars(session: Session) -> tuple[float, float]:
    """LoL's own independent sub-allocation -- parallel to
    get_valorant_pool_dollars."""
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    lol_allocation_pct = _get_float(session, LOL_ALLOCATION_PCT_KEY, DEFAULT_LOL_ALLOCATION_PCT)
    lol_futures_subpool_pct = _get_float(session, LOL_FUTURES_SUBPOOL_PCT_KEY, DEFAULT_LOL_FUTURES_SUBPOOL_PCT)
    lol_pool = bankroll * lol_allocation_pct * _allocation_scale(session)
    return lol_pool * (1.0 - lol_futures_subpool_pct), lol_pool * lol_futures_subpool_pct


def get_unit_dollars(session: Session) -> float:
    bankroll = _get_float(session, BANKROLL_KEY, DEFAULT_BANKROLL)
    units = _get_float(session, BANKROLL_UNITS_KEY, DEFAULT_BANKROLL_UNITS)
    return bankroll / units if units > 0 else 0.0


def get_staking_params(session: Session) -> tuple[float, float, float]:
    """Returns (fractional_kelly, max_stake_fraction, min_edge_to_bet) --
    user-editable overrides of staking.py's module constants."""
    from app.models.staking import FRACTIONAL_KELLY, MAX_STAKE_FRACTION, MIN_EDGE_TO_BET

    return (
        _get_float(session, FRACTIONAL_KELLY_KEY, FRACTIONAL_KELLY),
        _get_float(session, MAX_STAKE_FRACTION_KEY, MAX_STAKE_FRACTION),
        _get_float(session, MIN_EDGE_TO_BET_KEY, MIN_EDGE_TO_BET),
    )


def get_flat_params(session: Session) -> tuple[str, float, float]:
    """Returns (staking_mode, flat_marginal_edge, flat_full_edge) for
    size_stake_dollars. Mode is a string Setting (default "flat"); the edge
    tiers default to staking.py's module constants."""
    from app.models.staking import FLAT_MARGINAL_UNIT_EDGE, FLAT_FULL_UNIT_EDGE

    mode_row = session.get(Setting, STAKING_MODE_KEY)
    mode = mode_row.value if mode_row and mode_row.value else DEFAULT_STAKING_MODE
    return (
        mode,
        _get_float(session, FLAT_MARGINAL_EDGE_KEY, FLAT_MARGINAL_UNIT_EDGE),
        _get_float(session, FLAT_FULL_EDGE_KEY, FLAT_FULL_UNIT_EDGE),
    )
