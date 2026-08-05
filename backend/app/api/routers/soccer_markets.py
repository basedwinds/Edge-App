"""Soccer markets API -- parallel to routers/tennis_markets.py.

Moneyline_3way (attack/defense Poisson goal model, see elo_soccer.py) plus
a growing family of markets fed by the SAME underlying goal-distribution
model: game_spread/game_total/btts/team_total (full match), ftts (First
Team To Score), correct_score (exact scoreline), and first_half_*/
second_half_* (winner/spread/total/team_total/btts, derated via
elo_soccer.py::predict_half's own real first-half-goal-share constant).
The second batch (team_total/ftts/correct_score/half-family, added
2026-07-19) surfaced from a full catalog_scan.py audit that found this app
had real, live Kalshi/Polymarket inventory for all of them -- MLS-only real
inventory as of this build for most of it (the 5 European leagues are
off-season, see kalshi_soccer_client.py's own docstring), built for all 6
leagues anyway so European coverage activates automatically in-season.
Backtested against real historical odds for all 5 football-data.co.uk-
sourced leagues (see scripts/backtest_moneyline_soccer.py, re-run
2026-07-19 with elo_soccer.py's grid-searched HOME_ADVANTAGE_LOG/K -- see
that module's own docstring for the real grid-search numbers) -- NO edge
found at any league for moneyline/spread/total (moneyline Brier
0.5878-0.6012 vs market's 0.5717-0.5872; spread 0.2528-0.2587 vs
0.2486-0.2513; total 0.2342-0.2477 vs 0.2265-0.2428, consistently), same
standing finding as every other sport/market in this app; every market type
added since (btts onward) hasn't been through this app's own backtest
harness yet. MLS has no free historical-odds source at all (see
SoccerMatch's docstring in app/db/models.py) so it can never be backtested
-- ships live-only. model_validated: false everywhere, same policy as
every other market here.

moneyline_3way additionally blends in two independent, free, rule-based
situational signals -- Transfermarkt whole-league injury lists (see
app/models/news_adjustment/injury_rules_soccer.py) and ESPN whole-league
standings-based late-season "nothing left to play for" motivation (see
app/models/news_adjustment/motivation_rules_soccer.py) -- merged via
merge_adjustments (schema.py) in
app/ingestion/poller_soccer.py::refresh_soccer_news_adjustments, then
folded in via combine_probability_3way. NOT yet separately backtested
(this app's own backtest harness predates both signals), so they change
what model_prob IS for moneyline_3way without changing model_validated's
False status. spread/game_total/btts remain the pure Poisson baseline, no
situational blend."""
import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice
from app.api.routers.settings import get_soccer_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import FuturesMarketOut, ReasoningFactorOut, ReasoningOut, SoccerMarketOut
from app.clients import kalshi_soccer_client
from app.clients.football_data_client import PROMOTION_SOURCE_DIVISION
from app.db.database import get_session
from app.db.models import Market, MarketSnapshot, SoccerMatch, SoccerNewsAdjustmentCache
from app.ingestion.market_catalog_soccer import get_soccer_news_adjustment_cache, soccer_news_cache_to_pydantic
from app.ingestion.market_matcher_soccer import canonical_team_key, team_names_match
from app.models.baseline import elo_service_soccer
from app.models.combine import combine_probability_3way
from app.models.ladder_sanity import (
    SOCCER_LIVE_TRADING_MIN_PRICE_SWING,
    SOCCER_LIVE_TRADING_MIN_VOLUME_DELTA,
    find_resolved_entities,
    looks_already_live_by_trading,
)
from app.models.news_adjustment.schema import NewsAdjustment
from app.models.season_sim_soccer import SeasonSimResult, prob_points_at_least, simulate_season
from app.models.staking import FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

router = APIRouter(prefix="/soccer", tags=["soccer"])

GAME_MARKET_TYPES = {
    "moneyline_3way", "game_spread", "game_total", "btts",
    # Second batch (added 2026-07-19) -- see module docstring.
    "team_total", "ftts", "correct_score",
    "first_half_winner", "first_half_spread", "first_half_total", "first_half_team_total", "first_half_btts",
    "second_half_winner", "second_half_spread", "second_half_total", "second_half_team_total", "second_half_btts",
}

# Real threshold-LADDER market types (multiple lines/rungs per real match+
# team/side, same monotonic-threshold shape find_resolved_entities' own
# ladder_looks_resolved check depends on) -- moneyline_3way/ftts/
# correct_score/first_half_winner/second_half_winner are all discrete,
# non-threshold outcomes with no ladder to collapse, same reasoning
# game_spread/game_total were already excluded from this set for.
LADDER_MARKET_TYPES = {
    "game_spread", "game_total", "team_total",
    "first_half_spread", "first_half_total", "first_half_team_total",
    "second_half_spread", "second_half_total", "second_half_team_total",
}

