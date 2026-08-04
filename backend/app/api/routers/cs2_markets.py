"""CS2 markets API -- parallel to routers/valorant_markets.py.

Real live inventory here (confirmed 2026-07-19, see kalshi_cs2_client.py) is
series (whole-match) winner + total maps played -- map winner and
tournament winner futures are ready in code but currently have zero open
Kalshi markets.

Ratings are trained on a real historical liquipedia.net crawl (8,839
matches, 94 S-Tier + A-Tier tournaments, Oct 2023-Jul 2026 -- see
scripts/build_cs2_match_cache.py) plus this app's own live-polled match
history on top (see elo_service_cs2.py). K=32 is grid-searched against that
real data (scripts/derive_cs2_elo_constants.py -- 60.75% walk-forward
accuracy post-warmup, beats the naive 0.5 baseline). model_validated is
still False for every market_type here -- a real market-odds backtest
against Kalshi's own historical trade data now exists too
(scripts/backtest_cs2_market_odds.py, 85-match sample) and found the market
beats the model, same conclusion every sport in this app has found.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice
from app.api.routers.settings import get_cs2_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import Cs2MarketOut, FuturesMarketOut, ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.clients import flashscore_esports_client
from app.db.chunked import fetch_in_chunks
from app.db.models import Cs2Match, Market, MarketSnapshot
from app.ingestion import market_catalog_cs2
from app.ingestion.market_matcher_cs2 import team_names_match
from app.models.baseline import elo_service_cs2
from app.models.esports_tournament_pricing import price_tournament_winners
from app.models.tournament_sim_esports import TOURNAMENT_SIM_NOTE
from app.models.ladder_sanity import futures_group_decided, CS2_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA, ESPORTS_LIVE_TRADING_MIN_PRICE_SWING, looks_already_live_by_trading
from app.models.staking import FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

_NO_BASELINE_METHODOLOGY = "No detailed methodology available for this market type yet -- see the module docstring above."

router = APIRouter(prefix="/cs2", tags=["cs2"])

# "series_handicap" added 2026-08-02 alongside CS2's first real handicap
# inventory. It was absent for an honest reason -- Kalshi lists no CS2 handicap
# series at all, so until Polymarket ingestion existed there was nothing to
# price. elo_cs2.SeriesDistribution has carried prob_handicap_cover_a/b the
# whole time (identical to Valorant's/LoL's), so this needed no model work,
# only the routing Valorant's own router already had.
GAME_MARKET_TYPES = {"map_winner", "series_winner", "series_handicap", "series_total"}

NO_BASELINE_REASON = (
    "No baseline yet -- this market's model is still being built and validated against this app's "
    "own historical data, not shipped as a guessed number."
)

COLD_START_CAVEAT = (
    "Ratings are trained on a real historical liquipedia.net crawl (8,839 matches, 94 S-Tier + A-Tier "
    "tournaments, Oct 2023-Jul 2026) plus this app's own live-polled matches on top -- 60.75% "
    "walk-forward accuracy post-warmup, beats the naive 0.5 baseline. A real market-odds backtest "
    "against Kalshi's own historical trade data (85-match sample) found the market beats the model, "
    "so model_validated stays false regardless."
)


def _team_side(match: Cs2Match | None, team_name: str | None) -> str | None:
    if match is None or not team_name:
        return None
    if team_names_match(team_name, match.team_a):
        return "team_a"
    if team_names_match(team_name, match.team_b):
        return "team_b"
    return None


def _game_model_prob(m: Market, match: Cs2Match | None) -> float | None:
    if match is None or not match.best_of:
        return None
    dist = elo_service_cs2.get_series_distribution(
        match.team_a, match.team_b, match.best_of,
        match_date=match.estimated_start_time or match.match_date,
    )
    if dist is None:
        return None
    if m.market_type == "series_total":
        return round(dist.prob_total_maps_over(m.line), 4) if m.line is not None else None

    side = _team_side(match, m.team)
    if side is None:
        return None
    if m.market_type == "map_winner":
        if m.line is None:
            return None
        map_p = dist.prob_map_n_win_a(int(m.line))
        if map_p is None:
            return None
        return round(map_p if side == "team_a" else (1.0 - map_p), 4)
    if m.market_type == "series_winner":
        p = dist.prob_series_win_a() if side == "team_a" else dist.prob_series_win_b()
        return round(p, 4)
    if m.market_type == "series_handicap":
        if m.line is None:
            return None
        p = dist.prob_handicap_cover_a(m.line) if side == "team_a" else dist.prob_handicap_cover_b(m.line)
        return round(p, 4)
    return None


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_cs2_futures(session: Session = Depends(get_session)):
    """CS2's own tournament_winner futures. Priced (2026-07-23) by the Elo-
    seeded single-elim Monte Carlo (esports_tournament_pricing.py) off the field
    named by the markets themselves -- model_prob/edge are shown so the numbers
    are visible and can be tracked against the market.

    STAKED as of 2026-08-02, having previously been tracking-only. The bracket is
    still an APPROXIMATION (real CS2 events are double-elim/Swiss and this app
    has no real draw for them) -- but not staking it meant it never became a
    paper bet, never accrued forward CLV, and could never be shown right or
    wrong. Since forward CLV is the only thing this app trusts, the approximate
    model is the one that most needs measuring. It is badged in the UI instead,
    and the CLV-selection gate can retire it on real data. Season-long aggregate
    markets (e.g. a whole-year "win an
    international") stay unpriced -- they're not a single bracket at all."""
    markets = session.query(Market).filter(Market.sport == "cs2", Market.market_type == "tournament_winner", Market.status == "active").all()
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    # Drop groups whose tournament is already won. Kalshi still reports every leg
    # `active` long after the event decides, so status alone cannot catch it --
    # the BLAST Bounty 2026 Season 2 Finals had MOUZ at 0.995 with 31 dead legs
    # still listed and $5 stakes recommended on 0.5% longshots.
    _by_group = {}
    for _m in markets:
        _p = _implied_prob(snapshots_by_market.get(_m.id))
        _by_group.setdefault(_m.group_label or "", []).append(_p)
    _decided = {g for g, ps in _by_group.items() if futures_group_decided("tournament_winner", ps)}
    if _decided:
        markets = [m for m in markets if (m.group_label or "") not in _decided]
        # Hoisted: as an inline set literal this was rebuilt once per
        # snapshots_by_market entry -- quadratic, and the dominant cost of the
        # tennis endpoint at 34k markets (183M attribute reads, ~40s).
        _kept_market_ids = {m.id for m in markets}
        snapshots_by_market = {k: v for k, v in snapshots_by_market.items() if k in _kept_market_ids}

    priced = price_tournament_winners(markets, elo_service_cs2)
    # STAKED, not tracking-only, as of 2026-08-02. These were hardcoded to
    # kelly_fraction=None on the reasoning that the bracket is an approximation.
    # That reasoning was inverted: the paper logger only records rows the app
    # actually staked, so suppressing them meant they never became paper bets,
    # never accrued forward CLV, and could never be evaluated -- guaranteeing the
    # approximation could never be proven right OR wrong. Since forward CLV is
    # the only thing this app trusts, an approximate model is the one that most
    # needs measuring. They are badged approximate in the UI instead, and the
    # CLV-selection gate can retire them once the data speaks.
    _weekly, _futures_pool = get_cs2_pool_dollars(session)
    _unit = get_unit_dollars(session)
    _fk, _msf, _mineg = get_staking_params(session)
    _mode, _fm, _ff = get_flat_params(session)
    _clv = bucket_clv_stats(session)

    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        model_prob = priced.get(m.id)
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        _traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        _kelly = gate_kelly(
            kelly_fraction(model_prob, implied, _fk, _msf, _mineg, _traded, snap.yes_ask if snap else None),
            _clv, "cs2", m.market_type,
        )
        _stake = size_stake_dollars(_mode, _kelly, _futures_pool, model_prob, implied, _unit, _fm, _ff, unit_scale=FUTURES_UNIT_SCALE)
        out.append(
            FuturesMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                group_label=m.group_label,
                line=m.line,
                side=m.side,
                implied_prob=implied,
                yes_bid=snap.yes_bid if snap else None,
                yes_ask=snap.yes_ask if snap else None,
                last_price=snap.last_price if snap else None,
                volume=snap.volume if snap else None,
                updated_at=m.updated_at.isoformat() if m.updated_at else None,
                model_prob=model_prob,
                model_validated=False,
                edge=edge,
                kelly_fraction=_kelly,
                suggested_stake_dollars=_stake,
                suggested_stake_units=round(_stake / _unit, 3) if (_stake is not None and _unit > 0) else None,
                stake_pool="futures",
                line_move_pp=None,
                model_note=TOURNAMENT_SIM_NOTE if model_prob is not None else None,
            )
        )
    out.sort(key=lambda m: (m.group_label or "", -(m.model_prob or 0), m.team or ""))
    return out


