import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_session
from app.db.models import PlacedBet
from app.models.clv import compute_bet_clv

router = APIRouter(prefix="/placed-bets", tags=["placed-bets"])

VALID_SETTLE_STATUSES = {"won", "lost", "push", "void"}

# Game/match id columns in the SAME priority order as the frontend crossPlatformKey
# (markets.ts). race_event_id is deliberately NOT here: the frontend key doesn't
# read it (racing rows fall to the sport|market_type|team fallback), so this must
# stay byte-identical to the frontend -- the Recommended view's cross-platform
# "already placed" check compares this backend key against the frontend key.
_GAME_ID_ATTRS = [
    "nfl_game_id", "nba_game_id", "wnba_game_id", "mlb_game_id", "mma_fight_id",
    "tennis_match_id", "soccer_match_id", "valorant_match_id", "cs2_match_id",
    "lol_match_id",
]
_ESPORTS_ID_ATTRS = {"valorant_match_id", "cs2_match_id", "lol_match_id"}


def _fmt_line(v) -> str:
    """Match JS `${line}`: whole numbers drop the decimal (4.0 -> '4'), fractional
    keep it (4.5 -> '4.5'); None -> '' (JS `line ?? ''`)."""
    if v is None:
        return ""
    return str(int(v)) if float(v) == int(v) else str(v)


def _game_key(bet) -> str:
    """The real-world GAME/match this bet is tied to ("" for futures/season-long
    rows, which aren't game-tied). Mirrors the frontend capToOneRowPerGame id.

    Why it's exposed: the Recommended view already caps to ONE row per game
    (bets on the same game are correlated, not independent), but "already placed"
    was matched per-proposition -- so after placing e.g. Over 4.5, the single row
    for that game could later flip to Over 5.5 and read unplaced, tempting a
    second correlated position on the same game. Matching at game level keeps the
    display consistent with the cap the app already applies."""
    for attr in _GAME_ID_ATTRS:
        v = getattr(bet, attr, None)
        if v:
            return f"{attr.replace('_match_id', '')}:{v}" if attr in _ESPORTS_ID_ATTRS else str(v)
    return ""


def _cross_platform_key(bet) -> str:
    """BYTE-IDENTICAL to frontend markets.ts crossPlatformKey. Two rows sharing
    this key are the same real-world bet regardless of platform (Kalshi/Polymarket)
    or which ladder rung/price is shown. Used by the create copycat guard AND
    exposed as OpenBetOut/SettledBetOut.cross_key so the Recommended view can mark
    a proposition "placed" no matter which book's copy the deduped row shows. MUST
    match the frontend exactly (esports title-prefix, JS line formatting, race
    excluded, team??label fallback)."""
    gid = None
    for attr in _GAME_ID_ATTRS:
        v = getattr(bet, attr, None)
        if v:
            gid = f"{attr.replace('_match_id', '')}:{v}" if attr in _ESPORTS_ID_ATTRS else str(v)
            break
    mt = bet.market_type or ""
    if gid:
        return f"{gid}|{mt}|{bet.team or ''}|{_fmt_line(bet.line)}|{bet.side or ''}"
    return f"{bet.sport or ''}|{mt}|{bet.team if bet.team is not None else (bet.label or '')}"