NO_BASELINE_REASON = (
    "No baseline yet -- this market's model is still being built and validated against this app's "
    "own historical data, not shipped as a guessed number."
)

# Same gate/reasoning as elo_service_tennis.py::get_player_match_count's own
# real, validated finding (a 0-history entity's rating is a pure neutral
# placeholder, not an estimate) -- applied directly to Soccer's team-level
# case rather than re-deriving the same result from scratch.
NO_HISTORY_REASON = (
    "One or both teams have no tracked match history in this app's own data (football-data.co.uk / "
    "ESPN) -- their rating would be a pure league-average placeholder, not a real estimate, so no "
    "model number is shown rather than risk a misleadingly confident one."
)

LIVE_TRADING_LOOKBACK = datetime.timedelta(hours=6)  # see ladder_sanity.py's own module comment for why 6, not 1


def _moneyline_model_prob(market: Market, match: SoccerMatch | None, news: NewsAdjustment | None = None) -> float | None:
    """Blends in the free Transfermarkt injury signal (see
    injury_rules_soccer.py) via combine_probability_3way when a cached
    adjustment exists for this match -- otherwise identical to the pure
    Poisson baseline. Unlike NBA's split model_prob/final_prob, this app
    returns a single already-blended number for Soccer's moneyline (the
    blend delta is separately exposed via news_adjustment_pct in the API
    response for transparency, not as a second probability field)."""
    if match is None or market.side is None:
        return None
    dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
    if dist is None:
        return None
    p_home, p_draw, p_away = combine_probability_3way(dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win(), news)
    if market.side == "home":
        p = p_home
    elif market.side == "away":
        p = p_away
    elif market.side == "draw":
        p = p_draw
    else:
        return None
    return round(p, 4)


def _game_spread_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """market.team is which side this row's YES favors ("wins by more than
    line goals"); market.line is Kalshi/Polymarket's own line value in THEIR
    convention (positive number, "team wins by more than X"), which needs
    negating before it matches elo_soccer.py::prob_home_spread_cover's own
    sign convention (negative line = that side favored) -- see that
    function's own docstring. Away-side rows are scored by swapping which
    team's margin the function checks (mirrors the same line, home/away
    reversed), not by writing a second formula.

    REAL BUG this fixes (caught live 2026-07-19, this app's own first live
    browser check of the spread market): comparing market.team against
    match.home_team/away_team with EXACT string equality silently dropped
    every Polymarket spread row whose team spelling didn't byte-match
    whichever platform's poller created the SoccerMatch row first (e.g.
    Polymarket's "Austin FC" vs a match created from Kalshi's own "Austin")
    -- both real spellings of the same real club, already solved elsewhere
    in this app by team_names_match's fuzzy comparison (see
    market_matcher_soccer.py), just not reused here until this bug surfaced."""
    if match is None or market.team is None or market.line is None:
        return None
    dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
    if dist is None:
        return None
    if team_names_match(market.team, match.home_team):
        p = dist.prob_home_spread_cover(-market.line)
    elif team_names_match(market.team, match.away_team):
        p = 1.0 - dist.prob_home_spread_cover(market.line)
    else:
        return None
    return round(p, 4)


def _game_total_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    if match is None or market.line is None:
        return None
    dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
    if dist is None:
        return None
    return round(dist.prob_total_over(market.line), 4)


def _btts_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    if match is None:
        return None
    dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
    if dist is None:
        return None
    return round(dist.prob_btts(), 4)


def _team_total_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """Full-match team_total -- reuses team_names_match the same way
    _game_spread_model_prob does (see that function's own real-bug comment
    on why exact string equality silently drops cross-platform rows)."""
    if match is None or market.team is None or market.line is None:
        return None
    dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
    if dist is None:
        return None
    if team_names_match(market.team, match.home_team):
        side = "home"
    elif team_names_match(market.team, match.away_team):
        side = "away"
    else:
        return None
    return round(dist.prob_team_total_over(side, market.line), 4)


def _ftts_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    if match is None or market.side is None:
        return None
    dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
    if dist is None:
        return None
    p_home, p_away, p_none = dist.prob_first_to_score()
    if market.side == "home":
        p = p_home
    elif market.side == "away":
        p = p_away
    elif market.side == "none":
        p = p_none
    else:
        return None
    return round(p, 4)


def _correct_score_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    if match is None or market.correct_score_home is None or market.correct_score_away is None:
        return None
    dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
    if dist is None:
        return None
    return round(dist.prob_correct_score(market.correct_score_home, market.correct_score_away), 4)


