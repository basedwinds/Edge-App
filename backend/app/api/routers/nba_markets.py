"""NBA markets API -- parallel to routers/markets.py (NFL). Futures now
have a real model too (season_sim_nba.py's Monte Carlo) -- degrades
gracefully to model_prob=None until ESPN publishes the target season's
schedule (see season_sim_service_nba.py's docstring; confirmed live
2026-07-16 that the 2026-27 schedule doesn't exist yet). Game markets
(moneyline/spread/total) have a real model too (Phase 4: elo_service_nba +
game_lines_nba/scoring_ratings_service_nba), gated the same way NFL gates
preseason -- Summer League games get an explicit no_baseline_reason instead
of a number, never a guessed value.

Reuses `_batch_latest_snapshots`/`_implied_prob` from routers/markets.py
directly -- both are already fully sport-agnostic.
"""
import datetime
import json
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice, _WEIGHT_SCORE
from app.api.routers.settings import get_nba_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import FuturesMarketOut, ReasoningFactorOut, ReasoningOut
from app.data.nba_arenas import NBA_TEAM_TZ
from app.db.database import get_session
from app.db.models import Market, NbaGame
from app.ingestion.market_catalog_nba import get_nba_news_adjustment_cache, nba_news_cache_to_pydantic
from app.models import game_lines_nba
from app.models.baseline import elo_service_nba
from app.models.baseline.elo_nba import effective_home_court_adv, implied_elo_diff
from app.models.combine import combine_probability
from app.models.ladder_sanity import find_resolved_entities
from app.models.news_adjustment.schema import NewsAdjustment
from app.models.scoring_ratings_service_nba import get_ratings as get_scoring_ratings
from app.models.season_sim_service_nba import get_results as get_season_sim_results
from app.models.staking import FUTURES_UNIT_SCALE, has_real_trading, is_weekly_market_type, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

router = APIRouter(prefix="/nba", tags=["nba"])

FUTURES_MARKET_TYPES = {
    "championship", "conference_champion", "division_winner",
    "playoff_qualifier", "play_in_qualifier", "best_record", "worst_record", "win_total",
}
FUTURES_SIM_KEY = {
    "championship": "championship_pct",
    "conference_champion": "conf_champ_pct",
    "division_winner": "division_pct",
    "playoff_qualifier": "playoff_pct",
    "play_in_qualifier": "play_in_pct",
    "best_record": "best_record_pct",
    "worst_record": "worst_record_pct",
}
GAME_MARKET_TYPES = {
    "moneyline", "spread", "total", "team_total",
    "spread_1h", "spread_2h", "total_1h", "total_2h",
}

NO_BASELINE_REASONS = {
    "SUMMER": "No baseline -- NBA Summer League rosters are backups/two-way/rookie players, not real team strength.",
    "PRE": "No baseline -- preseason lineups are a coaching decision, not a fair team-strength test.",
}


class NbaMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str  # moneyline | spread | total
    source: str
    team: str | None
    game_label: str | None
    nba_game_id: str | None  # real game id, needed by the frontend's cross-platform dedup key for game-tied rows
    gameday: str | None
    gametime: str | None  # "HH:MM" UTC tip-off time, may be blank far out -- see NbaGame.gametime
    game_type: str | None  # SUMMER | PRE | REG | PLAYIN | POST -- see NbaGame.game_type
    line: float | None
    side: str | None
    no_baseline_reason: str | None
    implied_prob: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    model_prob: float | None
    model_validated: bool
    edge: float | None
    news_adjustment_pct: float | None
    news_confidence: str | None
    news_requires_review: bool  # True if a genuinely unresolved game-time decision exists -- see schema.py
    final_prob: float | None  # moneyline only -- baseline blended with the news adjustment, see combine.py
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None  # "weekly" | "futures" | None


def _to_team_perspective(p_home: float, market: Market, game: NbaGame) -> float:
    return round(p_home, 4) if market.team == game.home_team else round(1 - p_home, 4)


