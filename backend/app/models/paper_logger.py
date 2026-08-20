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
  * One paper bet per PROPOSITION (dedup), logged the first time it appears with
    edge -- the earliest honest entry, giving the line the most room to move.
    Per-proposition, not per-market: until 2026-08-20 this deduped on market_id,
    so Kalshi's and Polymarket's copies of one bet were logged twice. They
    resolve on the same event, so they are perfectly correlated and every
    downstream statistic counted them as independent (up to 1.66x on LoL,
    inflating z-scores by ~29%). "More sample is better" is only true of
    INDEPENDENT sample; more correlated rows just print a smaller error bar under
    the same information. See the logged_keys block below for the measurement.
  * Match markets only (the /markets endpoints); futures CLV is "not_applicable"
    (no single close), so they're skipped for now.
"""
import logging

from app.models.staking import EXTREME_MARKET_PRICE, IMPLAUSIBLE_EDGE

from app import sports as app_sports
from datetime import datetime, timedelta
from types import SimpleNamespace

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
# DERIVED from app.sports (a sport declares its own season_table there).
_SEASON_TABLES = dict(app_sports.SEASON_TABLES)
_MAX_ALERTS_PER_SPORT = 6  # cap Discord pings per sport per run (a slate-open lists hundreds at once)
# DERIVED from app.sports, not retyped. This list is what the alerts and the
# paper logger read, and CFB was once missing from it -- the sport simply never
# alerted and never accrued CLV, with nothing to indicate a problem.
_ENDPOINTS = list(app_sports.MARKETS_PATHS)
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
    from app.shutdown import is_shutting_down

    out: list[dict] = []
    try:
        with httpx.Client(timeout=90.0) as client:
            for ep in _ENDPOINTS:
                if is_shutting_down():  # see app/shutdown.py -- unkillable worker
                    break
                try:
                    r = client.get(f"{_BASE}{ep}")
                    if r.status_code == 200:
                        out.extend(r.json())
                except Exception:
                    log.exception("paper log fetch failed for %s", ep)
    except Exception:
        log.exception("paper log http client failed")
    return out


# A book spanning this much of the probability range has not told us what
# anything is worth -- its mid is the midpoint of ignorance, not a price.
# Chosen from live data rather than picked round: at 0.50, of the rows it
# removes 76% are priced within 5pp of 0.50 (the placeholder signature) against
# just 15% of the rows it keeps -- a 5x enrichment in exactly the thing being
# targeted. Tighter thresholds catch too little (>=0.95 removes only 6% and
# misses most of it); looser ones start cutting genuinely quoted thin markets
# without improving that ratio.
MAX_QUOTED_SPREAD = 0.50


# The harness deliberately logs BELOW the recommend gate.
#
# min_edge_to_bet was raised to 10pp so the recommended list stops spending
# ceiling on bets that don't pay -- measured: 3-5pp returns +1.6% and 5-8pp
# +1.1%, i.e. break-even, while 12-20pp returns +16.9% and 20-35pp +41.8%. The
# sub-10pp rows were also sized LARGER on average ($8.10 vs $7.32), so they were
# displacing capital, not merely cluttering the list.
#
# But that measurement only exists BECAUSE the harness logged those bands. If
# this followed the recommend gate up, the app would immediately go blind to the
# region it just learned about and could never notice the threshold drifting
# wrong in either direction. Recommend narrowly; measure broadly.
PAPER_MIN_EDGE = 0.03

_SOCCER_LEAGUE_LABEL = {
    "E0": "EPL", "SP1": "La Liga", "I1": "Serie A", "D1": "Bundesliga", "F1": "Ligue 1",
    "P1": "Liga Portugal", "N1": "Eredivisie", "E1": "EFL Championship",
    "SP2": "La Liga 2", "I2": "Serie B", "D2": "2. Bundesliga", "F2": "Ligue 2", "MLS": "MLS",
}


def _league_for_row(row: dict, m) -> "str | None":
    """The competition a paper bet belongs to, for the tracker's league column.

    PlacedBet.league was NEVER SET here, which is why 16,324 of 19,692 bets --
    every paper bet ever logged -- carry a null league and the tracker falls back
    to the bare sport. The manual mark-placed path does supply it, so the gap was
    invisible unless you looked at the paper rows, which are most of them.

    Read off the API row the recommendation already came from, so this costs no
    extra queries: each sport router already returns the field that identifies
    the competition. "League" means different things per sport, and the right
    answer is whatever a human would name:

      soccer   division code -> readable name (E0 -> "EPL")
      mma      the CARD ("UFC 320"), which is MMA's unit of competition
      esports  the tournament/event name, NOT the matchup (group_label is often
               the matchup, which is exactly how match names ended up in the
               league column)
      racing   the resolved series ("NASCAR Xfinity Series") -- the only thing
               distinguishing Cup from Xfinity from Truck, since all three
               arrive as sport="nascar"
      tennis   tour + tier ("ATP Challenger"), since a Tour match and an ITF
               match are otherwise indistinguishable

    Team sports (nfl/nba/mlb/wnba/cfb) return None on purpose: the sport IS the
    league there, and repeating it in both columns is noise.
    """
    sport = (m.sport or "").lower()
    if sport == "soccer":
        lg = row.get("league")
        return _SOCCER_LEAGUE_LABEL.get(lg, lg) if lg else None
    if sport in ("mma", "cs2", "valorant", "lol"):
        return row.get("event_name")
    if sport in ("f1", "irl", "nascar"):
        return row.get("series_label")
    if sport == "tennis":
        tour, tier = (row.get("tour") or "").lower(), row.get("tier")
        women = tour == "wta"
        if tier == "itf":
            return "ITF Women" if women else "ITF Men"
        if tier == "challenger":
            return "WTA 125" if women else "ATP Challenger"
        if tier == "tour":
            return "WTA Tour" if women else "ATP Tour"
        return tour.upper() or None
    return None


def _qualifies(row: dict, min_edge: float) -> bool:
    """Log a row if the app would bet it. Two ways in: it already has a sized
    stake (the staked sports' recommend gate), OR it's a tracking-only market
    (racing, futures) with a real positive edge >= min_edge and a real market
    price -- so unstaked-but-edged markets still accrue forward CLV (the whole
    point: everything unvalidated, let CLV judge uniformly)."""
    # SPREAD GATE, applied before anything else -- including staked rows.
    #
    # This is the second CLV contamination mode, and the one the earlier
    # volume-based attempt could not address. All 37 of soccer's closed CLV rows
    # were logged at 0.49/0.50 on markets with no real quote; a genuine price
    # appeared later at 0.80-0.96, reading as +20.68pp average CLV, individual
    # rows to +47pp. An extremeness test cannot see it, since 0.50 is the least
    # extreme price there is.
    #
    # It only became fixable once Polymarket bid/ask were actually stored (they
    # were hardcoded None for every sport until 2026-08-04). The first attempt
    # gated on "has a quote or has volume" and was measured as far too blunt --
    # it cut logging 774 -> 158 on tennis, because NO Polymarket row had a quote
    # and volume is legitimately absent on most untraded markets. Spread is the
    # right discriminator: it says whether the market has an opinion, where
    # volume only says whether anyone has acted on one.
    #
    # Deliberately applied to STAKED rows too. The question here is not "would we
    # bet this" but "is this price real enough to measure CLV against", and a
    # degenerate book fails that regardless of staking. The cost is tiny and
    # measured: 2 of 374 currently-staked rows. Both are worth a second look in
    # their own right -- e.g. a real $20 LoL recommendation on bid 0.08 / ask
    # 0.93, whose "price" of 0.505 is meaningless.
    bid, ask = row.get("yes_bid"), row.get("yes_ask")
    if bid is not None and ask is not None and (ask - bid) >= MAX_QUOTED_SPREAD:
        return False
    if row.get("suggested_stake_dollars"):
        return True
    edge = row.get("edge")
    if edge is None or edge < min_edge or row.get("implied_prob") is None:
        return False
    # THE SECOND DOOR MUST NOT READMIT WHAT THE STAKING GUARD REFUSED.
    #
    # staking.kelly_fraction already rejects a huge disagreement with an
    # extremely-priced market as implausible rather than an edge. But this
    # branch only looked at `edge`, so a market priced at 0.5% that the model
    # called 50% had no stake (guard fired) yet sailed in here on its +48pp
    # "edge" -- the exact rows the guard exists to disbelieve.
    #
    # This was not harmless bookkeeping. It poisoned the ONE validation signal
    # this app has. Measured 2026-08-03: 969 tennis rows logged at a market
    # price under 5% (median claimed edge +48.4pp, max +99.7pp), 968 of them
    # paper, still arriving at ~360/day. In the CLV report those 449 closed rows
    # averaged +44.39pp against +0.58pp for normally-priced bets, dragging the
    # whole tennis figure to +13.84pp -- versus +1.57pp for MLB, whose markets
    # are liquid and whose start times are reliable. A near-empty market
    # quoting anything later reads as enormous CLV, and the dormant
    # CLV-selection gate would have learned to trust precisely those buckets.
    #
    # Tracking-only markets (racing, futures) still come through: they are
    # unstaked by design, not because a guard disbelieved them.
    price = row.get("implied_prob")
    model = row.get("model_prob")
    if model is not None and price is not None:
        extreme = price <= EXTREME_MARKET_PRICE or price >= 1 - EXTREME_MARKET_PRICE
        if extreme and abs(model - price) >= IMPLAUSIBLE_EDGE:
            return False
    # THE SECOND CONTAMINATION MODE -- now filtered, on the condition the earlier
    # decision named.
    #
    # A market with no bid, no ask and no volume has a `last_price` that is a
    # PLACEHOLDER, not a price. Logging against it invents an edge: sampled 400
    # tennis bets logged at an entry of <=0.5% and 400 of 400 had no quote on
    # either side and zero volume, against a median model probability of 0.489.
    # That is a ~48pp "edge" over a number nobody ever offered. 978 of 5,327
    # tennis paper bets (18%) are this shape, and they are what makes the tennis
    # CLV buckets 76% contaminated -- by far the worst in the app.
    #
    # This filter was written, measured and REJECTED once before, correctly: at
    # the time Polymarket supplied no bid/ask at all and almost no volume, so the
    # rule cut tennis logging 774 -> 158 and would have silenced the harness to
    # fix a subset of it. That rejection was explicitly conditional -- "the real
    # defect is upstream ... fix that, and this filter becomes both cheap and
    # safe."
    #
    # Polymarket volume ingestion HAS since been fixed, so it was re-measured
    # rather than assumed. Today the rule keeps 100% of candidates in soccer,
    # LoL, Valorant, CS2 and MMA (where it would previously have gutted them),
    # and the rows it still drops are the genuinely unquoted ones: 60% of tennis
    # and 43% of MLB candidates have no quote AND no volume. Tennis keeps ~582
    # candidates and already has 2,903 settled samples, so nothing is silenced.
    bid, ask = row.get("yes_bid"), row.get("yes_ask")
    if bid is None and ask is None and not (row.get("volume") or 0) > 0:
        return False
    return True


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
    # The EXACT set the Recommended tab shows (verified byte-identical to the
    # frontend builders on frozen snapshots -- see models/recommended.py). Alerts
    # fire only for markets in this set, so a ping always corresponds to a row the
    # user can actually find in the app. Falls back to an empty set on failure,
    # which just means "no alerts this run" (paper logging still proceeds).
    try:
        from app.api.routers.settings import _read_all  # settings dict for pool sizes
        from app.models.recommended import compute_recommended

        with SessionLocal() as _s:
            _settings = _read_all(_s).model_dump()
        rec_failures: list = []
        recommended_ids = {r.id for r in compute_recommended(_settings, failures=rec_failures)}
        if rec_failures:
            # A PARTIAL set must not be used to say "not recommended" -- see
            # PlacedBet.was_recommended. Discard it for membership purposes;
            # alerts still fire on what was computed.
            log.warning("recommended set is PARTIAL (%d endpoints failed: %s) -- "
                        "was_recommended will be recorded as unknown this run",
                        len(rec_failures), rec_failures[:4])
    except Exception:
        log.exception("recommended-set computation failed; skipping alerts this run")
        recommended_ids = set()
        rec_failures = ["<computation raised>"]
    new_alerts: list[dict] = []  # newly-qualified bets worth pushing to Discord
    with db_write_lock():
        session = SessionLocal()
        try:
            min_edge = PAPER_MIN_EDGE   # NOT the recommend gate -- see PAPER_MIN_EDGE
            alert_cfg = get_alert_config(session)
            open_paper = (
                session.query(PlacedBet)
                .filter(PlacedBet.paper == True, PlacedBet.status == "pending")  # noqa: E712
                .all()
            )
            # A market gets ONE paper bet, ever -- not one per open slot.
            #
            # REAL BUG (2026-08-06): this used to gate on the PENDING set alone,
            # so the moment a paper bet settled its market became loggable
            # again. If the market still qualified, the next run logged a fresh
            # bet, the settler graded it seconds later off the already-known
            # result, and the cycle repeated every poll. One F1 podium market
            # accumulated 120 bets, 118 of them placed AFTER the race had run
            # and settled within 0-3 minutes each; F1 as a whole reached a 79x
            # duplication factor (3,327 rows, 42 distinct picks).
            #
            # It stayed latent until this session's settlement work made racing
            # bets gradeable at all -- before that they sat pending, so the old
            # guard happened to hold. Fixing settlement turned a dormant bug
            # into an active loop, which is exactly the kind of interaction that
            # only shows up in the data.
            #
            # A post-result "bet" is not an observation: it is logged knowing
            # the outcome, and it inflates any win-rate or ROI computed by
            # counting rows. Gating on EVERY paper bet for the market -- settled
            # or not -- is correct, because a Market row is one real market for
            # one real event and never legitimately needs a second entry.
            logged_ids = {
                mid for (mid,) in session.query(PlacedBet.market_id)
                .filter(PlacedBet.paper == True)  # noqa: E712
                .distinct().all()
            }
            open_ids = logged_ids
            # Cross-platform keys already being tracked -> don't re-announce the
            # OTHER platform's copy of a bet we've already alerted on (dedup holds
            # across separate runs, not just within one batch).
            alerted_keys = {_cross_platform_key(b) for b in open_paper}
            # THE SIBLING PLATFORM'S COPY IS NOT A SECOND OBSERVATION.
            #
            # Dedup above is per MARKET id, so Kalshi's and Polymarket's copies
            # of one proposition were logged as two paper bets. They resolve on
            # the same real event, so they win and lose together -- perfectly
            # correlated rows that every downstream statistic counts as
            # independent.
            #
            # Measured 2026-08-20, settled paper rows per distinct proposition:
            #     lol 1.66x  cs2 1.59x  valorant 1.53x  mma 1.31x  mlb 1.21x
            #     tennis 1.19x  soccer 1.12x  wnba 1.06x  nascar 1.00x
            # Effective n is the DISTINCT count, so every z-score taken off raw
            # paper rows was inflated by ~sqrt(factor) -- 1.66x on LoL is a 29%
            # overstatement of confidence. It nearly cost a wrong call: an MMA
            # finding read z=-3.20 across 72 rows and z=-2.04 across the 37
            # DISTINCT fights those rows actually covered.
            #
            # THIS FILE'S OWN DESIGN NOTE SAID THE OPPOSITE -- "more sample is
            # better and nothing is at risk". More CORRELATED sample is not more
            # information; it is the same information with a smaller error bar
            # printed under it, which is worse than having less data.
            #
            # Cross-platform PRICE comparison is not lost: that is
            # cross_platform_divergence.py's job and it reads live markets, not
            # this log. Alerts already deduped on this exact key (below) -- only
            # logging did not.
            #
            # Scoped by SPORT because the key renders non-esports game ids as a
            # bare number, so tennis_match_id 829 and soccer_match_id 829 would
            # otherwise collide and silently drop a real observation.
            _key_names = ("market_type", "team", "line", "side", "sport", *_GAME_ID_FIELDS)
            logged_keys = {
                (v[4], _cross_platform_key(SimpleNamespace(**dict(zip(_key_names, v)))))
                for v in session.query(
                    PlacedBet.market_type, PlacedBet.team, PlacedBet.line,
                    PlacedBet.side, PlacedBet.sport,
                    *[getattr(PlacedBet, f) for f in _GAME_ID_FIELDS],
                ).filter(PlacedBet.paper == True).all()  # noqa: E712
            }
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
                # Same proposition already logged from the other book -- see
                # logged_keys above.
                log_key = (m.sport, _cross_platform_key(m))
                if log_key in logged_keys:
                    open_ids.add(mid)   # so the run's "already logged" count stays honest
                    continue
                bet = PlacedBet(
                    # Recorded AT LOG TIME from the set already computed above
                    # for alerts. It cannot be derived later: the recommended
                    # set depends on pools, open bets and prices as they were at
                    # this instant. See PlacedBet.was_recommended for why the
                    # distinction matters to every number this record produces.
                    #
                    # `recommended_ids` is empty when that computation FAILED
                    # (it is wrapped in its own try/except so a failure only
                    # costs alerts, not logging). Writing False in that case
                    # would be a lie -- it would mark every bet in the run as
                    # not-recommended -- so an empty set records None instead.
                    was_recommended=(m.id in recommended_ids)
                    if (recommended_ids and not rec_failures) else None,
                    market_id=m.id,
                    market_type=m.market_type,
                    source=m.source,
                    sport=m.sport or "nfl",
                    team=m.team,
                    line=m.line,
                    side=m.side,
                    label=m.group_label or f"{m.sport} {m.market_type}",
                    league=_league_for_row(row, m),
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
                logged_keys.add(log_key)   # blocks the sibling book within this same run
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
                    m.id in recommended_ids          # in the Recommended tab, exactly
                    and akey not in alerted_keys     # not already announced (either book)
                ):
                    alerted_keys.add(akey)  # so the sibling platform in this same run is skipped too
                    new_alerts.append({
                        "sport": m.sport or "nfl", "market_type": m.market_type,
                        "team": m.team, "line": m.line, "side": m.side, "source": m.source,
                        "label": m.group_label,   # e.g. "PHI @ BAL" -> tells you WHICH game
                        "edge": row.get("edge"), "model": row.get("model_prob"),
                        "market": row.get("implied_prob"), "stake": row.get("suggested_stake_dollars"),
                    })
            session.commit()
            log.info("paper logger: added %d new paper bets (%d markets already logged, skipped)",
                     added, len(open_ids) - added)
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


_MARKET_LABELS = {
    "moneyline": "Moneyline", "moneyline_3way": "Moneyline", "spread": "Spread",
    "game_spread": "Spread", "total": "Total", "game_total": "Total",
    "team_total": "Team Total", "f5": "First 5 Innings", "rfi": "Run in 1st Inning",
    "series_winner": "Series Winner", "map_winner": "Map Winner",
    "series_handicap": "Map Handicap", "series_total": "Total Maps",
    "set_winner": "Set Winner", "set_total": "Total Sets", "exact_score": "Exact Score",
    "distance": "Goes the Distance", "rounds": "Round of Finish",
    "method_of_finish": "Method of Finish", "btts": "BTTS",
    "race_winner": "Race Winner", "top_n": "Top Finish", "pole": "Pole",
    "h2h": "Head-to-Head", "constructor_pole": "Constructor Pole",
}


def _describe_pick(a: dict) -> str:
    """The actual thing to bet, spelled out. The old format printed
    `team or market_type` and dropped `line`/`side` entirely, so a total rendered
    as the useless "total — total" with no number -- you couldn't tell WHAT to
    bet (user-reported 2026-08-02). line/side were already in the payload, just
    never used."""
    team, line, side = a.get("team"), a.get("line"), a.get("side")
    mt = a.get("market_type") or ""
    parts = []
    if team:
        parts.append(str(team))
    if side and str(side).lower() not in ("yes", "no"):
        parts.append(str(side).title())          # Over / Under / Home / Draw ...
    if line is not None:
        # whole numbers read better without the trailing .0 (map 2, not map 2.0)
        num = str(int(line)) if float(line) == int(line) else str(line)
        # map_winner's "line" is a MAP NUMBER, not a handicap -- label it so
        # "Verdant 2" doesn't read like a 2-map spread (same convention the
        # tracker already uses).
        parts.append(f"Map {num}" if mt == "map_winner" else num)
    if not parts:
        parts.append("Yes")                      # binary market with no team/line
    return " ".join(parts)


def _send_recommendation_alert(webhook_url: str, alerts: list[dict]) -> None:
    from app.clients.discord_notify import send_discord

    alerts = sorted(alerts, key=lambda a: -(a.get("edge") or 0))
    header = f"🔔 {len(alerts)} new recommended bet{'s' if len(alerts) != 1 else ''}"
    lines = [header]
    for a in alerts[:15]:  # cap the message; more are in the app
        edge = a.get("edge")
        edge_s = f"+{edge * 100:.1f}pp" if edge is not None else "?"
        stake = a.get("stake")
        stake_s = f" · ${stake:.0f}" if stake else ""
        market = _MARKET_LABELS.get(a.get("market_type") or "", a.get("market_type") or "")
        game = a.get("label")                    # e.g. "PHI @ BAL" -- context for which game
        game_s = f"{game} — " if game else ""
        lines.append(
            f"• [{a['sport'].upper()}] {game_s}{market}: {_describe_pick(a)} "
            f"(model {_fmt_pct(a.get('model'))} vs mkt {_fmt_pct(a.get('market'))}, {edge_s}){stake_s} · {a.get('source', '')}"
        )
    if len(alerts) > 15:
        lines.append(f"…and {len(alerts) - 15} more in the app.")
    send_discord(webhook_url, "\n".join(lines))