def _half_moneyline_model_prob(market: Market, match: SoccerMatch | None, half: int) -> float | None:
    if match is None or market.side is None:
        return None
    dist = elo_service_soccer.get_half_distribution(match.league, match.home_team, match.away_team, half)
    if dist is None:
        return None
    if market.side == "home":
        p = dist.prob_home_win()
    elif market.side == "away":
        p = dist.prob_away_win()
    elif market.side == "draw":
        p = dist.prob_draw()
    else:
        return None
    return round(p, 4)


def _half_spread_model_prob(market: Market, match: SoccerMatch | None, half: int) -> float | None:
    if match is None or market.team is None or market.line is None:
        return None
    dist = elo_service_soccer.get_half_distribution(match.league, match.home_team, match.away_team, half)
    if dist is None:
        return None
    if team_names_match(market.team, match.home_team):
        p = dist.prob_home_spread_cover(-market.line)
    elif team_names_match(market.team, match.away_team):
        p = 1.0 - dist.prob_home_spread_cover(market.line)
    else:
        return None
    return round(p, 4)


def _half_total_model_prob(market: Market, match: SoccerMatch | None, half: int) -> float | None:
    if match is None or market.line is None:
        return None
    dist = elo_service_soccer.get_half_distribution(match.league, match.home_team, match.away_team, half)
    if dist is None:
        return None
    return round(dist.prob_total_over(market.line), 4)


def _half_team_total_model_prob(market: Market, match: SoccerMatch | None, half: int) -> float | None:
    if match is None or market.team is None or market.line is None:
        return None
    dist = elo_service_soccer.get_half_distribution(match.league, match.home_team, match.away_team, half)
    if dist is None:
        return None
    if team_names_match(market.team, match.home_team):
        side = "home"
    elif team_names_match(market.team, match.away_team):
        side = "away"
    else:
        return None
    return round(dist.prob_team_total_over(side, market.line), 4)


def _half_btts_model_prob(market: Market, match: SoccerMatch | None, half: int) -> float | None:
    if match is None:
        return None
    dist = elo_service_soccer.get_half_distribution(match.league, match.home_team, match.away_team, half)
    if dist is None:
        return None
    return round(dist.prob_btts(), 4)


_MODEL_PROB_DISPATCH = {
    "game_spread": lambda m, match, news: _game_spread_model_prob(m, match),
    "game_total": lambda m, match, news: _game_total_model_prob(m, match),
    "btts": lambda m, match, news: _btts_model_prob(m, match),
    "team_total": lambda m, match, news: _team_total_model_prob(m, match),
    "ftts": lambda m, match, news: _ftts_model_prob(m, match),
    "correct_score": lambda m, match, news: _correct_score_model_prob(m, match),
    "first_half_winner": lambda m, match, news: _half_moneyline_model_prob(m, match, 1),
    "second_half_winner": lambda m, match, news: _half_moneyline_model_prob(m, match, 2),
    "first_half_spread": lambda m, match, news: _half_spread_model_prob(m, match, 1),
    "second_half_spread": lambda m, match, news: _half_spread_model_prob(m, match, 2),
    "first_half_total": lambda m, match, news: _half_total_model_prob(m, match, 1),
    "second_half_total": lambda m, match, news: _half_total_model_prob(m, match, 2),
    "first_half_team_total": lambda m, match, news: _half_team_total_model_prob(m, match, 1),
    "second_half_team_total": lambda m, match, news: _half_team_total_model_prob(m, match, 2),
    "first_half_btts": lambda m, match, news: _half_btts_model_prob(m, match, 1),
    "second_half_btts": lambda m, match, news: _half_btts_model_prob(m, match, 2),
}


def _model_prob(market: Market, match: SoccerMatch | None, news: NewsAdjustment | None = None) -> float | None:
    """moneyline_3way is the ONLY market_type that blends in the free
    situational signal (injuries + late-season motivation, see module
    docstring) -- every other market_type here (including all of this
    second batch: FTTS/correct_score/team_total/half-family) reflects the
    pure Poisson baseline, dispatched via _MODEL_PROB_DISPATCH instead of a
    long if/elif chain now that there are 15+ non-moneyline market types."""
    if market.market_type == "moneyline_3way":
        return _moneyline_model_prob(market, match, news)
    handler = _MODEL_PROB_DISPATCH.get(market.market_type)
    if handler is None:
        return None
    return handler(market, match, news)


def _batch_news_adjustments(session: Session, match_ids: set[int]) -> dict[int, NewsAdjustment]:
    if not match_ids:
        return {}
    rows = session.query(SoccerNewsAdjustmentCache).filter(SoccerNewsAdjustmentCache.soccer_match_id.in_(match_ids)).all()
    return {r.soccer_match_id: soccer_news_cache_to_pydantic(r) for r in rows}