def _moneyline_model_prob(m: Market, game: NbaGame, news: NewsAdjustment | None) -> tuple[float | None, float | None]:
    """Returns (model_prob, final_prob) -- model_prob is the pure Elo
    baseline, final_prob additionally blends the news adjustment (same
    "model_prob = baseline, final_prob = news-blended" split as NFL's
    markets.py, kept distinct rather than overwriting model_prob so the
    honest baseline-only number stays visible too)."""
    p_home = elo_service_nba.get_home_win_prob(game.home_team, game.away_team, game.location, game.home_rest, game.away_rest)
    if p_home is None or m.team is None:
        return None, None
    model_prob = _to_team_perspective(p_home, m, game)
    p_home_final = combine_probability(p_home, news, is_divisional=False)  # NBA has no divisional-squeeze effect, see elo_nba.py
    final_prob = _to_team_perspective(p_home_final, m, game)
    return model_prob, final_prob


def _spread_model_prob(m: Market, game: NbaGame, news: NewsAdjustment | None) -> float | None:
    """model_prob here already reflects the news blend (folded in via
    implied_elo_diff), same convention as NFL's spread handling -- no
    separate final_prob for spread."""
    if m.team is None or m.line is None:
        return None
    p_home = elo_service_nba.get_home_win_prob(game.home_team, game.away_team, game.location, game.home_rest, game.away_rest)
    if p_home is None:
        return None
    p_home_final = combine_probability(p_home, news, is_divisional=False)
    elo_diff_effective = implied_elo_diff(p_home_final)
    return round(game_lines_nba.prob_team_covers(m.team == game.home_team, m.line, elo_diff_effective), 4)


def _total_model_prob_for_game(m: Market, game: NbaGame) -> float | None:
    if m.line is None:
        return None
    ratings = get_scoring_ratings()
    home_scoring = ratings.get(game.home_team)
    away_scoring = ratings.get(game.away_team)
    return round(game_lines_nba.prob_over(m.line, home_scoring, away_scoring), 4)


def _team_total_model_prob(m: Market, game: NbaGame) -> float | None:
    if m.team is None or m.line is None:
        return None
    ratings = get_scoring_ratings()
    scoring = ratings.get(m.team)
    opponent = game.away_team if m.team == game.home_team else game.home_team
    opponent_scoring = ratings.get(opponent)
    return round(game_lines_nba.prob_team_over(m.line, scoring, opponent_scoring), 4)


def _half_spread_model_prob(m: Market, game: NbaGame, news: NewsAdjustment | None, half: int) -> float | None:
    if m.team is None or m.line is None:
        return None
    p_home = elo_service_nba.get_home_win_prob(game.home_team, game.away_team, game.location, game.home_rest, game.away_rest)
    if p_home is None:
        return None
    p_home_final = combine_probability(p_home, news, is_divisional=False)
    elo_diff_effective = implied_elo_diff(p_home_final)
    return round(game_lines_nba.prob_team_covers_half(m.team == game.home_team, m.line, elo_diff_effective, half), 4)


def _half_total_model_prob(m: Market, game: NbaGame, half: int) -> float | None:
    if m.line is None:
        return None
    ratings = get_scoring_ratings()
    home_scoring = ratings.get(game.home_team)
    away_scoring = ratings.get(game.away_team)
    return round(game_lines_nba.prob_over_half(m.line, home_scoring, away_scoring, half), 4)


def _futures_model_prob(m: Market, sim_results: dict) -> float | None:
    if m.team is None:
        return None
    team_sim = sim_results.get(m.team)
    if team_sim is None:
        return None
    if m.market_type == "win_total":
        # over/under ladder: line = "at least N wins" -- sum the histogram's tail.
        win_count_pct = team_sim.get("win_count_pct")
        idx = int(m.line) if m.line is not None else None
        if win_count_pct and idx is not None and 0 <= idx < len(win_count_pct):
            return round(sum(win_count_pct[idx:]), 4)
        return None
    sim_key = FUTURES_SIM_KEY.get(m.market_type)
    return round(team_sim[sim_key], 4) if (sim_key and sim_key in team_sim) else None


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_nba_futures(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "nba", Market.market_type.in_(FUTURES_MARKET_TYPES), Market.status == "active").all()
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    sim_results = get_season_sim_results()
    weekly_pool, futures_pool = get_nba_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)
    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        model_prob = _futures_model_prob(m, sim_results)
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "nba", m.market_type)
        stake_dollars = size_stake_dollars(staking_mode, kelly, futures_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=FUTURES_UNIT_SCALE)  # every FUTURES_MARKET_TYPES entry is a futures-pool market
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
                edge=round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None,
                kelly_fraction=kelly,
                suggested_stake_dollars=stake_dollars,
                suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
                stake_pool="futures" if kelly is not None else None,
                line_move_pp=None,
            )
        )
    out.sort(key=lambda m: (m.market_type, -(m.implied_prob or 0)))
    return out


