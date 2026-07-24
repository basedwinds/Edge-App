"""Auto paper-trading logger: the forward-CLV validation harness.

Every scheduled run, this snapshots the app's own edge-qualified recommendations
(any market the routers already sized a stake for -- i.e. it cleared the min-edge
+ real-trading gate) into the PlacedBet table flagged `paper=True`, recording the
ENTRY price/model_prob/edge. No real money moves.

Why it exists: this whole app's honest thesis is "no model beats the market on
average; the only proof of a real edge is beating the CLOSING line, forward." But
until now nothing recorded a bet, so there were 0 data points and the CLV machinery
(clv.py + clv_selection.py) sat inert. Paper bets flow through compute_bet_clv and
the CLV buckets EXACTLY like real bets, so after a few weeks /clv-buckets finally
answers, per (sport, market_type): are we consistently getting a better price than
the close? That's the number no backtest can give us for thin/short-retention
markets (racing especially -- it can't be historically backtested at all).

Design choices:
  * Reuses the live pricing by self-HTTPing each sport's /markets endpoint (same
    pattern as the cache warmer) rather than re-deriving model_prob in here.
  * Logs EVERY edge+trading-qualified market, NOT the portfolio-capped subset --
    the cap is a real-bankroll concern; for a paper CLV study more sample is
    better and nothing is at risk. Structural fields (game id, side, line) come
    from the Market row so the bet is CLV-computable.
  * One open paper bet per market (dedup), logged the first time a market appears
    with edge -- the earliest honest entry, giving the line the most room to move.
  * Match markets only (the /markets endpoints); futures CLV is "not_applicable"
    (no single close), so they're skipped for now.
"""
import logging

import httpx

from app.db.database import SessionLocal
from app.db.models import Market, PlacedBet
from app.ingestion.poller_lock import db_write_lock

log = logging.getLogger("paper_logger")

_BASE = "http://127.0.0.1:8756"
_ENDPOINTS = [
    "/markets", "/nba/markets", "/wnba/markets", "/mlb/markets", "/mma/markets",
    "/tennis/markets", "/soccer/markets", "/valorant/markets", "/cs2/markets",
    "/lol/markets", "/racing/markets",
]
# Copied straight across from the Market row -- PlacedBet uses the identical
# column names, so a game/race-tied bet stays CLV-computable (compute_bet_clv
# looks these up to find the closing snapshot at kickoff).
_GAME_ID_FIELDS = [
    "nfl_game_id", "nba_game_id", "wnba_game_id", "mlb_game_id", "mma_fight_id",
    "tennis_match_id", "soccer_match_id", "valorant_match_id", "cs2_match_id",
    "lol_match_id", "race_event_id",
]


def _fetch_priced() -> list[dict]:
    out: list[dict] = []
    try:
        with httpx.Client(timeout=90.0) as client:
            for ep in _ENDPOINTS:
                try:
                    r = client.get(f"{_BASE}{ep}")
                    if r.status_code == 200:
                        out.extend(r.json())
                except Exception:
                    log.exception("paper log fetch failed for %s", ep)
    except Exception:
        log.exception("paper log http client failed")
    return out


def _qualifies(row: dict, min_edge: float) -> bool:
    """Log a row if the app would bet it. Two ways in: it already has a sized
    stake (the staked sports' recommend gate), OR it's a tracking-only market
    (racing, futures) with a real positive edge >= min_edge and a real market
    price -- so unstaked-but-edged markets still accrue forward CLV (the whole
    point: everything unvalidated, let CLV judge uniformly)."""
    if row.get("suggested_stake_dollars"):
        return True
    edge = row.get("edge")
    return edge is not None and edge >= min_edge and row.get("implied_prob") is not None


def run_paper_log():
    """Self-HTTP the live prices and persist newly-qualified edges as paper bets
    (sport derived from the Market row so racing's per-series sport is correct).
    Only logs; never raises out to the scheduler."""
    from app.api.routers.settings import get_staking_params

    rows = _fetch_priced()
    if not rows:
        return
    with db_write_lock():
        session = SessionLocal()
        try:
            _, _, min_edge = get_staking_params(session)
            open_ids = {
                b.market_id
                for b in session.query(PlacedBet)
                .filter(PlacedBet.paper == True, PlacedBet.status == "pending")  # noqa: E712
                .all()
            }
            added = 0
            for row in rows:
                if not _qualifies(row, min_edge):
                    continue
                mid = row.get("id")
                if mid is None or mid in open_ids:
                    continue
                m = session.get(Market, mid)
                if m is None:
                    continue
                bet = PlacedBet(
                    market_id=m.id,
                    market_type=m.market_type,
                    source=m.source,
                    sport=m.sport or "nfl",
                    team=m.team,
                    line=m.line,
                    side=m.side,
                    label=m.group_label or f"{m.sport} {m.market_type}",
                    stake_pool=row.get("stake_pool") or "weekly",
                    stake_dollars=row.get("suggested_stake_dollars") or 0.0,
                    stake_units=row.get("suggested_stake_units"),
                    market_prob_at_placement=row.get("implied_prob"),
                    model_prob_at_placement=row.get("model_prob"),
                    edge_at_placement=row.get("edge"),
                    status="pending",
                    paper=True,
                )
                for f in _GAME_ID_FIELDS:
                    setattr(bet, f, getattr(m, f, None))
                session.add(bet)
                open_ids.add(mid)
                added += 1
            session.commit()
            log.info("paper logger: added %d new paper bets (%d already open)", added, len(open_ids) - added)
        except Exception:
            log.exception("paper log write failed")
        finally:
            session.close()