def _batch_recent_snapshots_for_live_check(session: Session, market_ids: list[int]) -> dict[int, list[MarketSnapshot]]:
    """Same shape/reasoning as tennis_markets.py's own version of this
    helper -- EVERY snapshot within LIVE_TRADING_LOOKBACK per market_id, not
    just the latest one, since looks_already_live_by_trading needs the
    MAX/MIN across the whole window."""
    if not market_ids:
        return {}
    from app.db.chunked import fetch_in_chunks

    cutoff = datetime.datetime.utcnow() - LIVE_TRADING_LOOKBACK
    rows = fetch_in_chunks(
        market_ids,
        lambda chunk: (
            session.query(
                MarketSnapshot.market_id, MarketSnapshot.last_price, MarketSnapshot.volume
            )
            .filter(MarketSnapshot.market_id.in_(chunk), MarketSnapshot.ts >= cutoff)
            .all()
        ),
    )
    out: dict[int, list[MarketSnapshot]] = {}
    for snap in rows:
        out.setdefault(snap.market_id, []).append(snap)
    return out


@router.get("/markets", response_model=list[SoccerMarketOut])
def list_soccer_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "soccer", Market.market_type.in_(GAME_MARKET_TYPES)).all()
    match_ids = {m.soccer_match_id for m in markets if m.soccer_match_id}
    matches_by_id = {
        m.id: m for m in session.query(SoccerMatch).filter(SoccerMatch.id.in_(match_ids)).all()
    } if match_ids else {}

    def _match_already_decided(m: Market) -> bool:
        match = matches_by_id.get(m.soccer_match_id) if m.soccer_match_id else None
        return match is not None and match.result_ft is not None

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    today_iso = now_utc.date().isoformat()

    def _match_already_started(m: Market) -> bool:
        """Same real bug class this app has now found and fixed for MLB/MMA/
        Tennis (see dead_market_sanity_check.py's module docstring) --
        checked proactively for Soccer from day one, PLUS a second, Soccer-
        specific check this app's own first live test run of this router
        (2026-07-19) actually caught: real MLS matches from March/April/May
        2026 (confirmed already played) were still showing as open
        "recommendations" because their `estimated_start_time` (sourced from
        Polymarket's `gameStartTime`) was corrupted to a FUTURE date
        (August/September/November) -- the exact mismatch flagged as a real,
        observed data quirk in polymarket_soccer_client.py's own module
        docstring, here proven to actually defeat this guard rather than
        being a theoretical concern. `match_date` (parsed from the market's
        own question text, e.g. "...on 2026-04-12?", NOT from gameStartTime)
        is the reliable signal instead -- any match_date strictly before
        today is real proof the match has been played, regardless of what
        estimated_start_time says."""
        match = matches_by_id.get(m.soccer_match_id) if m.soccer_match_id else None
        if match is None:
            return False
        if match.match_date and match.match_date < today_iso:
            return True
        if not match.estimated_start_time:
            return False
        try:
            start = datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return start < now_utc

    # Deliberately NOT using Market.updated_at as a staleness signal -- same
    # real reasoning as tennis_markets.py (onupdate only fires on an actual
    # value change, so a thin/illiquid market can go many poll cycles with a
    # genuinely unchanged price and still look "stale" by that measure).
    # MarketSnapshot.ts is the reliable "still open" signal instead -- a
    # fresh row gets inserted every poll cycle regardless of price movement.
    all_snapshots = _batch_latest_snapshots(session, [m.id for m in markets])
    # MEASURED AGAINST THE FEED, NOT THE WALL CLOCK -- same fix as
    # tennis_markets.py, applied here because this sport was measured to have the
    # same defect. Over 6 hours of real snapshot history the poll gap for this
    # sport reached 32 minutes against a 20-minute threshold, so every
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
        if feed_latest is None or now_utc - feed_latest > FEED_DEAD_AFTER:
            return now_utc - ts > STALE_BEHIND_FEED
        return feed_latest - ts > STALE_BEHIND_FEED

    # Ladder-resolved guard (LADDER_MARKET_TYPES only -- moneyline_3way/
    # ftts/correct_score/half-winner have no threshold ladder) -- same
    # structural-tell reasoning as tennis_markets.py's own version: a real,
    # still-undecided pregame ladder never prices a HIGHER threshold as more
    # likely than a lower one (e.g. "Over 2.5 goals" must be <= "Over 1.5
    # goals"), so two rungs converging on the same extreme value means the
    # real outcome is already locked in, independent of any timestamp this
    # app stores. The (match_id, market_type, team) grouping key already
    # keeps every ladder type/team combination separate on its own (e.g.
    # team_total's two teams, or first_half_total vs the full-match one),
    # no extra dimension needed for the second batch.
    ladder_groups: dict[tuple, list[tuple[float, float]]] = {}
    for m in markets:
        if m.line is None or m.soccer_match_id is None or m.market_type not in LADDER_MARKET_TYPES:
            continue
        snap = all_snapshots.get(m.id)
        implied = _implied_prob(snap)
        if implied is None:
            continue
        key = (m.soccer_match_id, m.market_type, m.team)
        ladder_groups.setdefault(key, []).append((m.line, implied))
    match_ids_by_group = {key: key[0] for key in ladder_groups}
    resolved_group_keys = find_resolved_entities(ladder_groups)
    matches_with_resolved_ladder = {match_ids_by_group[key] for key in resolved_group_keys}

    def _match_ladder_resolved(m: Market) -> bool:
        return m.soccer_match_id in matches_with_resolved_ladder

    recent_snapshots_for_live_check = _batch_recent_snapshots_for_live_check(session, [m.id for m in markets])

    def _market_looks_live_by_trading(m: Market) -> bool:
        if m.source != "kalshi":
            return False
        current = all_snapshots.get(m.id)
        current_price = current.last_price if current else None
        recent = recent_snapshots_for_live_check.get(m.id, [])
        # Soccer-specific, lower thresholds -- see ladder_sanity.py's own
        # comment on why Tennis's shared default (calibrated against a real
        # ~400k-700k volume swing) would never fire against Soccer's own
        # real, much smaller Kalshi volume scale (max observed 10,050 across
        # 90 real tracked moneyline rows at calibration time).
        return looks_already_live_by_trading(
            current_price, [(s.last_price, s.volume) for s in recent],
            min_volume_delta=SOCCER_LIVE_TRADING_MIN_VOLUME_DELTA,
            min_price_swing=SOCCER_LIVE_TRADING_MIN_PRICE_SWING,
        )

    matches_live_by_trading = {m.soccer_match_id for m in markets if m.soccer_match_id and _market_looks_live_by_trading(m)}

    def _match_looks_live_by_trading(m: Market) -> bool:
        return m.soccer_match_id in matches_live_by_trading

    markets = [
        m for m in markets
        if not _match_already_decided(m)
        and not _match_already_started(m)
        and not _match_ladder_resolved(m)
        and not _match_looks_live_by_trading(m)
        and (m.status or "active") == "active"
        and not _market_stale(m)
    ]
    # Hoisted: as an inline set literal this was rebuilt once per
    # all_snapshots entry -- quadratic, and the dominant cost of the
    # tennis endpoint at 34k markets (183M attribute reads, ~40s).
    _kept_market_ids = {m.id for m in markets}
    snapshots_by_market = {mid: s for mid, s in all_snapshots.items() if mid in _kept_market_ids}
    weekly_pool, futures_pool = get_soccer_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    def _either_team_unrated(match: SoccerMatch | None) -> bool:
        if match is None:
            return False
        return (
            elo_service_soccer.get_team_match_count(match.league, match.home_team) == 0
            or elo_service_soccer.get_team_match_count(match.league, match.away_team) == 0
        )

    news_by_match = _batch_news_adjustments(session, {m.soccer_match_id for m in markets if m.soccer_match_id})

    out = []
    for m in markets:
        match = matches_by_id.get(m.soccer_match_id) if m.soccer_match_id else None
        news = news_by_match.get(m.soccer_match_id) if m.soccer_match_id else None
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        if _either_team_unrated(match):
            model_prob = None
            no_baseline_reason = NO_HISTORY_REASON
        else:
            model_prob = _model_prob(m, match, news)
            no_baseline_reason = None if model_prob is not None else NO_BASELINE_REASON
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "soccer", m.market_type)
        stake_dollars = size_stake_dollars(staking_mode, kelly, weekly_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full)
        out.append(
            SoccerMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                side=m.side,
                line=m.line,
                match_label=f"{match.home_team} vs {match.away_team}" if match else None,
                soccer_match_id=m.soccer_match_id,
                league=match.league if match else None,
                season=match.season if match else None,
                match_date=match.match_date if match else None,
                estimated_start_time=match.estimated_start_time if match else None,
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
                stake_pool="weekly" if kelly is not None else None,
                news_adjustment_pct=news.adjustment_pct if (news is not None and m.market_type == "moneyline_3way") else None,
                correct_score_home=m.correct_score_home,
                correct_score_away=m.correct_score_away,
            )
        )
    out.sort(key=lambda m: (m.match_date or "9999", m.match_label or ""))
    return out