@router.get("/markets", response_model=list[Cs2MarketOut])
def list_cs2_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "cs2", Market.market_type.in_(GAME_MARKET_TYPES | {"tournament_winner"})).all()
    match_ids = {m.cs2_match_id for m in markets if m.cs2_match_id}
    matches_by_id = {mt.id: mt for mt in session.query(Cs2Match).filter(Cs2Match.id.in_(match_ids)).all()} if match_ids else {}

    def _match_already_decided(m: Market) -> bool:
        match = matches_by_id.get(m.cs2_match_id) if m.cs2_match_id else None
        return match is not None and match.winner is not None

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _match_already_started(m: Market) -> bool:
        match = matches_by_id.get(m.cs2_match_id) if m.cs2_match_id else None
        if match is None:
            return False
        if not match.estimated_start_time:
            # FALL BACK TO THE DATE. A row can legitimately carry no start time:
            # the platform may never publish one, and the repair for an ORPHANED
            # fixture is to clear a bogus future start rather than invent a time
            # of day nobody recorded. Without this fallback those rows read as
            # "not started" forever and a match played days ago keeps showing up
            # as recommendable -- which is exactly what happened to Invictus
            # Gaming vs LNG Esports (played 2026-08-02) right after that repair.
            #
            # Strictly BEFORE today, so a match dated today whose time is unknown
            # is still offered rather than hidden on a guess.
            if not match.match_date:
                return False
            try:
                day = datetime.date.fromisoformat(match.match_date[:10])
            except ValueError:
                return False
            return day < now_utc.date()
        try:
            start = datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return now_utc >= start

    all_snapshots = _batch_latest_snapshots(session, [m.id for m in markets])
    now_for_staleness = datetime.datetime.now(datetime.timezone.utc)
    # MEASURED AGAINST THE FEED, NOT THE WALL CLOCK -- same fix as
    # tennis_markets.py, applied here because this sport was measured to have the
    # same defect. Over 6 hours of real snapshot history the poll gap for this
    # sport reached 27 minutes against a 20-minute threshold, so every
    # overrun tipped EVERY market over the staleness line at once and emptied the
    # board until the next burst refilled it. Nothing was wrong with the markets;
    # the poll was just late. (Tennis showed this as matches vanishing from
    # Recommended and reappearing minutes later.)
    #
    # Comparing each market against the newest snapshot in the feed is
    # self-calibrating: a late poll shifts everything together and drops nothing,
    # while a market that stops updating WHILE its neighbours keep ticking -- the
    # genuine "delisted, price frozen" case this gate exists for -- still stands
    # out immediately. FEED_DEAD_AFTER keeps an absolute backstop so a feed that
    # dies completely cannot keep frozen markets alive forever.
    STALE_BEHIND_FEED = datetime.timedelta(minutes=20)
    FEED_DEAD_AFTER = datetime.timedelta(hours=2)

    _snap_times = [
        (s.ts if s.ts.tzinfo else s.ts.replace(tzinfo=datetime.timezone.utc))
        for s in all_snapshots.values() if s is not None and s.ts is not None
    ]
    feed_latest = max(_snap_times) if _snap_times else None

    def _market_stale(m: Market) -> bool:
        snap = all_snapshots.get(m.id)
        if snap is None or snap.ts is None:
            return False
        ts = snap.ts if snap.ts.tzinfo else snap.ts.replace(tzinfo=datetime.timezone.utc)
        if feed_latest is None or now_for_staleness - feed_latest > FEED_DEAD_AFTER:
            return now_for_staleness - ts > STALE_BEHIND_FEED
        return feed_latest - ts > STALE_BEHIND_FEED

    # REAL BUG this guards against (user-reported 2026-07-20: recommended
    # bets pricing off already-decided matches, e.g. "0.1%" prices) -- see
    # ladder_sanity.py's own module comment for the full esports-specific
    # calibration story. `_match_already_started` above only fires once
    # liquipedia.net has actually populated a real estimated_start_time,
    # which lags behind Kalshi's own live trading -- this catches the case
    # where that hasn't happened yet but the market's own price/volume
    # history already makes clear the series is live or over.
    LIVE_TRADING_LOOKBACK = datetime.timedelta(hours=6)  # see ladder_sanity.py's own module comment for why 6, not 1
    cutoff = datetime.datetime.utcnow() - LIVE_TRADING_LOOKBACK
    recent_rows = fetch_in_chunks(
        [m.id for m in markets],
        lambda chunk: (
            session.query(MarketSnapshot)
            .filter(MarketSnapshot.market_id.in_(chunk), MarketSnapshot.ts >= cutoff)
            .all()
        ),
    )
    recent_snapshots_by_market: dict[int, list[MarketSnapshot]] = {}
    for snap in recent_rows:
        recent_snapshots_by_market.setdefault(snap.market_id, []).append(snap)

    def _market_looks_live_by_trading(m: Market) -> bool:
        if m.source != "kalshi":
            # Kalshi-only, but the REASON changed 2026-08-02. This used to read
            # "no real CS2 Polymarket inventory to calibrate against", which is
            # no longer true -- CS2 now has real Polymarket match inventory
            # (see polymarket_cs2_client.py). It stays Kalshi-only because the
            # thresholds below are Kalshi-VOLUME-scale
            # (CS2_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA) and Polymarket's
            # volumes sit on a different scale entirely, so reusing them would
            # be a guess -- the same reason Tennis deferred its own
            # Polymarket-scale live detector rather than inventing a number.
            # Deferring costs little here: this detector exists to catch
            # matches whose estimated_start_time is missing or lags real
            # trading, and Polymarket supplies a real gameStartTime on 100% of
            # CS2 match markets (494/494 confirmed live), so those rows are
            # already gated by _match_already_started above -- verified live,
            # 0 Polymarket rows survived on already-started matches.
            return False
        current = all_snapshots.get(m.id)
        current_price = current.last_price if current else None
        recent = recent_snapshots_by_market.get(m.id, [])
        return looks_already_live_by_trading(
            current_price, [(s.last_price, s.volume) for s in recent],
            min_volume_delta=CS2_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA,
            min_price_swing=ESPORTS_LIVE_TRADING_MIN_PRICE_SWING,
        )

    matches_live_by_trading = {m.cs2_match_id for m in markets if m.cs2_match_id and _market_looks_live_by_trading(m)}

    def _match_looks_live_by_trading(m: Market) -> bool:
        return m.cs2_match_id in matches_live_by_trading

    # POSITIVE in-play/finished signal, the only gate here not inferred from a
    # timestamp or a price. Kalshi's start times for esports are demonstrably
    # wrong (the reported DRX case really began 4h before its recorded start),
    # and a result may never arrive for a team whose sponsor name our results
    # source does not know. See flashscore_esports_client for the measured
    # coverage (modest) and for why it still ships: it is ONE-DIRECTIONAL and
    # fails open, so it can only ever hide a match a real source reports as
    # live or over, never one that is genuinely upcoming.
    _fs_states = flashscore_esports_client.cached_match_states("cs2")
    _fs_hidden = {
        mid for mid, match in matches_by_id.items()
        if flashscore_esports_client.hides_match(
            _fs_states, match.team_a, match.team_b, match.estimated_start_time)
    } if _fs_states else set()

    def _match_live_on_flashscore(m: Market) -> bool:
        return getattr(m, "cs2_match_id", None) in _fs_hidden

    markets = [
        m for m in markets
        if not _match_live_on_flashscore(m)
        if not _match_already_decided(m)
        and not _match_already_started(m)
        and (m.status or "active") == "active"
        and not _market_stale(m)
        and not _match_looks_live_by_trading(m)
    ]
    # Hoisted: as an inline set literal this was rebuilt once per
    # all_snapshots entry -- quadratic, and the dominant cost of the
    # tennis endpoint at 34k markets (183M attribute reads, ~40s).
    _kept_market_ids = {m.id for m in markets}
    snapshots_by_market = {mid: s for mid, s in all_snapshots.items() if mid in _kept_market_ids}
    weekly_pool, futures_pool = get_cs2_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    # NOTE: the roster-change "Wait" caveat that used to be computed here was
    # removed 2026-07-23. It existed to hold off betting a just-changed roster,
    # but the calibration (scripts/calibrate_cs2_roster_window.py) found NO
    # post-roster-change accuracy penalty at any horizon -- the player-level
    # Elo + K-boost already absorb a lineup change -- so the flag only added
    # noise with nothing to wait for. The shared waitReason/"Wait" badge stays
    # for sports where a wait IS real (MLB starting-pitcher confirmation, NFL/
    # NBA injuries); esports simply no longer feed it.

    out = []
    for m in markets:
        match = matches_by_id.get(m.cs2_match_id) if m.cs2_match_id else None
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        model_prob = _game_model_prob(m, match) if m.market_type in GAME_MARKET_TYPES else None
        no_baseline_reason = None if model_prob is not None else NO_BASELINE_REASON
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "cs2", m.market_type)
        pool = futures_pool if m.market_type == "tournament_winner" else weekly_pool
        _uscale = FUTURES_UNIT_SCALE if pool is futures_pool else 1.0
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=_uscale)
        out.append(
            Cs2MarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                side=m.side,
                line=m.line,
                match_label=f"{match.team_a} vs {match.team_b}" if match else None,
                cs2_match_id=m.cs2_match_id,
                event_name=match.event_name if match else None,
                match_date=match.match_date if match else None,
                estimated_start_time=match.estimated_start_time if match else None,
                best_of=match.best_of if match else None,
                group_label=m.group_label,
                implied_prob=implied,
                yes_bid=snap.yes_bid if snap else None,
                yes_ask=snap.yes_ask if snap else None,
                last_price=snap.last_price if snap else None,
                volume=snap.volume if snap else None,
                updated_at=m.updated_at.isoformat() if m.updated_at else None,
                model_prob=model_prob,
                model_validated=False,
                edge=edge,
                no_baseline_reason=no_baseline_reason,
                kelly_fraction=kelly,
                suggested_stake_dollars=stake_dollars,
                suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
                stake_pool="futures" if m.market_type == "tournament_winner" else ("weekly" if kelly is not None else None),
            )
        )
    out.sort(key=lambda m: (m.match_date or "9999", m.match_label or m.group_label or "", m.market_type))
    return out


