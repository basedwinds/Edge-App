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
from datetime import datetime, timedelta

import httpx

from app.db.database import SessionLocal
from app.db.models import Market, PlacedBet
from app.ingestion.poller_lock import db_write_lock

log = logging.getLogger("paper_logger")

_BASE = "http://127.0.0.1:8756"
_ALERT_MAX_DAYS_TO_EVENT = 14  # don't Discord-alert on games/matches more than ~2 weeks out
_READINESS_WINDOW_DAYS = 21    # a season-sport's futures are "ready" once its season is within ~3 weeks
# Season-based sports where a futures bet is only worth surfacing once the real
# season is active/near. Event-based sports (tennis/mma/esports/racing) are
# omitted on purpose: their "futures" are tournament-scoped and only listed once
# the event is imminent, so there's no premature-futures problem to gate.
_SEASON_TABLES = {
    "nfl": ("NflGame", "gameday"),
    "nba": ("NbaGame", "gameday"),
    "wnba": ("WnbaGame", "gameday"),
    "mlb": ("MlbGame", "gameday"),
    "soccer": ("SoccerMatch", "match_date"),
}
_MAX_ALERTS_PER_SPORT = 6  # cap Discord pings per sport per run (a slate-open lists hundreds at once)
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


def _cross_platform_key(obj) -> str:
    """Mirror of frontend crossPlatformKey / placed_bets._cross_platform_key:
    the SAME real-world bet is offered on both Kalshi and Polymarket (same
    internal game/match id). This collapses those copies so a copycat is
    announced to Discord only ONCE -- otherwise a user gets pinged twice for one
    bet (real case: Ryuki Matsuda moneyline on both platforms). Works on either a
    Market or a PlacedBet (they share these column names)."""
    gid = None
    for f in _GAME_ID_FIELDS:
        v = getattr(obj, f, None)
        if v:
            gid = f"{f}:{v}" if f in ("valorant_match_id", "cs2_match_id", "lol_match_id") else str(v)
            break
    mt = getattr(obj, "market_type", "") or ""
    if gid:
        line = getattr(obj, "line", None)
        return f"{gid}|{mt}|{getattr(obj, 'team', '') or ''}|{'' if line is None else line}|{getattr(obj, 'side', '') or ''}"
    return f"{getattr(obj, 'sport', '') or ''}|{mt}|{getattr(obj, 'team', '') or ''}"


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


def _sport_season_active(session, sport) -> bool:
    """True if a SEASON-based sport's real season is active or within ~3 weeks --
    i.e. its season-long futures are finally worth surfacing. Excludes
    exhibition/preseason games (NFL 'PRE' etc.): NFL preseason starts ~5 weeks
    before the games that actually decide season futures, so it must NOT open the
    window (a naive "next game" check would be fooled by it). Returns True (fails
    OPEN) for unconfigured sports or on any error, so a real alert is never
    silently dropped."""
    cfg = _SEASON_TABLES.get(sport)
    if cfg is None:
        return True
    from app.db import models as _m

    Model = getattr(_m, cfg[0], None)
    if Model is None:
        return True
    try:
        col = getattr(Model, cfg[1])
        today = datetime.utcnow().date()
        lo = (today - timedelta(days=3)).isoformat()          # "active" = a game in the last few days
        hi = (today + timedelta(days=_READINESS_WINDOW_DAYS)).isoformat()
        q = session.query(Model).filter(col >= lo, col <= hi)
        if hasattr(Model, "game_type"):
            q = q.filter(Model.game_type != "PRE")            # exhibition/preseason doesn't count
        return bool(session.query(q.exists()).scalar())
    except Exception:
        return True


def _within_alert_window(session, bet, season_active=None) -> bool:
    """Gate a Discord PING (never the CLV paper-log itself). Two cases:
      * a bet tied to a real game/match -> only ping if kickoff is within ~2
        weeks. A liquid market weeks out with a big model edge is almost always
        model noise, not an edge -- real case: NFL Week 1 moneyline (MIA @ LV,
        Sept 13) pinged ~7 weeks early with a fake +22pp.
      * a futures/season-long bet (no single game) -> for SEASON sports, only
        ping once the real season is active/near (_sport_season_active);
        event-based sports (tennis/mma/esports/racing) are never gated here.
    Fails OPEN whenever the timing can't be determined, so a real alert is never
    silently dropped. `season_active`, when provided, is a precomputed set of
    ready season-sports (avoids re-querying per futures bet in one run)."""
    from app.models.clv import _game_kickoff_dt, _get_game

    try:
        game = _get_game(session, bet)
    except Exception:
        return True
    if game is None:
        # Futures / season-long: gate season sports on season readiness.
        if bet.sport in _SEASON_TABLES:
            if season_active is not None:
                return bet.sport in season_active
            return _sport_season_active(session, bet.sport)
        return True
    try:
        start = _game_kickoff_dt(game)
        if start is None:
            # Kickoff time not announced yet (e.g. NFL gametime "00:00" on
            # flex-scheduled games): fall back to the DATE so a far-future game
            # is still filtered. Day precision is enough for a 2-week window.
            gd = getattr(game, "gameday", None)
            if gd:
                try:
                    start = datetime.strptime(gd, "%Y-%m-%d")
                except ValueError:
                    start = None
    except Exception:
        return True
    if start is None:
        return True
    return start <= datetime.utcnow() + timedelta(days=_ALERT_MAX_DAYS_TO_EVENT)