_MARKET_TYPE_LABEL_TO_DIVISION = {
    ("league_winner", label): division for division, (_, label) in kalshi_soccer_client.LEAGUE_WINNER_SERIES.items()
}
_MARKET_TYPE_LABEL_TO_DIVISION.update({
    ("relegation", label): division for division, (_, label) in kalshi_soccer_client.RELEGATION_SERIES.items()
})
# top_half/top4/top2 (added 2026-07-19, EPL-only real inventory -- see
# kalshi_soccer_client.py::TOP_N_SERIES/_TOP_N_EVENT_LABELS for the real
# discovery this came from).
_MARKET_TYPE_LABEL_TO_DIVISION.update({
    (threshold, group_label): division
    for division, series_ticker in kalshi_soccer_client.TOP_N_SERIES.items()
    for threshold, group_label in kalshi_soccer_client._TOP_N_EVENT_LABELS.values()
})

# Season points ladders. group_label holds the division code itself (see
# market_catalog_soccer.upsert_kalshi_soccer_team_points_market) rather than a
# prose label, because these series have one event per league and no
# human-readable label to key on.
_MARKET_TYPE_LABEL_TO_DIVISION.update({
    ("team_points", division): division
    for division in kalshi_soccer_client.TEAM_POINTS_SERIES
})