# Calibration buckets group settled bets by their predicted probability at
# placement time (10-point buckets, 0-100%) so "does 70%-confidence
# actually win about 70% of the time" is checkable at a glance -- see
# get_bet_stats below.
def _bucket_label(p: float) -> str:
    """Real bug caught during verification: `int(p // 0.10)` puts 0.70 in
    the 60-70% bucket, not 70-80%, because 0.7 // 0.1 == 6.0 in floating
    point (0.7 isn't exactly representable) -- classic float trap.
    Rounding to whole percentage points first avoids it."""
    lo = min(round(p * 100) // 10, 9) * 10
    return f"{lo}-{lo + 10}%"


def _final_score_str(game) -> str | None:
    """Human-readable final score/result for a settled bet's game, resolved
    per-sport from whatever score fields that game model carries. None when
    there's no single game score (MMA whole-card, motorsport, futures)."""
    if game is None:
        return None
    hs = getattr(game, "home_score", None)
    as_ = getattr(game, "away_score", None)
    if hs is not None and as_ is not None:  # NFL/NBA/MLB/WNBA
        return f"{getattr(game, 'away_team', '?')} {int(as_)} - {int(hs)} {getattr(game, 'home_team', '?')}"
    ma, mb = getattr(game, "maps_won_a", None), getattr(game, "maps_won_b", None)
    if ma is not None and mb is not None:  # CS2/Valorant/LoL (maps)
        return f"{getattr(game, 'team_a', '?')} {ma}-{mb} {getattr(game, 'team_b', '?')}"
    rft = getattr(game, "result_ft", None)
    if rft:  # soccer full-time result (e.g. "2-1")
        return f"{getattr(game, 'home_team', '?')} {rft} {getattr(game, 'away_team', '?')}"
    if getattr(game, "winner_key", None) is not None:  # tennis
        winner = game.player_a_name if game.winner_key == game.player_a_key else game.player_b_name
        loser = game.player_b_name if game.winner_key == game.player_a_key else game.player_a_name
        sc = getattr(game, "score", None)
        return f"{winner} def. {loser}{f' {sc}' if sc else ''}"
    if getattr(game, "winner_id", None) is not None:  # MMA
        w = game.fighter_a_name if game.winner_id == game.fighter_a_id else game.fighter_b_name
        method = getattr(game, "method", None)
        rd = getattr(game, "round", None)
        return f"{w} def. by {method or 'decision'}{f' (R{rd})' if rd else ''}"
    rj = getattr(game, "result_json", None)  # racing
    if rj:
        try:
            import json
            from app.models.baseline import racing_ratings
            order = (json.loads(rj) or {}).get("order") or []
            if order:
                name = racing_ratings._series_state(getattr(game, "series", "f1")).get("id_to_name", {}).get(order[0], order[0])
                return f"Winner: {name}"
        except Exception:
            return None
    return None


def _realized_profit(row: PlacedBet, stake: float | None) -> float | None:
    """Realized profit on a settled bet, in whatever unit `stake` is given
    (dollars or units). Kalshi/Polymarket are binary contracts bought at a
    price p == market_prob_at_placement: staking S buys S/p contracts that
    each pay 1 on a win, so a WIN returns S/p and profits S*(1-p)/p; a LOSS
    forfeits the whole stake; push/void return the stake (0 profit). Pending
    bets aren't realized yet -> None. A won bet with no recorded price can't
    be valued -> None (rare; the snapshot almost always has it)."""
    if stake is None:
        return None
    if row.status == "lost":
        return -stake
    if row.status in ("push", "void"):
        return 0.0
    if row.status == "won":
        p = row.market_prob_at_placement
        if p is None or p <= 0:
            return None
        return stake * (1.0 - p) / p
    return None  # pending / unknown


class PlacedBetIn(BaseModel):
    market_id: int
    market_type: str
    source: str
    sport: str = "nfl"  # added when NBA became the 2nd sport (2026-07-17); defaults to "nfl" so existing callers are unaffected
    team: str | None = None
    line: float | None = None
    side: str | None = None
    label: str
    nfl_game_id: str | None = None
    nba_game_id: str | None = None
    wnba_game_id: str | None = None
    mlb_game_id: str | None = None
    mma_fight_id: str | None = None
    tennis_match_id: int | None = None
    soccer_match_id: int | None = None
    valorant_match_id: int | None = None
    cs2_match_id: int | None = None
    lol_match_id: int | None = None
    race_event_id: int | None = None  # F1/NASCAR/IndyCar -- links a racing bet to its RaceEvent for start-time + CLV
    stake_pool: str
    stake_dollars: float
    stake_units: float | None = None
    market_prob_at_placement: float | None = None
    model_prob_at_placement: float | None = None
    edge_at_placement: float | None = None


class PlacedBetOut(BaseModel):
    id: int
    market_id: int
    market_type: str
    source: str
    sport: str
    team: str | None
    line: float | None
    side: str | None
    label: str
    nfl_game_id: str | None
    nba_game_id: str | None
    wnba_game_id: str | None
    mlb_game_id: str | None
    mma_fight_id: str | None
    tennis_match_id: int | None
    soccer_match_id: int | None
    valorant_match_id: int | None
    cs2_match_id: int | None
    lol_match_id: int | None
    stake_pool: str
    stake_dollars: float
    stake_units: float | None
    market_prob_at_placement: float | None
    model_prob_at_placement: float | None
    edge_at_placement: float | None
    placed_at: str
    status: str
    settled_at: str | None
    settlement_note: str | None
    closing_prob: float | None
    clv_pp: float | None
    clv_status: str
    profit_dollars: float | None  # realized P/L, None while pending
    profit_units: float | None

    class Config:
        from_attributes = True


class SettleIn(BaseModel):
    status: str
    note: str | None = None


class LockedPoolsOut(BaseModel):
    weekly_locked_dollars: float
    futures_locked_dollars: float


class CalibrationBucketOut(BaseModel):
    range_label: str
    predicted_avg: float | None
    actual_win_rate: float | None
    n: int


class BetStatsOut(BaseModel):
    total_settled: int
    wins: int
    losses: int
    pushes: int
    voids: int
    win_rate: float | None
    brier_score: float | None
    market_brier_score: float | None  # same outcomes scored against the MARKET's own price, for comparison
    avg_clv_pp: float | None
    clv_sample_size: int
    calibration_buckets: list[CalibrationBucketOut]


class PortfolioSportOut(BaseModel):
    sport: str
    staked_dollars: float       # staked on SETTLED (won+lost) bets -- the ROI denominator
    net_profit_dollars: float
    roi: float | None           # net_profit / staked, None if nothing settled
    net_units: float
    wins: int
    losses: int
    pushes: int
    voids: int
    pending: int
    at_risk_dollars: float      # stake still live in pending bets
    avg_clv_pp: float | None
    clv_sample: int


class PortfolioSourceOut(BaseModel):
    source: str                 # kalshi | polymarket
    staked_dollars: float
    net_profit_dollars: float
    roi: float | None
    net_units: float
    wins: int
    losses: int
    pushes: int
    voids: int
    pending: int
    at_risk_dollars: float
    avg_clv_pp: float | None
    clv_sample: int


class PortfolioPointOut(BaseModel):
    date: str                   # YYYY-MM-DD (settlement day)
    cumulative_profit_dollars: float
    cumulative_profit_units: float


class FuturesSummaryOut(BaseModel):
    """Season-long / tournament futures kept SEPARATE from game bets: they tie
    up capital for months and have no clean closing line, so blending their P/L
    and ROI into the daily game-bet numbers (or into CLV) would mislead. No CLV
    field on purpose -- futures are clv_status 'not_applicable'."""
    staked_dollars: float
    net_profit_dollars: float
    roi: float | None
    net_units: float
    wins: int
    losses: int
    pushes: int
    voids: int
    pending: int
    at_risk_dollars: float
    by_sport: list[PortfolioSportOut]


class PortfolioOut(BaseModel):
    # Headline numbers are GAME bets only (daily rhythm + real CLV); futures are
    # split out below into their own summary.
    staked_dollars: float
    net_profit_dollars: float
    roi: float | None
    net_units: float
    wins: int
    losses: int
    pushes: int
    voids: int
    pending: int
    at_risk_dollars: float
    avg_clv_pp: float | None
    clv_sample: int
    by_sport: list[PortfolioSportOut]
    by_source: list[PortfolioSourceOut]
    equity_curve: list[PortfolioPointOut]
    futures: FuturesSummaryOut


class OpenBetOut(BaseModel):
    """A pending (unsettled) real bet, for the tracker's 'upcoming' watchlist.
    start_time is resolved live from the game/match record (None for MMA and
    any market with no single kickoff-equivalent moment)."""
    id: int
    market_id: int              # the underlying Market -- lets the tracker open reasoning + dedup placed
    sport: str
    source: str
    market_type: str
    label: str
    team: str | None
    side: str | None
    line: float | None
    stake_pool: str             # "weekly" | "futures" -- lets the tracker split game vs futures
    stake_dollars: float
    stake_units: float | None
    market_prob_at_placement: float | None
    model_prob_at_placement: float | None
    edge_at_placement: float | None
    placed_at: str
    start_time: str | None      # UTC ISO, None if unknown / no single start
    start_date: str | None = None  # YYYY-MM-DD fallback (MMA fights carry an event date even when the exact time is unknown)
    original_start_time: str | None = None  # scheduled start at placement, for reschedule detection
    rescheduled: bool = False   # current start is materially later than at placement (delayed/postponed to a new time)
    clv_status: str
    cross_key: str = ""         # frontend-identical crossPlatformKey: lets Recommended mark a bet "placed" on EITHER book
    game_key: str = ""          # real-world game id ("" for futures): marks the whole game covered


class SettledBetOut(BaseModel):
    """A settled (won/lost/push/void) real bet, for the tracker's completed-bets
    history. Most-recently-settled first."""
    id: int
    market_id: int
    sport: str
    source: str
    market_type: str
    label: str
    team: str | None
    side: str | None
    line: float | None
    stake_pool: str
    stake_dollars: float
    stake_units: float | None
    market_prob_at_placement: float | None
    model_prob_at_placement: float | None
    status: str
    profit_dollars: float | None
    profit_units: float | None
    settled_at: str | None
    clv_pp: float | None
    clv_status: str
    final_score: str | None     # e.g. "KC 3 - 7 DET"; None for MMA/futures (no single game score)
    cross_key: str = ""         # frontend-identical crossPlatformKey (see OpenBetOut.cross_key)
    game_key: str = ""          # real-world game id (see OpenBetOut.game_key)


def _to_out(session: Session, row: PlacedBet) -> PlacedBetOut:
    clv = compute_bet_clv(session, row)
    return PlacedBetOut(
        id=row.id,
        market_id=row.market_id,
        market_type=row.market_type,
        source=row.source,
        sport=row.sport,
        team=row.team,
        line=row.line,
        side=row.side,
        label=row.label,
        nfl_game_id=row.nfl_game_id,
        nba_game_id=row.nba_game_id,
        wnba_game_id=row.wnba_game_id,
        mlb_game_id=row.mlb_game_id,
        mma_fight_id=row.mma_fight_id,
        tennis_match_id=row.tennis_match_id,
        soccer_match_id=row.soccer_match_id,
        valorant_match_id=row.valorant_match_id,
        cs2_match_id=row.cs2_match_id,
        lol_match_id=row.lol_match_id,
        stake_pool=row.stake_pool,
        stake_dollars=row.stake_dollars,
        stake_units=row.stake_units,
        market_prob_at_placement=row.market_prob_at_placement,
        model_prob_at_placement=row.model_prob_at_placement,
        edge_at_placement=row.edge_at_placement,
        placed_at=row.placed_at.isoformat(),
        status=row.status,
        settled_at=row.settled_at.isoformat() if row.settled_at else None,
        settlement_note=row.settlement_note,
        closing_prob=clv["closing_prob"],
        clv_pp=clv["clv_pp"],
        clv_status=clv["status"],
        profit_dollars=_realized_profit(row, row.stake_dollars),
        profit_units=_realized_profit(row, row.stake_units),
    )


@router.get("", response_model=list[PlacedBetOut])
def list_placed_bets(status: str | None = None, sport: str = "nfl", session: Session = Depends(get_session)):
    # sport defaults to "nfl" so every EXISTING caller (NFL's own Placed Bets
    # page, built before NBA existed) keeps its exact current behavior --
    # added 2026-07-17 when NBA became the 2nd sport sharing this same table.
    # Real bets only -- the Placed Bets page is for bets the user actually made.
    # Auto-logged paper bets (paper_logger.py) are a background forward-CLV
    # dataset surfaced in aggregate via /clv-buckets, not this per-bet list.
    query = session.query(PlacedBet).filter(PlacedBet.sport == sport, PlacedBet.paper == False)  # noqa: E712
    if status:
        query = query.filter(PlacedBet.status == status)
    rows = query.order_by(PlacedBet.placed_at.desc()).all()
    return [_to_out(session, r) for r in rows]


@router.get("/locked", response_model=LockedPoolsOut)
def get_locked_pools(sport: str = "nfl", session: Session = Depends(get_session)):
    """Sum of stake_dollars across PENDING placed bets, per pool -- this is
    capital already committed to a real, live bet, so Recommended Bets'
    portfolio cap needs to subtract it from the pool's available budget
    (otherwise a placed bet doesn't actually reduce room for new
    recommendations, which defeats the point of tracking it).

    REAL BUG this guards against, caught while wiring up NBA's own staking
    (2026-07-17): each sport has its OWN separate weekly/futures pool with
    its own dollar budget (see settings.py::get_pool_dollars vs.
    get_nba_pool_dollars) -- summing locked dollars across ALL sports
    combined, as this endpoint used to, would let an NBA bet's locked
    capital wrongly eat into NFL's reported available budget and vice
    versa. sport defaults to "nfl" so the existing NFL frontend call
    (built before NBA existed) is unaffected."""
    # paper == False: auto-logged paper bets don't lock REAL capital, so they
    # must not eat the recommendation portfolio budget (see paper_logger.py).
    rows = (
        session.query(PlacedBet)
        .filter(PlacedBet.status == "pending", PlacedBet.sport == sport, PlacedBet.paper == False)  # noqa: E712
        .all()
    )
    weekly = sum(r.stake_dollars for r in rows if r.stake_pool == "weekly")
    futures = sum(r.stake_dollars for r in rows if r.stake_pool == "futures")
    return LockedPoolsOut(weekly_locked_dollars=weekly, futures_locked_dollars=futures)


@router.get("/stats", response_model=BetStatsOut)
def get_bet_stats(sport: str = "nfl", session: Session = Depends(get_session)):
    """Calibration + CLV summary across every placed bet -- the "is any of
    this actually trustworthy" dashboard. Win rate needs a large sample to
    mean much (a handful of games is mostly luck); Brier score and CLV are
    both usable with a much smaller sample, which matters given this app
    just started tracking real placed bets. sport defaults to "nfl" for the
    same backward-compat reason as get_locked_pools above -- mixing NFL and
    NBA calibration together would muddy "is THIS sport's model
    trustworthy" into an uninterpretable blend of two different models."""
    # Real bets only -- /stats is the "is MY real betting trustworthy" ROI +
    # calibration view. Paper bets (paper_logger.py) are the forward-CLV study
    # and live in /clv-buckets instead, so they must not pollute real win-rate.
    all_bets = session.query(PlacedBet).filter(PlacedBet.sport == sport, PlacedBet.paper == False).all()  # noqa: E712
    settled = [b for b in all_bets if b.status in ("won", "lost")]
    wins = sum(1 for b in settled if b.status == "won")
    losses = sum(1 for b in settled if b.status == "lost")
    pushes = sum(1 for b in all_bets if b.status == "push")
    voids = sum(1 for b in all_bets if b.status == "void")
    win_rate = round(wins / len(settled), 4) if settled else None

    brier_terms: list[float] = []
    market_brier_terms: list[float] = []
    bucket_data: dict[str, list[tuple[float, float]]] = {}
    for b in settled:
        outcome = 1.0 if b.status == "won" else 0.0
        if b.model_prob_at_placement is not None:
            brier_terms.append((b.model_prob_at_placement - outcome) ** 2)
            bucket_data.setdefault(_bucket_label(b.model_prob_at_placement), []).append(
                (b.model_prob_at_placement, outcome)
            )
        if b.market_prob_at_placement is not None:
            market_brier_terms.append((b.market_prob_at_placement - outcome) ** 2)

    brier_score = round(sum(brier_terms) / len(brier_terms), 4) if brier_terms else None
    market_brier_score = round(sum(market_brier_terms) / len(market_brier_terms), 4) if market_brier_terms else None

    buckets = []
    for lo in range(0, 100, 10):
        label = f"{lo}-{lo + 10}%"
        entries = bucket_data.get(label, [])
        if entries:
            predicted_avg = round(sum(p for p, _ in entries) / len(entries), 4)
            actual_win_rate = round(sum(o for _, o in entries) / len(entries), 4)
        else:
            predicted_avg = None
            actual_win_rate = None
        buckets.append(CalibrationBucketOut(range_label=label, predicted_avg=predicted_avg, actual_win_rate=actual_win_rate, n=len(entries)))

    clv_values = []
    for b in all_bets:
        clv = compute_bet_clv(session, b)
        if clv["status"] == "closed" and clv["clv_pp"] is not None:
            clv_values.append(clv["clv_pp"])
    avg_clv_pp = round(sum(clv_values) / len(clv_values), 4) if clv_values else None

    return BetStatsOut(
        total_settled=len(settled),
        wins=wins,
        losses=losses,
        pushes=pushes,
        voids=voids,
        win_rate=win_rate,
        brier_score=brier_score,
        market_brier_score=market_brier_score,
        avg_clv_pp=avg_clv_pp,
        clv_sample_size=len(clv_values),
        calibration_buckets=buckets,
    )


@router.get("/portfolio", response_model=PortfolioOut)
def get_portfolio(session: Session = Depends(get_session)):
    """Cross-sport bet-tracker rollup -- the "how am I actually doing"
    dashboard that replaces a standalone bet diary. REAL bets only (paper
    bets are the forward-CLV study, not real money). Realized P/L uses each
    bet's own placement price (see _realized_profit); ROI is net profit over
    stake on SETTLED (won+lost) bets -- pushes/voids return the stake so they
    don't belong in the denominator, and pending stake isn't resolved yet
    (surfaced separately as at_risk_dollars). CLV is folded in here too so
    price-capture and money outcome sit side by side, exactly as asked."""
    rows = session.query(PlacedBet).filter(PlacedBet.paper == False).all()  # noqa: E712

    def _blank() -> dict:
        return {"staked": 0.0, "net": 0.0, "net_units": 0.0, "wins": 0, "losses": 0,
                "pushes": 0, "voids": 0, "pending": 0, "at_risk": 0.0, "clv": []}

    overall = _blank()
    per_sport: dict[str, dict] = {}
    per_source: dict[str, dict] = {}
    # Futures are tracked in a parallel, separate set of aggregates (see
    # FuturesSummaryOut) -- discriminated by stake_pool, which the recommender
    # already sets to "futures" for season-long/tournament markets.
    fut_overall = _blank()
    fut_per_sport: dict[str, dict] = {}
    # settlement-day -> summed realized $/units that day, for the cumulative curve
    daily: dict[str, float] = {}
    daily_units: dict[str, float] = {}

    for r in rows:
        is_futures = r.stake_pool == "futures"
        prof = _realized_profit(r, r.stake_dollars)
        prof_u = _realized_profit(r, r.stake_units)
        if is_futures:
            buckets = (fut_overall, fut_per_sport.setdefault(r.sport, _blank()))
        else:
            buckets = (overall, per_sport.setdefault(r.sport, _blank()), per_source.setdefault(r.source, _blank()))
        if r.status == "pending":
            for agg in buckets:
                agg["pending"] += 1
                agg["at_risk"] += r.stake_dollars or 0.0
        else:
            if r.status == "won":
                for agg in buckets:
                    agg["wins"] += 1
            elif r.status == "lost":
                for agg in buckets:
                    agg["losses"] += 1
            elif r.status == "push":
                for agg in buckets:
                    agg["pushes"] += 1
            elif r.status == "void":
                for agg in buckets:
                    agg["voids"] += 1
            # ROI denominator = stake on decided (won/lost) bets only
            if r.status in ("won", "lost"):
                for agg in buckets:
                    agg["staked"] += r.stake_dollars or 0.0
            if prof is not None:
                for agg in buckets:
                    agg["net"] += prof
                if not is_futures:  # only game bets feed the daily equity curve
                    day = (r.settled_at or r.placed_at).date().isoformat()
                    daily[day] = daily.get(day, 0.0) + prof
            if prof_u is not None:
                for agg in buckets:
                    agg["net_units"] += prof_u
                if not is_futures and r.stake_units is not None:
                    day = (r.settled_at or r.placed_at).date().isoformat()
                    daily_units[day] = daily_units.get(day, 0.0) + prof_u
        # CLV (real close only) -- game bets only; futures are not_applicable
        if not is_futures:
            clv = compute_bet_clv(session, r)
            if clv["status"] == "closed" and clv["clv_pp"] is not None:
                for agg in buckets:
                    agg["clv"].append(clv["clv_pp"])

    def _roi(agg: dict) -> float | None:
        return round(agg["net"] / agg["staked"], 4) if agg["staked"] > 0 else None

    def _avg_clv(agg: dict) -> float | None:
        return round(sum(agg["clv"]) / len(agg["clv"]), 4) if agg["clv"] else None

    by_sport = [
        PortfolioSportOut(
            sport=sport,
            staked_dollars=round(agg["staked"], 2),
            net_profit_dollars=round(agg["net"], 2),
            roi=_roi(agg),
            net_units=round(agg["net_units"], 2),
            wins=agg["wins"], losses=agg["losses"], pushes=agg["pushes"], voids=agg["voids"],
            pending=agg["pending"], at_risk_dollars=round(agg["at_risk"], 2),
            avg_clv_pp=_avg_clv(agg), clv_sample=len(agg["clv"]),
        )
        for sport, agg in sorted(per_sport.items(), key=lambda kv: kv[1]["net"], reverse=True)
    ]

    by_source = [
        PortfolioSourceOut(
            source=source,
            staked_dollars=round(agg["staked"], 2),
            net_profit_dollars=round(agg["net"], 2),
            roi=_roi(agg),
            net_units=round(agg["net_units"], 2),
            wins=agg["wins"], losses=agg["losses"], pushes=agg["pushes"], voids=agg["voids"],
            pending=agg["pending"], at_risk_dollars=round(agg["at_risk"], 2),
            avg_clv_pp=_avg_clv(agg), clv_sample=len(agg["clv"]),
        )
        for source, agg in sorted(per_source.items(), key=lambda kv: kv[1]["net"], reverse=True)
    ]

    running = 0.0
    running_u = 0.0
    curve: list[PortfolioPointOut] = []
    for day in sorted(daily.keys()):
        running += daily[day]
        running_u += daily_units.get(day, 0.0)
        curve.append(PortfolioPointOut(date=day, cumulative_profit_dollars=round(running, 2), cumulative_profit_units=round(running_u, 2)))

    fut_by_sport = [
        PortfolioSportOut(
            sport=sport,
            staked_dollars=round(agg["staked"], 2),
            net_profit_dollars=round(agg["net"], 2),
            roi=_roi(agg),
            net_units=round(agg["net_units"], 2),
            wins=agg["wins"], losses=agg["losses"], pushes=agg["pushes"], voids=agg["voids"],
            pending=agg["pending"], at_risk_dollars=round(agg["at_risk"], 2),
            avg_clv_pp=None, clv_sample=0,   # futures have no meaningful CLV
        )
        for sport, agg in sorted(fut_per_sport.items(), key=lambda kv: kv[1]["net"], reverse=True)
    ]
    futures = FuturesSummaryOut(
        staked_dollars=round(fut_overall["staked"], 2),
        net_profit_dollars=round(fut_overall["net"], 2),
        roi=_roi(fut_overall),
        net_units=round(fut_overall["net_units"], 2),
        wins=fut_overall["wins"], losses=fut_overall["losses"],
        pushes=fut_overall["pushes"], voids=fut_overall["voids"],
        pending=fut_overall["pending"], at_risk_dollars=round(fut_overall["at_risk"], 2),
        by_sport=fut_by_sport,
    )

    return PortfolioOut(
        staked_dollars=round(overall["staked"], 2),
        net_profit_dollars=round(overall["net"], 2),
        roi=_roi(overall),
        net_units=round(overall["net_units"], 2),
        wins=overall["wins"], losses=overall["losses"], pushes=overall["pushes"], voids=overall["voids"],
        pending=overall["pending"], at_risk_dollars=round(overall["at_risk"], 2),
        avg_clv_pp=_avg_clv(overall), clv_sample=len(overall["clv"]),
        by_sport=by_sport, by_source=by_source, equity_curve=curve,
        futures=futures,
    )


@router.get("/open", response_model=list[OpenBetOut])
def get_open_bets(session: Session = Depends(get_session)):
    """Pending (unsettled) REAL bets across every sport -- the tracker's
    'upcoming, watch these' list. Start time is resolved live from each bet's
    game/match record (reusing the CLV module's per-sport resolver), so it's
    accurate for existing bets with no start-time column. Soonest kickoff
    first; bets with no known single start (MMA, anything unresolved) sort
    last."""
    from app.models.clv import _get_game, _game_kickoff_dt

    rows = (
        session.query(PlacedBet)
        .filter(PlacedBet.status == "pending", PlacedBet.paper == False)  # noqa: E712
        .all()
    )
    from app.db.models import MmaFight

    out: list[tuple[datetime.datetime | None, OpenBetOut]] = []
    for r in rows:
        start_dt: datetime.datetime | None = None
        start_date: str | None = None
        try:
            game = _get_game(session, r)
            if game is not None:
                start_dt = _game_kickoff_dt(game)
        except Exception:
            start_dt = None
        # MMA is excluded from _get_game (whole-card CLV ambiguity), but a fight
        # now carries its own estimated_start_time and always an event_date --
        # surface those so MMA bets aren't dateless in the tracker.
        if r.sport == "mma" and r.mma_fight_id:
            fight = session.get(MmaFight, r.mma_fight_id)
            if fight is not None:
                start_date = fight.event_date
                if start_dt is None and fight.estimated_start_time:
                    try:
                        start_dt = datetime.datetime.fromisoformat(fight.estimated_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
                    except (ValueError, AttributeError):
                        start_dt = None
        # Reschedule = the match moved to a DIFFERENT CALENDAR DAY vs placement
        # (a genuine postponement). The old ">3h later" rule false-fired
        # constantly on soft, same-day start ESTIMATES that just drift as the day
        # firms up -- tennis order-of-play especially (e.g. an "02:00" estimate
        # sliding to "08:00" is not a reschedule, just the estimate updating).
        # Same-day lateness is handled by the "delayed?" label + sort, not this
        # badge.
        rescheduled = False
        if r.original_start_time and start_dt is not None:
            try:
                orig = datetime.datetime.fromisoformat(r.original_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
                rescheduled = start_dt.date() != orig.date() and start_dt > orig
            except (ValueError, AttributeError):
                rescheduled = False
        clv = compute_bet_clv(session, r)
        # These datetimes are naive UTC (the CLV module works in UTC); emit an
        # explicit 'Z' so the browser parses them as UTC, not local time -- else
        # a just-started game misreads as hours away (timezone-offset bug).
        out.append((start_dt, OpenBetOut(
            id=r.id, market_id=r.market_id, sport=r.sport, source=r.source, market_type=r.market_type,
            label=r.label, team=r.team, side=r.side, line=r.line,
            stake_pool=r.stake_pool,
            stake_dollars=r.stake_dollars, stake_units=r.stake_units,
            market_prob_at_placement=r.market_prob_at_placement,
            model_prob_at_placement=r.model_prob_at_placement,
            edge_at_placement=r.edge_at_placement,
            placed_at=r.placed_at.isoformat() + "Z",
            start_time=(start_dt.isoformat() + "Z") if start_dt else None,
            start_date=start_date,
            original_start_time=r.original_start_time,
            rescheduled=rescheduled,
            clv_status=clv["status"],
            cross_key=_cross_platform_key(r),
            game_key=_game_key(r),
        )))
    # Ordering: UPCOMING first (soonest start at top -- the actionable ones),
    # then genuinely-DELAYED bets (start already >4h past but still pending --
    # waiting on a result/settlement, not actionable) pushed BELOW them, then
    # no-start futures last. A bet rescheduled to a NEW future time carries that
    # new time as start_dt, so it lands back among the upcoming bets in its new
    # slot instead of clinging to the top as "delayed" (per the tracker's own
    # start-time display + reschedule badge).
    now = datetime.datetime.utcnow()

    def _sort_key(t):
        dt = t[0]
        if dt is None:
            return (2, 0.0)
        ts = dt.timestamp()
        if (now - dt).total_seconds() > 4 * 3600:
            return (1, -ts)  # delayed/overdue: below upcoming, most-recent first
        return (0, ts)       # upcoming (or just-started): soonest first

    out.sort(key=_sort_key)
    return [o for _, o in out]


@router.get("/settled", response_model=list[SettledBetOut])
def get_settled_bets(session: Session = Depends(get_session)):
    """Completed (won/lost/push/void) REAL bets across every sport -- the
    tracker's history list. Most-recently-settled first (bets missing a
    settled_at, e.g. legacy rows, sort last by placed_at)."""
    rows = (
        session.query(PlacedBet)
        .filter(PlacedBet.status.in_(("won", "lost", "push", "void")), PlacedBet.paper == False)  # noqa: E712
        .all()
    )
    def _key(r: PlacedBet):
        return r.settled_at or r.placed_at or datetime.datetime.min
    rows.sort(key=_key, reverse=True)
    # bet_settlement._get_game links EVERY sport (incl. MMA + racing), unlike
    # clv._get_game which excludes MMA -- so final scores resolve for all sports.
    from app.models.bet_settlement import _get_game as _settle_get_game
    out: list[SettledBetOut] = []
    for r in rows:
        clv = compute_bet_clv(session, r)
        try:
            final_score = _final_score_str(_settle_get_game(session, r))
        except Exception:
            final_score = None
        out.append(SettledBetOut(
            id=r.id, market_id=r.market_id, sport=r.sport, source=r.source, market_type=r.market_type,
            label=r.label, team=r.team, side=r.side, line=r.line,
            stake_pool=r.stake_pool, stake_dollars=r.stake_dollars, stake_units=r.stake_units,
            market_prob_at_placement=r.market_prob_at_placement,
            model_prob_at_placement=r.model_prob_at_placement,
            status=r.status,
            profit_dollars=_realized_profit(r, r.stake_dollars),
            profit_units=_realized_profit(r, r.stake_units),
            settled_at=(r.settled_at.isoformat() + "Z") if r.settled_at else None,
            clv_pp=clv["clv_pp"], clv_status=clv["status"],
            final_score=final_score,
            cross_key=_cross_platform_key(r),
            game_key=_game_key(r),
        ))
    return out


@router.get("/paper-summary")
def get_paper_summary(session: Session = Depends(get_session)):
    """Visibility for the auto paper-trading logger (paper_logger.py): how many
    paper bets are tracking forward CLV right now, so the CLV page shows it's
    working even before any game has closed (buckets fill only as bets settle)."""
    from collections import Counter
    from app.models.clv_selection import bucket_clv_stats

    rows = session.query(PlacedBet.sport, PlacedBet.status).filter(PlacedBet.paper == True).all()  # noqa: E712
    by_sport = Counter(s for s, _ in rows)
    pending = sum(1 for _, st in rows if st == "pending")
    stats = bucket_clv_stats(session)  # TTL-cached; closed bets (paper + real) with real CLV
    with_clv = sum(b["n"] for b in stats.values())
    return {
        "total": len(rows),
        "pending": pending,
        "settled": len(rows) - pending,
        "with_clv": with_clv,
        "by_sport": dict(sorted(by_sport.items(), key=lambda kv: -kv[1])),
    }


@router.get("/clv-buckets")
def get_clv_buckets(session: Session = Depends(get_session)):
    """Per-(sport, market_type) forward-CLV status -- which buckets are earning
    their place. Inert until buckets reach the min sample (see
    clv_selection.py). Cross-sport."""
    from app.models.clv_selection import bucket_report
    return bucket_report(session)


@router.get("/clv-conditional")
def get_clv_conditional(session: Session = Depends(get_session)):
    """Pre-registered conditional-CLV slices (edge-magnitude, tennis tier) -- the
    'is the edge hiding in a subset?' view. See clv_selection.conditional_clv_report."""
    from app.models.clv_selection import conditional_clv_report
    return conditional_clv_report(session)


def _resolve_bet_start(session: Session, bet: PlacedBet) -> tuple["datetime.datetime | None", str | None]:
    """(start_datetime, start_date) for a bet's game/match, MMA-aware. Shared by
    /open and placement (which snapshots it as original_start_time)."""
    from app.models.clv import _get_game, _game_kickoff_dt
    from app.db.models import MmaFight

    start_dt = None
    start_date = None
    try:
        game = _get_game(session, bet)
        if game is not None:
            start_dt = _game_kickoff_dt(game)
    except Exception:
        start_dt = None
    if bet.sport == "mma" and bet.mma_fight_id:
        fight = session.get(MmaFight, bet.mma_fight_id)
        if fight is not None:
            start_date = fight.event_date
            if start_dt is None and fight.estimated_start_time:
                try:
                    start_dt = datetime.datetime.fromisoformat(fight.estimated_start_time.replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, AttributeError):
                    start_dt = None
    return start_dt, start_date


@router.post("", response_model=PlacedBetOut)
def create_placed_bet(body: PlacedBetIn, session: Session = Depends(get_session)):
    row = PlacedBet(**body.model_dump())
    # Copycat guard: the same real-world bet is offered on BOTH Kalshi and
    # Polymarket (same internal game/match id). The Recommended view collapses
    # them into one row, but which platform it shows can flip over time (it
    # prefers the higher-volume side), so the "same" bet can re-appear looking
    # new and get marked placed a second time hours later -- a real copycat seen
    # live (Ryuki Matsuda moneyline: Polymarket at 16:22, then Kalshi at 20:33,
    # both real $20 on tennis match 1502). If a REAL, still-pending bet already
    # covers this cross-platform key, return it instead of creating a duplicate
    # (idempotent -- the frontend still gets a valid placed bet back).
    key = _cross_platform_key(row)
    for existing in (
        session.query(PlacedBet)
        .filter(PlacedBet.paper == False, PlacedBet.status == "pending")  # noqa: E712
        .all()
    ):
        if _cross_platform_key(existing) == key:
            return _to_out(session, existing)
    session.add(row)
    session.flush()
    # Snapshot the game's scheduled start now, so a later reschedule (start moves
    # to a different day) is detectable in /open.
    start_dt, _ = _resolve_bet_start(session, row)
    if start_dt is not None:
        row.original_start_time = start_dt.isoformat() + "Z"
    session.commit()
    session.refresh(row)
    return _to_out(session, row)


@router.post("/{bet_id}/settle", response_model=PlacedBetOut)
def settle_placed_bet(bet_id: int, body: SettleIn, session: Session = Depends(get_session)):
    if body.status not in VALID_SETTLE_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_SETTLE_STATUSES)}")
    row = session.get(PlacedBet, bet_id)
    if row is None:
        raise HTTPException(404, "placed bet not found")
    row.status = body.status
    row.settled_at = datetime.datetime.utcnow()
    row.settlement_note = body.note
    session.commit()
    session.refresh(row)
    return _to_out(session, row)


@router.delete("/{bet_id}")
def delete_placed_bet(bet_id: int, session: Session = Depends(get_session)):
    row = session.get(PlacedBet, bet_id)
    if row is not None:
        session.delete(row)
        session.commit()
    return {"status": "ok"}