def _batch_news_adjustments(session: Session, game_ids: set[str]) -> dict[str, NewsAdjustment]:
    if not game_ids:
        return {}
    from app.db.models import NbaNewsAdjustmentCache

    rows = session.query(NbaNewsAdjustmentCache).filter(NbaNewsAdjustmentCache.nba_game_id.in_(game_ids)).all()
    return {row.nba_game_id: nba_news_cache_to_pydantic(row) for row in rows}


@router.get("/markets", response_model=list[NbaMarketOut])
def list_nba_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "nba", Market.market_type.in_(GAME_MARKET_TYPES), Market.status == "active").all()
    game_ids = {m.nba_game_id for m in markets if m.nba_game_id}
    games_by_id = {g.id: g for g in session.query(NbaGame).filter(NbaGame.id.in_(game_ids)).all()} if game_ids else {}
    # Skip markets tied to a game that's already final -- once the poller
    # stops refreshing a played game's market, its price is frozen while this
    # endpoint would otherwise keep computing a fresh model_prob off current
    # Elo, "predicting" an already-decided game. Same fix as mlb_markets.py.
    def _game_already_final(m: Market) -> bool:
        game = games_by_id.get(m.nba_game_id) if m.nba_game_id else None
        return game is not None and game.home_score is not None

    # SECOND, related gap (confirmed real but not yet triggered -- current
    # NBA games are all Summer League with no baseline anyway, same reasoning
    # documented in mlb_markets.py's own version of this fix, fixed here
    # proactively before the regular season makes it bite the way it did for
    # MLB): the check above only excludes games with a recorded FINAL score,
    # but a game that's simply IN PROGRESS has no home_score yet either. Same
    # day-boundary-safe conversion as mlb_markets.py::_game_kickoff_local --
    # NbaGame.gametime is a raw UTC clock reading with no date attached (like
    # MLB, unlike NFL's local gametime), so naively combining it with the
    # local `gameday` can land on the wrong UTC calendar day for evening
    # games at negative UTC offsets.
    def _game_kickoff_utc(gameday: str, gametime: str, tz_name: str) -> datetime.datetime:
        gday = datetime.date.fromisoformat(gameday)
        gtime = datetime.time.fromisoformat(gametime)
        for day_offset in (0, 1):
            candidate_utc = datetime.datetime.combine(gday + datetime.timedelta(days=day_offset), gtime, tzinfo=datetime.timezone.utc)
            if candidate_utc.astimezone(ZoneInfo(tz_name)).date() == gday:
                return candidate_utc
        return datetime.datetime.combine(gday, gtime, tzinfo=datetime.timezone.utc)  # neither candidate round-tripped -- naive fallback rather than guessing further

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _game_already_started(m: Market) -> bool:
        game = games_by_id.get(m.nba_game_id) if m.nba_game_id else None
        if game is None or not game.gameday or not game.gametime:
            return False
        tz_name = NBA_TEAM_TZ.get(game.home_team)
        if tz_name is None:
            return False
        return now_utc >= _game_kickoff_utc(game.gameday, game.gametime, tz_name)

    # THIRD gap (2026-07-19, found while chasing the same class of bug for
    # Tennis/MMA/MLB -- see ladder_sanity.py): the checks above depend on
    # this app's own schedule data being right about the game's real kickoff
    # instant. Detected structurally instead, as a second layer: a real,
    # still-live pregame ladder (spread/total/team_total, any half-variant)
    # never prices two DIFFERENT thresholds at the same extreme value --
    # seeing that happen is a direct tell the real outcome is already locked
    # in, independent of any timestamp this app stores.
    all_snapshots_pre = _batch_latest_snapshots(session, [m.id for m in markets])
    _LADDER_TYPES = {"spread", "total", "team_total", "spread_1h", "spread_2h", "total_1h", "total_2h"}
    ladder_groups: dict[tuple, list[tuple[float, float]]] = {}
    for m in markets:
        if m.line is None or m.nba_game_id is None or m.market_type not in _LADDER_TYPES:
            continue
        implied = _implied_prob(all_snapshots_pre.get(m.id))
        if implied is None:
            continue
        ladder_groups.setdefault((m.nba_game_id, m.market_type, m.team), []).append((m.line, implied))
    resolved_group_keys = find_resolved_entities(ladder_groups)
    games_with_resolved_ladder = {key[0] for key in resolved_group_keys}

    def _game_ladder_resolved(m: Market) -> bool:
        return m.nba_game_id in games_with_resolved_ladder

    markets = [
        m for m in markets
        if not _game_already_final(m) and not _game_already_started(m) and not _game_ladder_resolved(m)
    ]
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    news_by_game = _batch_news_adjustments(session, game_ids)
    weekly_pool, futures_pool = get_nba_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        game = games_by_id.get(m.nba_game_id)
        implied = _implied_prob(snap)
        news = news_by_game.get(m.nba_game_id)

        model_prob = final_prob = None
        no_baseline_reason = None
        if game is not None:
            no_baseline_reason = NO_BASELINE_REASONS.get(game.game_type)
            if no_baseline_reason is None:
                if m.market_type == "moneyline":
                    model_prob, final_prob = _moneyline_model_prob(m, game, news)
                elif m.market_type == "spread":
                    model_prob = _spread_model_prob(m, game, news)
                elif m.market_type == "total":
                    model_prob = _total_model_prob_for_game(m, game)
                elif m.market_type == "team_total":
                    model_prob = _team_total_model_prob(m, game)
                elif m.market_type in ("spread_1h", "spread_2h"):
                    model_prob = _half_spread_model_prob(m, game, news, 1 if m.market_type == "spread_1h" else 2)
                elif m.market_type in ("total_1h", "total_2h"):
                    model_prob = _half_total_model_prob(m, game, 1 if m.market_type == "total_1h" else 2)

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "nba", m.market_type)
        pool = weekly_pool if is_weekly_market_type(m.market_type) else futures_pool
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full)

        out.append(
            NbaMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                game_label=f"{game.away_team} @ {game.home_team}" if game else None,
                nba_game_id=m.nba_game_id,
                gameday=game.gameday if game else None,
                gametime=game.gametime if game else None,
                game_type=game.game_type if game else None,
                line=m.line,
                side=m.side,
                no_baseline_reason=no_baseline_reason,
                implied_prob=implied,
                last_price=snap.last_price if snap else None,
                volume=snap.volume if snap else None,
                updated_at=m.updated_at.isoformat() if m.updated_at else None,
                news_adjustment_pct=news.adjustment_pct if news else None,
                news_confidence=news.confidence if news else None,
                news_requires_review=bool(news.requires_review) if news else False,
                final_prob=final_prob,
                model_prob=model_prob,
                model_validated=False,
                edge=round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None,
                kelly_fraction=kelly,
                suggested_stake_dollars=stake_dollars,
                suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
                stake_pool=("weekly" if is_weekly_market_type(m.market_type) else "futures") if kelly is not None else None,
            )
        )
    out.sort(key=lambda m: (m.gameday or "9999", m.game_label or ""))
    return out