_FUTURES_MARKET_TYPES = ["league_winner", "relegation", "top_half", "top4", "top2", "team_points"]

_SIM_PROB_FIELD_BY_MARKET_TYPE = {
    "relegation": "relegation_prob",
    "top_half": "top_half_prob",
    "top4": "top4_prob",
    "top2": "top2_prob",
}


def _futures_division(m: Market) -> str | None:
    return _MARKET_TYPE_LABEL_TO_DIVISION.get((m.market_type, m.group_label or ""))


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_soccer_futures(session: Session = Depends(get_session)):
    """League Winner + Relegation (all 5 European leagues) + Top-Half/
    Top-4/Top-2 (EPL only, added 2026-07-19 -- see kalshi_soccer_client.py::
    TOP_N_SERIES for the real discovery). MLS Cup still isn't built (see
    season_sim_soccer.py's own module docstring on why -- conference-based
    playoff, a genuinely different structure this round-robin model doesn't
    cover). One Monte Carlo run PER LEAGUE (not per market row, and shared
    across every market type for that league) -- all of a division's rows
    across every futures market type come from the SAME simulation,
    computed once and reused, same "don't recompute per row" reasoning as
    Tennis's own bracket sim."""
    markets = session.query(Market).filter(
        Market.sport == "soccer", Market.market_type.in_(_FUTURES_MARKET_TYPES), Market.status == "active"
    ).all()
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    weekly_pool, futures_pool = get_soccer_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    by_league: dict[str, list[Market]] = {}
    for m in markets:
        division = _futures_division(m)
        if division:
            by_league.setdefault(division, []).append(m)

    sim_by_league: dict[str, SeasonSimResult | None] = {}
    for division, league_markets in by_league.items():
        state = elo_service_soccer.get_rating_state(division)
        if state is None:
            sim_by_league[division] = None
            continue
        second_tier_division = PROMOTION_SOURCE_DIVISION.get(division)
        second_tier_state = elo_service_soccer.get_rating_state(second_tier_division) if second_tier_division else None
        canonical_teams = list({canonical_team_key(m.team) for m in league_markets if m.team})
        sim_by_league[division] = simulate_season(
            state, canonical_teams, division, n_simulations=3000, second_tier_state=second_tier_state,
        )

    out = []
    for m in markets:
        division = _futures_division(m)
        sim_result = sim_by_league.get(division) if division else None
        model_prob = None
        if sim_result is not None and m.team:
            if m.market_type == "team_points":
                # Not a rank question -- read the simulated season-points
                # distribution at this rung's threshold. Left unpriced (None) when
                # the sim doesn't cover the team, rather than defaulting to 0.0
                # the way the rank markets below do: on a points ladder 0.0 is a
                # confident "will not reach", which is a fabricated price.
                raw = prob_points_at_least(sim_result, canonical_team_key(m.team), m.line) if m.line is not None else None
                model_prob = round(raw, 4) if raw is not None else None
            else:
                prob_field = _SIM_PROB_FIELD_BY_MARKET_TYPE.get(m.market_type, "champion_prob")
                prob_dict = getattr(sim_result, prob_field)
                model_prob = round(prob_dict.get(canonical_team_key(m.team), 0.0), 4)
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "soccer", m.market_type)
        stake_dollars = size_stake_dollars(staking_mode, kelly, futures_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=FUTURES_UNIT_SCALE, min_market_price=FUTURES_MIN_MARKET_PRICE)
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        out.append(
            FuturesMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                group_label=m.group_label,
                # Points threshold on team_points rungs, null on every other
                # futures type. Was hardcoded None, which would have rendered
                # "Arsenal" with no number -- the same unactionable-bet bug the
                # WNBA spread and Discord alerts each hit.
                line=m.line,
                side=None,
                implied_prob=implied,
                yes_bid=snap.yes_bid if snap else None,
                yes_ask=snap.yes_ask if snap else None,
                last_price=snap.last_price if snap else None,
                volume=snap.volume if snap else None,
                updated_at=m.updated_at.isoformat() if m.updated_at else None,
                model_prob=model_prob,
                model_validated=False,
                edge=edge,
                kelly_fraction=kelly,
                suggested_stake_dollars=stake_dollars,
                suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
                stake_pool="futures" if kelly is not None else None,
                line_move_pp=None,
            )
        )
    out.sort(key=lambda m: (m.group_label or "", -(m.implied_prob or 0)))
    return out


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_soccer_market_reasoning(market_id: int, session: Session = Depends(get_session)):
    from fastapi import HTTPException

    m = session.get(Market, market_id)
    if m is None or m.sport != "soccer":
        raise HTTPException(status_code=404, detail="Soccer market not found")
    match = session.get(SoccerMatch, m.soccer_match_id) if m.soccer_match_id else None
    news = None
    if m.soccer_match_id is not None:
        cache = get_soccer_news_adjustment_cache(session, m.soccer_match_id)
        news = soccer_news_cache_to_pydantic(cache) if cache else None
    snap = _batch_latest_snapshots(session, [m.id]).get(m.id)
    implied = _implied_prob(snap)
    model_prob = _model_prob(m, match, news)
    edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
    label = f"{match.home_team} vs {match.away_team}" if match else "Unknown match"

    factors = []
    if match is not None:
        dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
        home_n = elo_service_soccer.get_team_match_count(match.league, match.home_team)
        away_n = elo_service_soccer.get_team_match_count(match.league, match.away_team)
        factors.append(ReasoningFactorOut(label="League", detail=f"{match.league} ({match.season})"))
        factors.append(ReasoningFactorOut(
            label="Team match history",
            detail=f"{match.home_team}: {home_n} rated matches, {match.away_team}: {away_n} rated matches",
        ))
        if dist is not None:
            factors.append(ReasoningFactorOut(
                label="Model expected goals",
                detail=f"{match.home_team} {dist.expected_home_goals:.2f} - {dist.expected_away_goals:.2f} {match.away_team}",
            ))
            factors.append(ReasoningFactorOut(
                label="Model outcome split",
                detail=f"Home {dist.prob_home_win()*100:.1f}% / Draw {dist.prob_draw()*100:.1f}% / Away {dist.prob_away_win()*100:.1f}%",
            ))
        if m.market_type in ("game_spread", "first_half_spread", "second_half_spread") and m.team is not None and m.line is not None:
            half_note = " (1st half)" if m.market_type == "first_half_spread" else " (2nd half)" if m.market_type == "second_half_spread" else ""
            factors.append(ReasoningFactorOut(label="Pick", detail=f"{m.team} wins by more than {m.line} goals{half_note}"))
        if m.market_type in ("game_total", "first_half_total", "second_half_total") and m.line is not None:
            half_note = " (1st half)" if m.market_type == "first_half_total" else " (2nd half)" if m.market_type == "second_half_total" else ""
            factors.append(ReasoningFactorOut(label="Pick", detail=f"Over {m.line} total goals{half_note}"))
        if m.market_type in ("team_total", "first_half_team_total", "second_half_team_total") and m.team is not None and m.line is not None:
            half_note = " (1st half)" if m.market_type == "first_half_team_total" else " (2nd half)" if m.market_type == "second_half_team_total" else ""
            factors.append(ReasoningFactorOut(label="Pick", detail=f"{m.team} over {m.line} goals{half_note}"))
        if m.market_type in ("btts", "first_half_btts", "second_half_btts"):
            half_note = " (1st half)" if m.market_type == "first_half_btts" else " (2nd half)" if m.market_type == "second_half_btts" else ""
            factors.append(ReasoningFactorOut(label="Pick", detail=f"Both teams to score{half_note}"))
        if m.market_type in ("first_half_winner", "second_half_winner") and m.side is not None:
            half_word = "1st half" if m.market_type == "first_half_winner" else "2nd half"
            pick = "Draw" if m.side == "draw" else (m.team or "—")
            factors.append(ReasoningFactorOut(label="Pick", detail=f"{pick} ({half_word})"))
        if m.market_type == "ftts" and m.side is not None:
            pick = "Neither team scores" if m.side == "none" else (m.team or "—")
            factors.append(ReasoningFactorOut(label="Pick", detail=f"First to score: {pick}"))
        if m.market_type == "correct_score" and m.correct_score_home is not None and m.correct_score_away is not None and match is not None:
            factors.append(ReasoningFactorOut(
                label="Pick", detail=f"{match.home_team} {m.correct_score_home} - {m.correct_score_away} {match.away_team}",
            ))
        if news is not None:
            factors.append(ReasoningFactorOut(
                label="Situational adjustment (injuries + late-season motivation, moneyline only)",
                detail=f"{news.adjustment_pct:+.1f}pp home-perspective ({news.confidence} confidence): "
                       + "; ".join(f.factor for f in news.factors),
            ))

    methodology = (
        "A walk-forward attack/defense Poisson goal-rating model (see elo_soccer.py), trained on real "
        "match results from football-data.co.uk (EPL/La Liga/Serie A/Bundesliga/Ligue 1) or ESPN (MLS). "
        "Backtested against real historical closing odds for the 5 European leagues: the model does NOT "
        "beat the market for any of the three market types built (moneyline Brier 0.588-0.601 vs market's "
        "0.572-0.587; spread 0.253-0.259 vs 0.249-0.251; total 0.234-0.248 vs 0.227-0.243, consistently "
        "across every league tested) -- shipped anyway as an honest, real-data-derived reference estimate, not a "
        "validated edge. MLS has no free historical-odds source at all, so it can never be backtested "
        "this way -- its ratings are still fit from real results, just never checked against a market "
        "baseline."
    )
    insight = ""
    if match is not None:
        dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
        if dist is not None:
            sseed = f"{match.home_team}|{match.away_team}|{dist.expected_home_goals:.2f}|{dist.expected_away_goals:.2f}"
            eh, ea = dist.expected_home_goals, dist.expected_away_goals
            favored = match.home_team if eh >= ea else match.away_team
            hi, lo = max(eh, ea), min(eh, ea)
            splits = f"{dist.prob_home_win()*100:.0f}% home / {dist.prob_draw()*100:.0f}% draw / {dist.prob_away_win()*100:.0f}% away"
            if abs(eh - ea) < 0.25:
                insight = _seeded_choice(sseed, [
                    f"The goal-rating model sees this one close to level, expecting around {eh:.1f}-{ea:.1f} and splitting out to {splits}. ",
                    f"By the goal model these two project nearly even ({eh:.1f} to {ea:.1f} in expected goals), which lands at {splits}. ",
                ])
            else:
                insight = _seeded_choice(sseed, [
                    f"The goal-rating model leans {favored}, projecting about {hi:.1f}-{lo:.1f} in expected goals and splitting out to {splits}. ",
                    f"This starts with the goal model, which favors {favored} on an expected {hi:.1f}-{lo:.1f} scoreline ({splits}). ",
                    f"{favored} comes out ahead in the goal projection, around {hi:.1f} to {lo:.1f} in expected goals -- {splits}. ",
                ])
    if news is not None and abs(news.adjustment_pct) >= 1.0 and match is not None:
        lean = match.home_team if news.adjustment_pct > 0 else match.away_team
        insight += _seeded_choice(f"{news.adjustment_pct}", [
            f"On top of that, injuries and late-season motivation nudge it {abs(news.adjustment_pct):.1f}pp toward {lean} ({news.confidence} confidence). ",
            f"The situational read then tilts {lean}'s way by {abs(news.adjustment_pct):.1f}pp -- injuries and late-season stakes -- at {news.confidence} confidence. ",
        ])
    if not insight and m.market_type in _FUTURES_MARKET_TYPES:
        comp = m.group_label or "the league"
        team = m.team or "this side"
        what = {
            "league_winner": f"{team} winning {comp}",
            "relegation": f"{team} going down from {comp}",
            "top_half": f"{team} finishing in the top half of {comp}",
            "top4": f"{team} finishing in the top four of {comp}",
            "top2": f"{team} finishing in the top two of {comp}",
        }.get(m.market_type, f"{team} in {comp}")
        insight = _seeded_choice(f"{team}|{m.market_type}|socfut", [
            f"This comes out of a full-season simulation: the app plays {comp}'s remaining fixtures thousands of times off each club's attack/defense goal ratings, and this is how often the final table shows {what}. ",
            f"Priced from a season Monte Carlo of {comp} -- run every remaining match thousands of times on the goal-rating model, then count the share of seasons ending with {what}. ",
            f"The number is read off a simulated run of {comp}: each club's goal ratings drive every remaining fixture, repeated thousands of times, and this tracks {what}. ",
        ])
    insight += _edge_sentence(model_prob, implied)

    return ReasoningOut(
        market_id=m.id,
        market_type=m.market_type,
        label=label,
        model_prob=model_prob,
        market_prob=implied,
        edge=edge,
        insight=insight,
        methodology=methodology,
        factors=factors,
        caveats=[
            "model_validated: false -- this model has not been shown to beat the market.",
            "Situational adjustment (Transfermarkt injuries + ESPN standings-based late-season motivation, "
            "both free/rule-based) is folded into moneyline_3way only -- spread/total/btts still reflect the "
            "pure Poisson baseline.",
        ],
    )