def _cap_alerts_per_sport(alerts: list[dict], per_sport: int = _MAX_ALERTS_PER_SPORT) -> list[dict]:
    """Keep only the top-N-by-edge alerts PER SPORT. A market refresh (a whole new
    day's slate listing at once) can newly-qualify dozens of staked bets in a
    single run; without this the Discord message balloons (real case: 360 in one
    ping). The un-alerted ones are still logged for CLV and visible in the app --
    only the ping is trimmed to the best few per sport."""
    from collections import defaultdict

    by_sport: dict[str, list[dict]] = defaultdict(list)
    for a in alerts:
        by_sport[a.get("sport") or "?"].append(a)
    capped: list[dict] = []
    for lst in by_sport.values():
        lst.sort(key=lambda a: -(a.get("edge") or 0))
        capped.extend(lst[:per_sport])
    capped.sort(key=lambda a: -(a.get("edge") or 0))
    return capped


def run_paper_log():
    """Self-HTTP the live prices and persist newly-qualified edges as paper bets
    (sport derived from the Market row so racing's per-series sport is correct).
    Only logs; never raises out to the scheduler."""
    from app.api.routers.settings import get_staking_params, get_alert_config

    rows = _fetch_priced()
    if not rows:
        return
    new_alerts: list[dict] = []  # newly-qualified bets worth pushing to Discord
    with db_write_lock():
        session = SessionLocal()
        try:
            _, _, min_edge = get_staking_params(session)
            alert_cfg = get_alert_config(session)
            open_paper = (
                session.query(PlacedBet)
                .filter(PlacedBet.paper == True, PlacedBet.status == "pending")  # noqa: E712
                .all()
            )
            open_ids = {b.market_id for b in open_paper}
            # Cross-platform keys already being tracked -> don't re-announce the
            # OTHER platform's copy of a bet we've already alerted on (dedup holds
            # across separate runs, not just within one batch).
            alerted_keys = {_cross_platform_key(b) for b in open_paper}
            # Which season-sports are "ready" (season active/near) -> gates their
            # futures alerts. Computed ONCE per run (5 quick queries) rather than
            # per futures bet.
            season_active = {sp for sp in _SEASON_TABLES if _sport_season_active(session, sp)}
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
                # A newly-logged paper bet == a market that JUST cleared the
                # recommendation gate for the first time -> alert-worthy if its
                # edge clears the alert floor AND the app actually sized a real
                # stake for it. The stake requirement is what keeps alerts honest:
                # ~2/3 of edge>=3% markets are thin/barely-traded and get NO stake
                # (a phantom edge vs a near-empty book) -- those are logged for CLV
                # but must not ping. Matches what the Recommended view shows.
                akey = _cross_platform_key(m)
                if (
                    (row.get("edge") or 0) >= alert_cfg["min_edge"]
                    and row.get("suggested_stake_dollars")
                    and akey not in alerted_keys
                    and _within_alert_window(session, bet, season_active)
                ):
                    alerted_keys.add(akey)  # so the sibling platform in this same run is skipped too
                    new_alerts.append({
                        "sport": m.sport or "nfl", "market_type": m.market_type,
                        "team": m.team, "line": m.line, "side": m.side, "source": m.source,
                        "edge": row.get("edge"), "model": row.get("model_prob"),
                        "market": row.get("implied_prob"), "stake": row.get("suggested_stake_dollars"),
                    })
            session.commit()
            log.info("paper logger: added %d new paper bets (%d already open)", added, len(open_ids) - added)
        except Exception:
            log.exception("paper log write failed")
        finally:
            session.close()

    # Push new-recommendation alerts OUTSIDE the write lock (network I/O). No-op
    # unless a Discord webhook is configured + there are new alert-worthy bets.
    # Cap per sport so a slate-open refresh can't spam a 300-line ping.
    new_alerts = _cap_alerts_per_sport(new_alerts)
    if new_alerts and alert_cfg.get("webhook_url"):
        try:
            _send_recommendation_alert(alert_cfg["webhook_url"], new_alerts)
        except Exception:
            log.exception("recommendation alert send failed")


def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _send_recommendation_alert(webhook_url: str, alerts: list[dict]) -> None:
    from app.clients.discord_notify import send_discord

    alerts = sorted(alerts, key=lambda a: -(a.get("edge") or 0))
    header = f"🔔 {len(alerts)} new recommended bet{'s' if len(alerts) != 1 else ''}"
    lines = [header]
    for a in alerts[:15]:  # cap the message; more are in the app
        who = a.get("team") or a.get("market_type")
        edge = a.get("edge")
        edge_s = f"+{edge * 100:.1f}pp" if edge is not None else "?"
        stake = a.get("stake")
        stake_s = f" · ${stake:.0f}" if stake else ""
        lines.append(
            f"• [{a['sport'].upper()}] {who} — {a['market_type']} "
            f"(model {_fmt_pct(a.get('model'))} vs mkt {_fmt_pct(a.get('market'))}, {edge_s}){stake_s} · {a.get('source', '')}"
        )
    if len(alerts) > 15:
        lines.append(f"…and {len(alerts) - 15} more in the app.")
    send_discord(webhook_url, "\n".join(lines))