# Fallback methodology text for futures market types not given a dedicated
# insight sentence below -- mirrors routers/markets.py's _FALLBACK_METHODOLOGY
# dict, scoped to what NBA actually has (no awards/division-extra/leader-stat
# futures exist for NBA yet, unlike NFL).
_FALLBACK_METHODOLOGY_NBA = {
    "win_total": "Over/under ladder: read directly from the season Monte Carlo's per-team win-count histogram (2,000 trials), summing the tail at and above the line.",
}


def _moneyline_insight_nba(
    home_team: str, away_team: str, home_r: float | None, away_r: float | None,
    hca: float, is_neutral: bool, factors_raw: list[dict], model_prob: float | None, market_prob: float | None,
) -> str:
    seed = f"{home_team}|{away_team}|{home_r}|{away_r}"

    sit_lean = None
    sit_wash = False
    top_name = None
    top_rat = ""
    if factors_raw:
        home_score = sum(_WEIGHT_SCORE.get(f.get("weight"), 0) for f in factors_raw if f.get("direction") == "favor_home")
        away_score = sum(_WEIGHT_SCORE.get(f.get("weight"), 0) for f in factors_raw if f.get("direction") == "favor_away")
        top = max(factors_raw, key=lambda f: _WEIGHT_SCORE.get(f.get("weight"), 0))
        top_name = top.get("factor") or "an unnamed factor"
        top_rat = top.get("rationale", "")
        if home_score == away_score:
            sit_wash = True
        else:
            sit_lean = home_team if home_score > away_score else away_team

    def situational() -> str:
        if sit_lean:
            rat = f" ({top_rat})" if top_rat else ""
            return _seeded_choice(seed + "s", [
                f"From there, the situational side -- injuries, load management, schedule spot -- leans {sit_lean}'s way, led by {top_name}{rat}.",
                f"On top of that, the off-court picture tilts {sit_lean}, with {top_name}{rat} the driver.",
                f"Layer in availability and rest and it favors {sit_lean} a touch, headlined by {top_name}{rat}.",
                f"The situational read then nudges {sit_lean}'s way, most notably {top_name}{rat}.",
            ])
        if sit_wash:
            return _seeded_choice(seed + "s", [
                f"The situational layer -- injuries, load management, schedule spot -- nets out about even; {top_name} is worth a note but doesn't really tilt it.",
                f"Off the court it's roughly a wash, {top_name} aside, so nothing there swings the read.",
            ])
        return _seeded_choice(seed + "s", [
            "There's nothing on the situational sheet yet -- no injury, load-management, schedule, or coaching flags -- so the rating carries it.",
            "No availability or rest notes are on file, so this comes down to the ratings alone.",
        ])

    story = ""
    if home_r is not None and away_r is not None:
        gap = home_r - away_r
        hca_note = ("and on a neutral floor home court is off the table" if is_neutral
                    else f"with {home_team} adding a {hca:.0f}-point home-court bump (altitude and rest already rolled in)")
        stronger, s_r, weaker, w_r = (home_team, home_r, away_team, away_r) if gap > 0 else (away_team, away_r, home_team, home_r)
        if abs(gap) < 25:
            setup = _seeded_choice(seed, [
                f"This one projects tight -- Elo has {home_team} and {away_team} nearly level ({home_r:.0f} to {away_r:.0f}), {hca_note}.",
                f"There's little between these two on the ratings ({home_r:.0f} to {away_r:.0f}), {hca_note}.",
                f"About as even as it gets: {home_team} and {away_team} sit close ({home_r:.0f} to {away_r:.0f}), {hca_note}.",
                f"Elo can barely separate them ({home_r:.0f} to {away_r:.0f}), {hca_note}.",
            ])
        elif abs(gap) >= 45:
            setup = _seeded_choice(seed, [
                f"This is {stronger}'s game to lose on paper -- Elo has them a clear {abs(gap):.0f} points over {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}.",
                f"{stronger} is the decided side, a full {abs(gap):.0f} Elo points above {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}.",
                f"The ratings make {stronger} the class here, {abs(gap):.0f} clear of {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}.",
            ])
        else:
            setup = _seeded_choice(seed, [
                f"{stronger} comes in as the side Elo prefers, {abs(gap):.0f} points up on {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}.",
                f"The lean is {stronger}, who rate {abs(gap):.0f} points ahead of {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}.",
                f"Elo gives the edge to {stronger}, {abs(gap):.0f} to the good over {weaker} ({s_r:.0f} to {w_r:.0f}), {hca_note}.",
            ])
        story = f"{setup} {situational()}"
    else:
        story = situational()

    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _margin_insight_nba(home_team: str, away_team: str, home_r: float | None, away_r: float | None, model_prob: float | None, market_prob: float | None) -> str:
    if home_r is not None and away_r is not None:
        gap = home_r - away_r
        seed = f"{home_team}|{away_team}|{home_r}|{away_r}|m"
        stronger, s_r, weaker, w_r = (home_team, home_r, away_team, away_r) if gap >= 0 else (away_team, away_r, home_team, home_r)
        story = _seeded_choice(seed, [
            f"The number's fair value rides mostly on the {abs(gap):.0f}-point Elo edge {stronger} holds over {weaker}.",
            f"Most of this spread traces to the {abs(gap):.0f}-point rating gap between {stronger} and {weaker}.",
            f"What sets the line is the {abs(gap):.0f}-Elo cushion {stronger} carries on {weaker}.",
        ])
    else:
        story = ""
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _total_insight_nba(model_prob: float | None, market_prob: float | None) -> str:
    seed = f"{model_prob}|{market_prob}|nbatot"
    lead = _seeded_choice(seed, [
        "This one's built off each team's own recent scoring pace blended with their opponent's, rather than a single strength number.",
        "The total works from how the two sides have actually been scoring and conceding lately, not one overall rating.",
        "Here the model leans on each team's recent pace against the other's, rather than a head-to-head strength read.",
    ])
    tail = " There's no divisional/dome/turf structural shift to layer on the way there is for NFL -- checked, and there's no comparable NBA effect."
    return f"{lead}{tail} {_edge_sentence(model_prob, market_prob)}".strip()