def _game_insight_cs2(match: Cs2Match, model_prob: float | None, market_prob: float | None) -> str:
    a_rating = elo_service_cs2.get_team_rating(match.team_a)
    b_rating = elo_service_cs2.get_team_rating(match.team_b)
    story = ""
    if a_rating is not None and b_rating is not None:
        gap = a_rating - b_rating
        seed = f"{match.team_a}|{match.team_b}|{a_rating}|{b_rating}"
        if abs(gap) < 30:
            story = _seeded_choice(seed, [
                f"This one projects tight -- team Elo has {match.team_a} and {match.team_b} rated almost even ({a_rating:.0f} to {b_rating:.0f}), so there's little to separate them going in.",
                f"There's barely anything between these two on the ratings ({a_rating:.0f} to {b_rating:.0f}), which makes it close to a coin flip on paper.",
                f"About as even as it gets: team Elo puts {match.team_a} and {match.team_b} nearly level ({a_rating:.0f} to {b_rating:.0f}).",
            ])
        else:
            stronger, s_r, weaker, w_r = (match.team_a, a_rating, match.team_b, b_rating) if gap > 0 else (match.team_b, b_rating, match.team_a, a_rating)
            story = _seeded_choice(seed, [
                f"{stronger} comes in as the stronger side by team Elo, clear of {weaker} ({s_r:.0f} to {w_r:.0f}).",
                f"The ratings favor {stronger} here, sitting above {weaker} ({s_r:.0f} to {w_r:.0f}).",
                f"Team Elo gives {stronger} the edge, ahead of {weaker} ({s_r:.0f} to {w_r:.0f}).",
            ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_cs2_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport != "cs2":
        raise HTTPException(404, "market not found")
    match = session.get(Cs2Match, m.cs2_match_id) if m.cs2_match_id else None
    label = f"{match.team_a} vs {match.team_b}" if match else (m.group_label or m.market_type)
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    caveats = [
        "model_validated: false -- real market-odds backtest found the market beats the model.",
        COLD_START_CAVEAT,
    ]
    methodology = _NO_BASELINE_METHODOLOGY
    insight = ""

    if m.market_type in GAME_MARKET_TYPES and match is not None:
        methodology = (
            "Team-level Elo (K=32, grid-searched against a real 8,839-match historical liquipedia.net "
            "crawl -- see elo_cs2.py) gives a per-map win probability, extended to a full best-of-N series-score "
            "distribution via the standard 'race to k' binomial identity."
        )
        if match.best_of:
            factors.append(ReasoningFactorOut(label="Best of", detail=str(match.best_of)))
        a_rating = elo_service_cs2.get_team_rating(match.team_a)
        b_rating = elo_service_cs2.get_team_rating(match.team_b)
        if a_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.team_a} Elo rating", detail=f"{a_rating:.0f}"))
        if b_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.team_b} Elo rating", detail=f"{b_rating:.0f}"))
        insight = _game_insight_cs2(match, model_prob, market_prob)

    elif m.market_type == "tournament_winner":
        methodology = (
            "Elo-seeded single-elimination Monte Carlo of the event bracket: each team's CS2 team Elo sets "
            "its per-match win probabilities, the bracket is simulated many thousands of times, and the "
            "share of runs a team wins the whole event becomes its price. An APPROXIMATION -- real events "
            "are often double-elimination or Swiss, so this is a reference estimate (approx badge), not a "
            "validated edge."
        )
        team = m.team or (m.group_label or "this team")
        rating = elo_service_cs2.get_team_rating(m.team) if m.team else None
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} Elo rating", detail=f"{rating:.0f}"))
        seed = f"{team}|{rating}|cs2tw"
        rt = f" (team Elo {rating:.0f})" if rating is not None else ""
        insight = _seeded_choice(seed, [
            f"This is the tournament outright for {team}{rt}. It comes from an Elo-seeded Monte Carlo of the event bracket -- {team}'s rating drives each round's win odds, and the price is how often they take the whole thing across thousands of simulated runs.",
            f"{team}'s{rt} title price is read off a bracket simulation: seed every team by CS2 Elo, play the event out many thousands of times, and count how often {team} is left standing.",
            f"Priced from a simulated run of the bracket -- {team}{rt} is carried through the event thousands of times on Elo-based match odds, and the share of wins is this number.",
        ]) + " Bracket's simplified to single-elim, so treat it as a reference read. " + _edge_sentence(model_prob, market_prob)

    if not insight:
        insight = f"{methodology} {_edge_sentence(model_prob, market_prob)}"

    return ReasoningOut(
        market_id=m.id,
        market_type=m.market_type,
        label=label,
        model_prob=model_prob,
        market_prob=market_prob,
        edge=edge,
        insight=insight,
        methodology=methodology,
        factors=factors,
        caveats=caveats,
    )