def _futures_sim_insight_nba(team: str, rating: float | None, team_sim: dict, model_prob: float | None, market_prob: float | None) -> str:
    if rating is not None:
        playoff_pct = team_sim.get("playoff_pct")
        champ_pct = team_sim.get("championship_pct")
        seed = f"{team}|{rating}|nbafut"
        if playoff_pct is not None and champ_pct is not None:
            story = _seeded_choice(seed, [
                f"This comes out of a full season simulation anchored on {team}'s current Elo ({rating:.0f}) -- run it forward and they land around a {playoff_pct * 100:.0f}% chance at the playoffs (play-in included) and {champ_pct * 100:.0f}% at the title overall.",
                f"{team}'s rating ({rating:.0f}) is carried through a rest-of-season sim to get here, which also works out to roughly {playoff_pct * 100:.0f}% to reach the playoffs (with the play-in) and {champ_pct * 100:.0f}% to win the championship.",
                f"Behind this number is a season-long simulation off {team}'s Elo ({rating:.0f}); the same run gives them about a {playoff_pct * 100:.0f}% playoff shot (play-in and all) and {champ_pct * 100:.0f}% at the ring.",
            ])
        else:
            story = _seeded_choice(seed, [
                f"This is a season simulation built off {team}'s current Elo ({rating:.0f}), carried across the rest of the schedule.",
                f"{team}'s rating ({rating:.0f}) drives this through a rest-of-season simulation.",
            ])
    else:
        story = ""
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_nba_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    """NBA equivalent of routers/markets.py::get_market_reasoning -- same
    "explain how the number passed in was derived, don't recompute it"
    contract. Deliberately leaner than NFL's version: no awards/division-
    extra/leader-stat/season-stat futures exist for NBA yet, so only game
    markets (moneyline/spread/team_total/totals + half variants) and the
    season-sim futures/win_total ladder need branches here."""
    m = session.get(Market, market_id)
    if m is None or m.sport != "nba":
        raise HTTPException(404, "market not found")
    game = session.get(NbaGame, m.nba_game_id) if m.nba_game_id else None
    label = f"{game.away_team} @ {game.home_team}" if game else (m.group_label or m.market_type)
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    caveats = ["model_validated: false -- no model in this app has been shown to beat the market in backtesting (NBA has no free historical odds source to even run that check against -- see Backtests)."]
    methodology = _FALLBACK_METHODOLOGY_NBA.get(m.market_type, "No detailed methodology available for this market type yet.")
    insight = ""

    if m.market_type == "moneyline" and game is not None:
        home_r = elo_service_nba.get_team_rating(game.home_team)
        away_r = elo_service_nba.get_team_rating(game.away_team)
        methodology = (
            "Elo rating model, blended with a free rule-based news/situational layer (injuries with a "
            "player-value proxy, load management near a clinched playoff seed, trap/letdown schedule "
            "spots, coaching changes) when a factor is on file. Calibration-checked against real "
            "outcomes (no free historical NBA odds source exists to run a market-beating go/no-go the "
            "way NFL's moneyline backtest does -- see Backtests)."
        )
        if home_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} Elo rating", detail=f"{home_r:.0f}"))
        if away_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} Elo rating", detail=f"{away_r:.0f}"))
        hca = effective_home_court_adv(game.home_team, game.location, game.home_rest, game.away_rest)
        is_neutral = game.location == "Neutral"
        hca_detail = f"{hca:.0f} Elo points"
        if is_neutral:
            hca_detail += " (neutral site -- zeroed out)"
        elif game.home_team in ("DEN", "UTAH"):
            hca_detail += " (includes an altitude bonus)"
        factors.append(ReasoningFactorOut(label="Home-court advantage applied", detail=hca_detail))
        news_cache = get_nba_news_adjustment_cache(session, game.id)
        factors_raw: list[dict] = []
        if news_cache:
            factors_raw = json.loads(news_cache.factors_json)
            for f in factors_raw:
                name = f.get("factor", "Situational factor")
                direction = f.get("direction", "neutral").replace("_", " ")
                weight = f.get("weight", "")
                rationale = f.get("rationale", "")
                detail = f"{weight} weight, {direction}" if weight else direction
                if rationale:
                    detail += f" -- {rationale}"
                factors.append(ReasoningFactorOut(label=name, detail=detail))
            factors.append(ReasoningFactorOut(label="Combined news adjustment", detail=f"{news_cache.adjustment_pct:+.1f}pp ({news_cache.confidence} confidence)"))
        else:
            factors.append(ReasoningFactorOut(label="Situational factors", detail="none on file for this game yet"))
        insight = _moneyline_insight_nba(game.home_team, game.away_team, home_r, away_r, hca, is_neutral, factors_raw, model_prob, market_prob)

    elif m.market_type in ("spread", "spread_1h", "spread_2h") and game is not None:
        methodology = (
            "Margin-space probability model: Normal-distribution approximation, mean derived from "
            "the Elo rating difference, blended with the same news/situational layer as moneyline via "
            "an inverted-Elo-diff bridge."
        )
        if m.market_type in ("spread_1h", "spread_2h"):
            methodology += " Half-scoring split derived from real halftime PBP splits."
        home_r = elo_service_nba.get_team_rating(game.home_team)
        away_r = elo_service_nba.get_team_rating(game.away_team)
        if home_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} Elo rating", detail=f"{home_r:.0f}"))
        if away_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} Elo rating", detail=f"{away_r:.0f}"))
        insight = _margin_insight_nba(game.home_team, game.away_team, home_r, away_r, model_prob, market_prob)

    elif m.market_type in ("total", "total_1h", "total_2h") and game is not None:
        methodology = (
            "Points-total probability model: Normal-distribution approximation over each team's "
            "trailing scoring rate blended with their opponent's."
        )
        if m.market_type in ("total_1h", "total_2h"):
            methodology += " Half-scoring split derived from real halftime PBP splits."
        insight = _total_insight_nba(model_prob, market_prob)

    elif m.market_type == "team_total" and game is not None:
        methodology = (
            "Points-total probability model applied to a single team: this team's trailing scoring "
            "rate blended with their opponent's trailing points-allowed rate."
        )
        opponent = game.away_team if m.team == game.home_team else game.home_team
        if m.team:
            factors.append(ReasoningFactorOut(label="Team", detail=m.team))
        if opponent:
            factors.append(ReasoningFactorOut(label="Opponent", detail=opponent))
        insight = _total_insight_nba(model_prob, market_prob)

    elif game is None and m.team and (m.market_type in FUTURES_SIM_KEY or m.market_type == "win_total"):
        methodology = (
            "Season Monte Carlo simulation (2,000 trials) using current Elo ratings and the real "
            "remaining schedule, with the real top-6-seeds-direct + play-in tournament format and "
            "best-of-7 series (fixed bracket, no reseeding)."
        )
        if m.market_type == "win_total":
            methodology = _FALLBACK_METHODOLOGY_NBA["win_total"]
        rating = elo_service_nba.get_team_rating(m.team)
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} current Elo rating", detail=f"{rating:.0f}"))
        sim_results = get_season_sim_results()
        team_sim = sim_results.get(m.team) or {}
        for sim_key, sim_label in [
            ("division_pct", "Division win probability"),
            ("conf_champ_pct", "Conference championship probability"),
            ("playoff_pct", "Playoff probability (incl. play-in)"),
            ("championship_pct", "Championship probability"),
            ("best_record_pct", "Best regular-season record probability"),
        ]:
            if sim_key in team_sim and sim_key != FUTURES_SIM_KEY.get(m.market_type):
                factors.append(ReasoningFactorOut(label=sim_label, detail=f"{team_sim[sim_key] * 100:.1f}%"))
        caveats.append("Playoff seeding uses simplified tiebreakers (win total, then a coin flip), not the NBA's real tiebreaker rules.")
        insight = _futures_sim_insight_nba(m.team, rating, team_sim, model_prob, market_prob)

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
