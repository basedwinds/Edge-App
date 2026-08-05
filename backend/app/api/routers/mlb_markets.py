"""MLB markets API -- parallel to nba_markets.py.

Moneyline and spread (run-line) are real models (Elo + starting-pitcher
blend for moneyline; the same blend fed through a real, derived margin
regression for spread -- see game_lines_mlb.py). Total/team_total use
league-average runs PLUS a real, derived per-park adjustment (see
game_lines_mlb.py::PARK_FACTOR) -- team-BEHAVIOR signals (trailing team
scoring, starting-pitcher ERA) were checked and confirmed NOT to beat naive
for MLB totals, unlike NFL/NBA where a team-scoring blend helped, but the
structural park-factor signal (real, well-documented effects like Coors
Field's altitude) IS real and validated. Still labeled with a caveat via
`no_baseline_reason` even though the estimate is real, since it's a
materially narrower claim (park-driven, not team-specific) than the
team-behavior models the label's absence implies elsewhere in this app.
Futures use model_prob=None (no season-simulation model built for MLB yet).

Reuses `_batch_latest_snapshots`/`_implied_prob` from routers/markets.py
directly -- both are already fully sport-agnostic.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import datetime
import json
import math
from zoneinfo import ZoneInfo

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice, _WEIGHT_SCORE
from app.api.routers.settings import get_mlb_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import FuturesMarketOut, ReasoningFactorOut, ReasoningOut
from app.clients import weather_client
from app.data.mlb_ballparks import BALLPARKS, ORIENTATION_DEG, TEAM_TZ
from app.db.database import get_session
from app.db.models import Market, MlbGame, MlbNewsAdjustmentCache
from app.ingestion.market_catalog_mlb import get_mlb_news_adjustment_cache, mlb_news_cache_to_pydantic
from app.models import game_lines_mlb
from app.models.baseline import elo_mlb, elo_service_mlb
from app.models.combine import combine_probability
from app.models.ladder_sanity import find_resolved_entities
from app.models.news_adjustment.schema import NewsAdjustment
from app.models.season_sim_service_mlb import get_results as get_mlb_season_sim_results
from app.models.staking import FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, is_weekly_market_type, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

router = APIRouter(prefix="/mlb", tags=["mlb"])

FUTURES_MARKET_TYPES = {
    "championship", "conference_champion", "division_winner",
    "playoff_qualifier", "best_record", "worst_record", "win_total",
}
# "conference_champion" naming kept for schema parity with NFL/NBA -- these
# are the real AL/NL PENNANT winners, see season_sim_mlb.py's "pennant_pct".
FUTURES_SIM_KEY = {
    "championship": "championship_pct",
    "conference_champion": "pennant_pct",
    "division_winner": "division_pct",
    "playoff_qualifier": "playoff_pct",
    "best_record": "best_record_pct",
    "worst_record": "worst_record_pct",
}
GAME_MARKET_TYPES = {"moneyline", "spread", "total", "team_total", "f5", "rfi"}

NAIVE_TOTAL_NOTE = (
    "League-average runs adjusted for a real, derived ballpark factor (e.g. Coors Field runs well "
    "above average) -- team-scoring-rate and starting-pitcher-ERA signals were checked and confirmed "
    "NOT to help for MLB totals, unlike NFL/NBA (see game_lines_mlb.py)."
)
NO_MODEL_REASONS = {
    "total": NAIVE_TOTAL_NOTE,
    "team_total": NAIVE_TOTAL_NOTE,
}


class MlbMarketOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: int
    market_type: str
    source: str
    team: str | None
    game_label: str | None
    mlb_game_id: str | None
    gameday: str | None
    gametime: str | None
    game_type: str | None  # R | S | F | D | L | W | A -- see MlbGame.game_type
    home_probable_pitcher: str | None
    away_probable_pitcher: str | None
    line: float | None
    side: str | None
    no_baseline_reason: str | None
    implied_prob: float | None
    last_price: float | None
    volume: float | None
    updated_at: str | None
    news_adjustment_pct: float | None
    news_confidence: str | None
    news_requires_review: bool  # True if a genuinely unresolved game-time decision exists -- see schema.py
    final_prob: float | None  # moneyline only -- baseline blended with the news adjustment, see combine.py
    model_prob: float | None
    model_validated: bool
    edge: float | None
    kelly_fraction: float | None
    suggested_stake_dollars: float | None
    suggested_stake_units: float | None
    stake_pool: str | None


def _to_team_perspective(p_home: float, market: Market, game: MlbGame) -> float:
    return round(p_home, 4) if market.team == game.home_team else round(1 - p_home, 4)


def _game_kickoff_local(gameday: str, gametime: str, tz_name: str) -> datetime.datetime:
    """Resolves this project's already-documented gameday/gametime ambiguity
    (see project memory / RecommendedBetsTable.tsx's formatGameDate) --
    `gameday` is the LOCAL calendar date (MLB Stats API's own officialDate,
    ground truth), `gametime` is a raw UTC clock reading with NO date
    attached. Naively combining them as `f"{gameday}T{gametime}:00Z"`
    silently assumes the UTC calendar day equals the local one, which is
    FALSE for evening games at negative UTC offsets (the real UTC instant is
    on gameday+1) -- caught live while wiring up the real temperature signal
    below: it was returning "no forecast" for TODAY's Coors Field game
    because the miscalculated instant landed a day in the past. Fixed
    properly here (not just documented as a known gap) by trying both
    candidate UTC days and keeping whichever one's LOCAL conversion actually
    lands back on `gameday` -- both halves of the input are individually
    correct, only their day-pairing was ambiguous, so this isn't a guess."""
    tz = ZoneInfo(tz_name)
    for day_offset in (0, 1):
        candidate_date = datetime.date.fromisoformat(gameday) + datetime.timedelta(days=day_offset)
        candidate_utc = datetime.datetime.fromisoformat(f"{candidate_date.isoformat()}T{gametime}:00+00:00")
        candidate_local = candidate_utc.astimezone(tz)
        if candidate_local.date().isoformat() == gameday:
            return candidate_local
    # Neither candidate round-tripped back to `gameday` (shouldn't happen for
    # a real gametime) -- fall back to the naive same-day reading rather than
    # guess further.
    return datetime.datetime.fromisoformat(f"{gameday}T{gametime}:00+00:00").astimezone(tz)


def _game_weather(game: MlbGame) -> dict | None:
    """Real live forecast {temp_f, out_wind_mph} at first pitch, for the
    real, checked TEMP_SLOPE/OUT_WIND_SLOPE signals in game_lines_mlb.py --
    None for domed/retractable parks (not in BALLPARKS, see that module's
    docstring for why they're excluded rather than guessed at) or when the
    game is too far out for a forecast yet (Open-Meteo's ~16-day window).
    `out_wind_mph` is the signed wind-blowing-OUT component relative to this
    park's own real, sourced orientation (ORIENTATION_DEG) -- None (not 0.0)
    when the park has no orientation on file, same "unknown = no adjustment"
    convention rather than assuming a neutral crosswind."""
    if not game.gameday or not game.gametime:
        return None
    ballpark = BALLPARKS.get(game.home_team)
    if ballpark is None:
        return None
    game_local = _game_kickoff_local(game.gameday, game.gametime, ballpark["tz"]).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    weather = weather_client.fetch_hourly_weather(
        ballpark["lat"], ballpark["lon"], game_local.strftime("%Y-%m-%dT%H:%M"), ballpark["tz"]
    )
    if weather is None:
        return None
    out_wind_mph = None
    orientation = ORIENTATION_DEG.get(game.home_team)
    if orientation is not None:
        blowing_out_from_deg = (orientation + 180.0) % 360.0
        out_wind_mph = weather["wind_mph"] * math.cos(math.radians(weather["wind_dir"] - blowing_out_from_deg))
    return {"temp_f": weather["temp_f"], "out_wind_mph": out_wind_mph}


def _moneyline_model_prob(m: Market, game: MlbGame, news: NewsAdjustment | None) -> tuple[float | None, float | None]:
    """Returns (model_prob, final_prob) -- model_prob is the pure Elo+
    pitcher-blend baseline, final_prob additionally blends the injury
    adjustment (same "model_prob = baseline, final_prob = news-blended"
    split as NFL/NBA's markets.py)."""
    if m.team is None:
        return None, None
    p_home = elo_service_mlb.get_home_win_prob(
        game.home_team, game.away_team, game.season, game.location,
        game.home_probable_pitcher_id, game.away_probable_pitcher_id,
    )
    if p_home is None:
        return None, None
    model_prob = _to_team_perspective(p_home, m, game)
    # Checked (not just left unchecked) 2026-07-17, see scripts/check_mlb_divisional_signal.py:
    # MLB does NOT have NFL's real divisional-squeeze effect -- if anything the OPPOSITE
    # (divisional favorites win MORE often than Elo predicts at every mismatch bucket, and
    # Elo's own Brier is BETTER for divisional games, not worse). Real total-suppression
    # check also came back null (z=0.35, noise). Kept at False deliberately, not a stale default.
    p_home_final = combine_probability(p_home, news, is_divisional=False)
    final_prob = _to_team_perspective(p_home_final, m, game)
    return model_prob, final_prob


def _spread_model_prob(m: Market, game: MlbGame, news: NewsAdjustment | None) -> float | None:
    """Folds the situational/news (position-player injury) layer into the
    margin-space run-line model, mirroring NFL's `_spread_model_prob`
    exactly: blend the SAME win-probability adjustment moneyline uses
    (combine_probability), then invert back to an effective elo_diff
    (elo_mlb.py::implied_elo_diff, already built for exactly this in an
    earlier round but never wired in) rather than hand-building a second,
    separate margin-space version of the injury signal. Previously pure
    Elo-margin with no news blend at all -- caught in the Phase 10 polish
    audit (2026-07-17), the same gap NFL had already closed."""
    if m.team is None or m.line is None:
        return None
    p_home_baseline = elo_service_mlb.get_home_win_prob(
        game.home_team, game.away_team, game.season, game.location,
        game.home_probable_pitcher_id, game.away_probable_pitcher_id,
    )
    if p_home_baseline is None:
        return None
    p_home_final = combine_probability(p_home_baseline, news, is_divisional=False)  # see _moneyline_model_prob's comment -- checked, MLB has no real divisional effect
    elo_diff_effective = elo_mlb.implied_elo_diff(p_home_final)
    return round(game_lines_mlb.prob_team_covers(m.team == game.home_team, m.line, elo_diff_effective), 4)


def _total_model_prob(m: Market, game: MlbGame) -> float | None:
    if m.line is None:
        return None
    weather = _game_weather(game) or {}
    p_over = game_lines_mlb.prob_over(m.line, game.home_team, weather.get("temp_f"), weather.get("out_wind_mph"))
    return round(p_over if m.side != "under" else 1.0 - p_over, 4)


def _team_total_model_prob(m: Market, game: MlbGame) -> float | None:
    if m.line is None:
        return None
    weather = _game_weather(game) or {}
    p_over = game_lines_mlb.prob_team_over(m.line, game.home_team, weather.get("temp_f"), weather.get("out_wind_mph"))
    return round(p_over if m.side != "under" else 1.0 - p_over, 4)


def _f5_model_prob(m: Market, game: MlbGame) -> float | None:
    elo_diff = elo_service_mlb.get_elo_diff(
        game.home_team, game.away_team, game.season, game.location,
        game.home_probable_pitcher_id, game.away_probable_pitcher_id,
    )
    if elo_diff is None:
        return None
    p_home, p_away, p_tie = game_lines_mlb.prob_f5_outcome(elo_diff)
    if m.side == "tie":
        return round(p_tie, 4)
    if m.team == game.home_team:
        return round(p_home, 4)
    if m.team == game.away_team:
        return round(p_away, 4)
    return None


def _rfi_model_prob(m: Market, game: MlbGame) -> float | None:
    combined_era = elo_service_mlb.get_combined_era(
        game.season, game.home_probable_pitcher_id, game.away_probable_pitcher_id,
    )
    p_rfi = game_lines_mlb.prob_rfi(combined_era)
    return round(p_rfi if m.side != "no" else 1.0 - p_rfi, 4)


def _futures_model_prob(m: Market, sim_results: dict) -> float | None:
    """Real season Monte Carlo simulation (season_sim_mlb.py, 2026-07-17 --
    previously always None, no model built yet) -- team-Elo only (no
    starting-pitcher blend, unknowable that far ahead), 2000 trials, real
    2022+ postseason bracket (6 teams/league, no reseeding, verified live
    against the actual rule)."""
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
def list_mlb_futures(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "mlb", Market.market_type.in_(FUTURES_MARKET_TYPES), Market.status == "active").all()
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    sim_results = get_mlb_season_sim_results()
    weekly_pool, futures_pool = get_mlb_pool_dollars(session)
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
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "mlb", m.market_type)
        stake_dollars = size_stake_dollars(staking_mode, kelly, futures_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=FUTURES_UNIT_SCALE, min_market_price=FUTURES_MIN_MARKET_PRICE)
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


def _batch_mlb_news_adjustments(session: Session, game_ids: set[str]) -> dict[str, NewsAdjustment]:
    if not game_ids:
        return {}
    rows = session.query(MlbNewsAdjustmentCache).filter(MlbNewsAdjustmentCache.mlb_game_id.in_(game_ids)).all()
    return {row.mlb_game_id: mlb_news_cache_to_pydantic(row) for row in rows}


@router.get("/markets", response_model=list[MlbMarketOut])
def list_mlb_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "mlb", Market.market_type.in_(GAME_MARKET_TYPES), Market.status == "active").all()
    game_ids = {m.mlb_game_id for m in markets if m.mlb_game_id}
    games_by_id = {g.id: g for g in session.query(MlbGame).filter(MlbGame.id.in_(game_ids)).all()} if game_ids else {}
    # REAL BUG caught live via the Recommended Bets page (2026-07-17): once a
    # Kalshi/Polymarket market for an already-PLAYED game stops being
    # actively refreshed (the poller only re-fetches currently-open live
    # events), its Market row sits in the DB forever with a frozen price --
    # and this endpoint was still computing a fresh model_prob for it off
    # CURRENT (post-game) Elo ratings, "predicting" a game whose outcome is
    # already known. That produced nonsensical 50-60pp "edges" that looked
    # like real opportunities on the Recommended Bets page. Confirmed this
    # is a pre-existing gap shared by NFL/NBA too (7,276 / 17,867 finished
    # games respectively), not MLB-specific -- they just haven't hit it yet
    # because every currently-relevant NFL/NBA game (preseason/Summer League)
    # is already excluded from getting a real model_prob for an unrelated
    # reason (see NO_BASELINE_REASONS). Fixed here by simply dropping any
    # market tied to a finished game before it reaches this endpoint's output
    # at all -- there's no live betting value in a market for a decided game.
    def _game_already_final(m: Market) -> bool:
        game = games_by_id.get(m.mlb_game_id) if m.mlb_game_id else None
        return game is not None and game.home_score is not None

    # SECOND, related real bug caught live the same way (2026-07-17): the
    # check above only excludes games with a recorded FINAL score -- but this
    # app has no live in-game score tracking at all (home_score/away_score
    # only get populated once the game is fully over), so a game that's
    # simply IN PROGRESS (e.g. real BAL@HOU game 824766, caught live at
    # 9-0 through 7 innings) sailed straight through. Its market prices
    # correctly reflected the real, live, already-decided score (implied
    # ~99.5% for "BOS Over 7.5" once BOS actually had 9 runs), but this
    # endpoint kept computing a static PREGAME model_prob (Elo + park +
    # weather, no knowledge the game had even started) as if nothing had
    # happened yet -- producing the exact same "huge fake edge" symptom as
    # the finished-game bug, just for the in-progress case. Fixed using the
    # SAME real kickoff-time resolution built for the weather/CLV signals
    # this session (TEAM_TZ covers all 30 teams, not just weather's 21
    # outdoor ones) -- a market is excluded once its game's real kickoff
    # instant is in the past, regardless of whether a final score has been
    # recorded yet. This is a strict superset of the home_score check above
    # (every finished game's kickoff is also in the past), so both live on
    # here for clarity/documentation rather than collapsing into one.
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _game_already_started(m: Market) -> bool:
        game = games_by_id.get(m.mlb_game_id) if m.mlb_game_id else None
        if game is None or not game.gameday or not game.gametime:
            return False
        tz_name = TEAM_TZ.get(game.home_team)
        if tz_name is None:
            return False
        kickoff = _game_kickoff_local(game.gameday, game.gametime, tz_name)
        return now_utc >= kickoff

    # THIRD gap (2026-07-19, found while chasing the same class of bug for
    # Tennis/MMA -- see ladder_sanity.py): the checks above depend on this
    # app's OWN schedule data being right. Confirmed live elsewhere this
    # session that a market-matching bug can attach the wrong game's real
    # kickoff time to a market (see project_mlb_edge_finder.md's Phase 11) --
    # if that ever happens again, `_game_already_started` would trust a
    # wrong instant. Detected structurally instead, as a second layer: a
    # real, still-live pregame ladder (spread/total/team_total) never prices
    # two DIFFERENT thresholds at the same extreme value (Over 7.5 must be
    # at least as likely as Over 8.5) -- seeing that happen is a direct tell
    # the real outcome is already locked in, independent of any timestamp.
    all_snapshots_pre = _batch_latest_snapshots(session, [m.id for m in markets])
    ladder_groups: dict[tuple, list[tuple[float, float]]] = {}
    for m in markets:
        if m.line is None or m.mlb_game_id is None or m.market_type not in ("spread", "total", "team_total"):
            continue
        implied = _implied_prob(all_snapshots_pre.get(m.id))
        if implied is None:
            continue
        ladder_groups.setdefault((m.mlb_game_id, m.market_type, m.team), []).append((m.line, implied))
    resolved_group_keys = find_resolved_entities(ladder_groups)
    games_with_resolved_ladder = {key[0] for key in resolved_group_keys}

    def _game_ladder_resolved(m: Market) -> bool:
        return m.mlb_game_id in games_with_resolved_ladder

    markets = [
        m for m in markets
        if not _game_already_final(m) and not _game_already_started(m) and not _game_ladder_resolved(m)
    ]
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    news_by_game = _batch_mlb_news_adjustments(session, game_ids)
    weekly_pool, futures_pool = get_mlb_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        game = games_by_id.get(m.mlb_game_id)
        implied = _implied_prob(snap)
        news = news_by_game.get(m.mlb_game_id)

        model_prob = final_prob = None
        no_baseline_reason = NO_MODEL_REASONS.get(m.market_type)
        if game is not None:
            if m.market_type == "moneyline":
                model_prob, final_prob = _moneyline_model_prob(m, game, news)
            elif m.market_type == "spread":
                model_prob = _spread_model_prob(m, game, news)
            elif m.market_type == "total":
                model_prob = _total_model_prob(m, game)
            elif m.market_type == "team_total":
                model_prob = _team_total_model_prob(m, game)
            elif m.market_type == "f5":
                model_prob = _f5_model_prob(m, game)
            elif m.market_type == "rfi":
                model_prob = _rfi_model_prob(m, game)

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "mlb", m.market_type)
        pool = weekly_pool if is_weekly_market_type(m.market_type) else futures_pool
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full)

        out.append(
            MlbMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                game_label=f"{game.away_team} @ {game.home_team}" if game else None,
                mlb_game_id=m.mlb_game_id,
                gameday=game.gameday if game else None,
                gametime=game.gametime if game else None,
                game_type=game.game_type if game else None,
                home_probable_pitcher=game.home_probable_pitcher if game else None,
                away_probable_pitcher=game.away_probable_pitcher if game else None,
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


def _moneyline_insight_mlb(
    home_team: str, away_team: str, home_r: float | None, away_r: float | None,
    home_sp: str | None, away_sp: str | None, factors_raw: list[dict],
    model_prob: float | None, market_prob: float | None,
) -> str:
    # Seed all phrasing off the matchup so a given game reads the same across
    # refreshes, but different games get different wording + a different LEAD.
    seed = f"{home_team}|{away_team}|{home_r}|{away_r}"

    # --- figure out the injury picture first: it decides some leads ---
    inj_lean_team = None
    inj_wash = False
    top_name = None
    if factors_raw:
        home_score = sum(_WEIGHT_SCORE.get(f.get("weight"), 0) for f in factors_raw if f.get("direction") == "favor_home")
        away_score = sum(_WEIGHT_SCORE.get(f.get("weight"), 0) for f in factors_raw if f.get("direction") == "favor_away")
        top = max(factors_raw, key=lambda f: _WEIGHT_SCORE.get(f.get("weight"), 0))
        top_name = top.get("factor") or "an unnamed factor"
        if home_score == away_score:
            inj_wash = True
        else:
            inj_lean_team = home_team if home_score > away_score else away_team

    # Narrative "movements" that connect into one throughline rather than a
    # list of facts: a setup (who's favored / how even), a development that
    # weaves pitching + injuries in with causal transitions, then the shared
    # edge sentence as the conclusion. Each movement has seeded variants.
    have_pitch = bool(home_sp and away_sp)

    def pitch_supports() -> str:
        # transition: pitching folded into an already-set favorite
        if not have_pitch:
            return ""
        return _seeded_choice(seed + "p", [
            f", and the pitching matchup doesn't do much to change that -- {away_sp} and {home_sp} roughly fold their season ERAs into where the two already sit",
            f", and with {away_sp} and {home_sp} on the mound, their ERAs mostly reinforce that same read",
            f" -- and today's arms, {away_sp} and {home_sp}, are already priced into it through their ERAs",
        ])

    def pitch_decides() -> str:
        # transition: pitching as the main separator in a coin flip
        if not have_pitch:
            return _seeded_choice(seed + "p", [
                " which leaves very little to actually separate them.",
                " so there's not much doing the separating.",
            ])
        return _seeded_choice(seed + "p", [
            f" which leaves the pitching to do most of the separating -- {away_sp} against {home_sp}, each priced in through their ERA.",
            f" so it's really {away_sp} versus {home_sp} on the mound doing the work, folded in via their ERAs.",
            f" and the arms end up carrying it -- {away_sp} and {home_sp}, each blended in through their ERA.",
        ])

    def injury_develops(as_tiebreak: bool) -> str:
        if inj_lean_team:
            if as_tiebreak:
                return _seeded_choice(seed + "i", [
                    f"The clearest edge to be had is health: {inj_lean_team} comes out ahead on the injury report, with {top_name} the name to know on the other side.",
                    f"What little separation there is comes from the injury sheet, and it favors {inj_lean_team} -- {top_name} the notable absence.",
                    f"If anything tips it, it's health -- {inj_lean_team} has the better of the injury news, {top_name} the headline.",
                ])
            return _seeded_choice(seed + "i", [
                f"On top of that, the injury report leans {inj_lean_team}'s way a touch, {top_name} the name to know.",
                f"The injury sheet nudges it a little further {inj_lean_team}'s way, led by {top_name}.",
                f"Health tilts fractionally toward {inj_lean_team} too, most notably {top_name}.",
            ])
        if inj_wash:
            return _seeded_choice(seed + "i", [
                f"The injury news barely moves the needle -- {top_name} is the one to note, but it cuts about even.",
                f"Health is close to a wash here; {top_name} aside, neither side really gains from it.",
            ])
        return _seeded_choice(seed + "i", [
            "There's nothing on the injury sheet to add, and the starters are already in the ratings, so that's the whole picture.",
            "No position-player injuries are on file either, and with the arms already priced in, that's about all there is to weigh.",
        ])

    story = ""
    if home_r is not None and away_r is not None:
        gap = home_r - away_r
        stronger, s_r, weaker, w_r = (home_team, home_r, away_team, away_r) if gap >= 0 else (away_team, away_r, home_team, home_r)
        if abs(gap) < 25 and inj_lean_team:
            # coin flip decided by health -> setup the closeness, then develop the injury
            setup = _seeded_choice(seed, [
                f"There's almost nothing separating these two on paper -- {home_team} and {away_team} sit within {abs(gap):.0f} Elo points of each other ({home_r:.0f} to {away_r:.0f}), so it comes down to the margins.",
                f"This one projects about even, with {home_team} and {away_team} inside {abs(gap):.0f} Elo points ({home_r:.0f} to {away_r:.0f}) -- close enough that the small stuff decides it.",
                f"Ratings put {home_team} and {away_team} nearly level ({home_r:.0f} to {away_r:.0f}, {abs(gap):.0f} apart), which throws the read onto everything around the edges.",
            ])
            dev = injury_develops(as_tiebreak=True)
            tail = _seeded_choice(seed + "pt", [
                f" The arms, {away_sp} and {home_sp}, are already baked into that near-even read." if have_pitch else "",
                f" {away_sp} and {home_sp} on the mound don't tilt it much -- both are priced into the ratings already." if have_pitch else "",
            ])
            story = f"{setup} {dev}{tail}"
        elif abs(gap) < 25:
            # true toss-up -> pitching carries it
            setup = _seeded_choice(seed, [
                f"About as even as it gets: {home_team} and {away_team} are within {abs(gap):.0f} Elo points ({home_r:.0f} to {away_r:.0f}),",
                f"This is a genuine pick 'em -- {home_team} and {away_team} sit just {abs(gap):.0f} apart on Elo ({home_r:.0f} to {away_r:.0f}),",
                f"There's barely daylight between these two ({home_r:.0f} to {away_r:.0f}, {abs(gap):.0f} apart),",
            ])
            story = f"{setup}{pitch_decides()} {injury_develops(as_tiebreak=False)}"
        elif abs(gap) >= 45:
            # clear favorite -> setup dominance, pitching supports, injury adds
            setup = _seeded_choice(seed, [
                f"This one's {stronger}'s to lose on paper -- the ratings have them a clear {abs(gap):.0f} points above {weaker} ({s_r:.0f} to {w_r:.0f})",
                f"{stronger} is the class of the matchup here, sitting a full {abs(gap):.0f} Elo points over {weaker} ({s_r:.0f} to {w_r:.0f})",
                f"The ratings make {stronger} the decided side, {abs(gap):.0f} points clear of {weaker} ({s_r:.0f} to {w_r:.0f})",
            ])
            story = f"{setup}{pitch_supports()}. {injury_develops(as_tiebreak=False)}"
        else:
            # moderate edge
            setup = _seeded_choice(seed, [
                f"{stronger} comes in as the side the ratings prefer, about {abs(gap):.0f} Elo points up on {weaker} ({s_r:.0f} to {w_r:.0f})",
                f"The lean here is {stronger}, who rate {abs(gap):.0f} points ahead of {weaker} ({s_r:.0f} to {w_r:.0f})",
                f"Elo gives the edge to {stronger}, {abs(gap):.0f} points to the good over {weaker} ({s_r:.0f} to {w_r:.0f})",
            ])
            story = f"{setup}{pitch_supports()}. {injury_develops(as_tiebreak=False)}"
    elif have_pitch:
        story = _seeded_choice(seed, [
            f"With no team ratings to lean on, the read rides on the starters -- {away_sp} against {home_sp} -- each blended in through their season ERA.",
            f"Without ratings to anchor it, this comes down to the mound: {away_sp} versus {home_sp}, folded in via their ERAs.",
        ]) + " " + injury_develops(as_tiebreak=False)

    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _margin_insight_mlb(home_team: str, away_team: str, home_r: float | None, away_r: float | None, model_prob: float | None, market_prob: float | None) -> str:
    noise = _seeded_choice(f"{home_r}|{away_r}|noise", [
        "That said, run margins are far noisier in baseball than in football or basketball -- bullpen swings and balls-in-play luck muddy the exact spread -- so the lean over a coin flip is real but modest.",
        "Worth keeping in mind that baseball run margins wobble a lot (bullpens, batted-ball luck), so any edge on the spread is genuine but small by nature.",
    ])
    if home_r is not None and away_r is not None:
        gap = home_r - away_r
        stronger, s_r, weaker, w_r = (home_team, home_r, away_team, away_r) if gap >= 0 else (away_team, away_r, home_team, home_r)
        base = _seeded_choice(f"{home_team}|{away_team}|{home_r}|{away_r}|rl", [
            f"The fair value here rides mostly on the {abs(gap):.0f}-point Elo edge {stronger} holds over {weaker}, starters included.",
            f"Most of this run line traces to the {abs(gap):.0f}-point rating gap {stronger} carries on {weaker} (starters folded in).",
            f"What sets the number is the {abs(gap):.0f}-Elo cushion {stronger} has over {weaker}, pitching included.",
        ])
        story = f"{base} {noise}"
    else:
        story = noise
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _f5_insight_mlb(home_team: str, away_team: str, model_prob: float | None, market_prob: float | None) -> str:
    seed = f"{home_team}|{away_team}|f5"
    story = _seeded_choice(seed, [
        "This is the run-line's margin model, but refit from scratch on real first-five-innings scoring rather than the full-game model rescaled -- so it actually reflects how the opening frames play. The tie is handled separately, off a real ~15.4% empirical tie rate, because low, discrete early scoring ends level more often than a smooth approximation would guess, and that holds no matter how lopsided the matchup is.",
        "It runs on the same shape as the run line, only fit directly to first-five scoring data instead of shrinking the full-game numbers down. The draw gets its own treatment -- a measured ~15.4% tie rate -- since baseball's sparse early scoring pushes toward ties more than the continuous model expects, regardless of the matchup.",
        "Under the hood this is the run-line margin model re-derived just for the first five innings on real per-inning scores, not the whole-game version scaled back. Ties come from an actual ~15.4% empirical rate rather than the model's own math, because early innings finish even more often than a smooth curve predicts.",
    ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _futures_sim_insight_mlb(team: str, rating: float | None, team_sim: dict, model_prob: float | None, market_prob: float | None) -> str:
    if rating is not None:
        playoff_pct = team_sim.get("playoff_pct")
        champ_pct = team_sim.get("championship_pct")
        seed = f"{team}|{rating}|futsim"
        if playoff_pct is not None and champ_pct is not None:
            story = _seeded_choice(seed, [
                f"This comes out of a full season simulation anchored on {team}'s current Elo ({rating:.0f}) -- run it forward and they land around a {playoff_pct * 100:.0f}% chance at the playoffs and {champ_pct * 100:.0f}% at the World Series overall.",
                f"{team}'s rating ({rating:.0f}) is fed through a rest-of-season sim to get here, which also shakes out to roughly {playoff_pct * 100:.0f}% to reach the playoffs and {champ_pct * 100:.0f}% to win it all.",
                f"Behind this number is a season-long simulation off {team}'s Elo ({rating:.0f}); the same run gives them about a {playoff_pct * 100:.0f}% playoff shot and {champ_pct * 100:.0f}% at the title.",
            ])
        else:
            story = _seeded_choice(seed, [
                f"This is a season simulation built off {team}'s current Elo ({rating:.0f}), carried forward across the remaining schedule.",
                f"{team}'s rating ({rating:.0f}) drives this through a rest-of-season simulation.",
            ])
    else:
        story = ""
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _rfi_insight_mlb(combined_era: float | None, model_prob: float | None, market_prob: float | None) -> str:
    if combined_era is not None:
        story = _seeded_choice(f"{combined_era:.2f}|rfi", [
            f"Whether a run crosses in the 1st inning leans on the two starters' combined current-season ERA ({combined_era:.2f}) -- weaker arms give up early runs a bit more often. It's a real, checked signal, but an honestly weak one (far softer than the moneyline pitcher blend or the total's park factor), so it only shifts things a little off the league-average rate.",
            f"The read here is mostly the starters' combined ERA ({combined_era:.2f}): shakier pitching tends to leak first-inning runs. That's a genuine effect but a small one -- much lighter than the pitcher blend or park factor elsewhere -- so expect only a modest move off the base rate.",
        ])
    else:
        story = _seeded_choice("rfi_flat", [
            "One or both starters don't have a qualifying current-season ERA yet, so rather than guess at a starter-specific tweak this just uses the flat league-average first-inning rate (~49.4%).",
            "Without a qualifying ERA for both starters, this falls back to the league-average first-inning rate (~49.4%) instead of inventing a pitcher adjustment the data can't support yet.",
        ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _total_insight_mlb(home_team: str, weather: dict | None, model_prob: float | None, market_prob: float | None) -> str:
    # The totals model is deliberately park + weather (team form was checked
    # and doesn't help MLB totals), so lead with the park -- it's the backbone --
    # then let the weather adjustments read as "and on top of that".
    park_factor = game_lines_mlb.PARK_FACTOR.get(home_team, 0.0)
    pseed = f"{home_team}|{park_factor}"
    if abs(park_factor) >= 0.3:
        direction = "pushes it up" if park_factor > 0 else "drags it down"
        sentences = [_seeded_choice(pseed, [
            f"The number here is really a ballpark story: {home_team}'s home park {direction} by {abs(park_factor):.2f} runs versus a league-average yard -- a real, derived effect, not team form (that was checked and doesn't move MLB totals).",
            f"This starts with the park: {home_team}'s yard {direction} by {abs(park_factor):.2f} runs against league average, which is the backbone of the total (team form was checked and doesn't help here).",
            f"Where this total really comes from is the ballpark -- {home_team}'s home run environment {direction} by {abs(park_factor):.2f} runs versus average, a derived effect rather than anything about team form.",
        ])]
    else:
        sentences = [_seeded_choice(pseed, [
            f"{home_team}'s home park is a near-neutral run environment ({park_factor:+.2f} runs vs. league average), so there's little ballpark thumb on the scale to start with.",
            f"There's not much ballpark effect to lean on here -- {home_team}'s yard plays close to league average ({park_factor:+.2f} runs) -- so the total rests on the conditions.",
        ])]
    temp_f = (weather or {}).get("temp_f")
    out_wind_mph = (weather or {}).get("out_wind_mph")
    if temp_f is not None:
        shift = game_lines_mlb.TEMP_SLOPE * (temp_f - game_lines_mlb.LEAGUE_AVG_TEMP_F)
        direction = "nudges the total up" if shift > 0 else "trims the total"
        sentences.append(f"Today's forecast ({temp_f:.0f}°F) then {direction} by {abs(shift):.2f} runs against a typical {game_lines_mlb.LEAGUE_AVG_TEMP_F:.0f}°F night -- warmer air simply carries fly balls further, another checked signal.")
    elif home_team in BALLPARKS:
        sentences.append("There's no forecast temperature in range yet; once one lands, the same checked warm-weather bump would fold in.")
    if out_wind_mph is not None:
        wind_shift = game_lines_mlb.OUT_WIND_SLOPE * out_wind_mph
        if abs(wind_shift) >= 0.1:
            direction = "adds" if wind_shift > 0 else "shaves"
            blow_direction = "blowing out toward center" if out_wind_mph > 0 else "blowing in from center"
            sentences.append(f"And with the wind {blow_direction} for this park's orientation, that {direction} another {abs(wind_shift):.2f} runs -- a signal we sanity-checked against Wrigley's famous wind before trusting it league-wide.")
    sentences.append(_edge_sentence(model_prob, market_prob))
    return " ".join(sentences)


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_mlb_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    """MLB equivalent of nba_markets.py::get_nba_market_reasoning -- same
    "explain how the number passed in was derived, don't recompute it"
    contract."""
    m = session.get(Market, market_id)
    if m is None or m.sport != "mlb":
        raise HTTPException(404, "market not found")
    game = session.get(MlbGame, m.mlb_game_id) if m.mlb_game_id else None
    label = f"{game.away_team} @ {game.home_team}" if game else (m.group_label or m.market_type)
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    caveats = ["model_validated: false -- no model in this app has been shown to beat the market in backtesting (MLB has no free historical odds source to even run that check against -- see Backtests)."]
    methodology = "No detailed methodology available for this market type yet."
    insight = ""

    if m.market_type == "moneyline" and game is not None:
        home_r = elo_service_mlb.get_team_rating(game.home_team)
        away_r = elo_service_mlb.get_team_rating(game.away_team)
        methodology = (
            "Team-Elo rating blended with a starting-pitcher signal (current-season ERA, converted to "
            "Elo-equivalent points via a pooled regression against 10 years of real outcomes), further "
            "blended with a free rule-based position-player-injury adjustment when one is on file. "
            "Starting-pitcher availability is priced in via the probable-pitcher matchup itself, not a "
            "separate injury factor. Calibration-checked against real outcomes (no free historical MLB "
            "odds source exists to run a market-beating go/no-go the way NFL's moneyline backtest does "
            "-- see Backtests)."
        )
        if home_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} Elo rating", detail=f"{home_r:.0f}"))
        if away_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} Elo rating", detail=f"{away_r:.0f}"))
        if game.home_probable_pitcher:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} probable starter", detail=game.home_probable_pitcher))
        if game.away_probable_pitcher:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} probable starter", detail=game.away_probable_pitcher))
        news_cache = get_mlb_news_adjustment_cache(session, game.id)
        factors_raw: list[dict] = []
        if news_cache:
            factors_raw = json.loads(news_cache.factors_json)
            for f in factors_raw:
                name = f.get("factor", "Situational factor")
                direction = f.get("direction", "neutral").replace("_", " ")
                weight = f.get("weight", "")
                detail = f"{weight} weight, {direction}" if weight else direction
                factors.append(ReasoningFactorOut(label=name, detail=detail))
            factors.append(ReasoningFactorOut(label="Combined injury adjustment", detail=f"{news_cache.adjustment_pct:+.1f}pp ({news_cache.confidence} confidence)"))
        else:
            factors.append(ReasoningFactorOut(label="Position-player injuries", detail="none on file for this game yet"))
        insight = _moneyline_insight_mlb(
            game.home_team, game.away_team, home_r, away_r,
            game.home_probable_pitcher, game.away_probable_pitcher, factors_raw, model_prob, market_prob,
        )

    elif m.market_type == "spread" and game is not None:
        methodology = (
            "Margin-space probability model: Normal-distribution approximation, mean derived from a "
            "real linear regression of actual run margin against the same Elo+pitcher-blend rating "
            "difference the moneyline model uses, further blended with the same free rule-based "
            "position-player-injury adjustment moneyline uses (folded in via win-probability space, then "
            "converted back to an effective rating difference -- see game_lines_mlb.py)."
        )
        home_r = elo_service_mlb.get_team_rating(game.home_team)
        away_r = elo_service_mlb.get_team_rating(game.away_team)
        if home_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} Elo rating", detail=f"{home_r:.0f}"))
        if away_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} Elo rating", detail=f"{away_r:.0f}"))
        spread_news_cache = get_mlb_news_adjustment_cache(session, game.id)
        if spread_news_cache:
            factors.append(ReasoningFactorOut(label="Combined injury adjustment", detail=f"{spread_news_cache.adjustment_pct:+.1f}pp ({spread_news_cache.confidence} confidence)"))
        insight = _margin_insight_mlb(game.home_team, game.away_team, home_r, away_r, model_prob, market_prob)

    elif m.market_type in ("total", "team_total") and game is not None:
        methodology = (
            "League-average runs (Normal-distribution approximation) plus a real, derived per-park "
            "adjustment and, for outdoor parks with a forecast in range, real, checked temperature and "
            "orientation-relative wind adjustments -- team-scoring-rate and starting-pitcher-ERA signals "
            "were checked and found NOT to improve on the league-average baseline for MLB totals (unlike "
            "NFL/NBA); raw wind SPEED (no direction) was also checked and found not to help on its own "
            "(see game_lines_mlb.py)."
        )
        park_factor = game_lines_mlb.PARK_FACTOR.get(game.home_team, 0.0)
        factors.append(ReasoningFactorOut(label=f"{game.home_team} home park factor", detail=f"{park_factor:+.2f} runs vs. league average"))
        weather = _game_weather(game)
        temp_f = (weather or {}).get("temp_f")
        out_wind_mph = (weather or {}).get("out_wind_mph")
        if temp_f is not None:
            factors.append(ReasoningFactorOut(label="Forecast temperature", detail=f"{temp_f:.0f}°F"))
        if out_wind_mph is not None:
            factors.append(ReasoningFactorOut(label="Wind (relative to park orientation)", detail=f"{abs(out_wind_mph):.1f} mph {'out' if out_wind_mph > 0 else 'in'}"))
        if m.market_type == "team_total" and m.team:
            factors.append(ReasoningFactorOut(label="Team", detail=m.team))
        insight = _total_insight_mlb(game.home_team, weather, model_prob, market_prob)

    elif m.market_type == "f5" and game is not None:
        methodology = (
            "3-way (home/away/tie) margin model for just the first 5 innings -- Normal-distribution "
            "approximation for the home/away direction, mean/std derived from real first-5-innings score "
            "margins (not the full-game margin model naively rescaled). The tie probability is a real "
            "empirical rate (~15.4%), not derived from the Normal model -- a continuity-corrected band "
            "badly undershot the real tie rate in testing."
        )
        home_r = elo_service_mlb.get_team_rating(game.home_team)
        away_r = elo_service_mlb.get_team_rating(game.away_team)
        if home_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} Elo rating", detail=f"{home_r:.0f}"))
        if away_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} Elo rating", detail=f"{away_r:.0f}"))
        insight = _f5_insight_mlb(game.home_team, game.away_team, model_prob, market_prob)

    elif m.market_type == "rfi" and game is not None:
        methodology = (
            "Binary probability from both starters' combined current-season ERA -- a real but genuinely "
            "weak signal (checked against real data, sign-consistent, but an order of magnitude weaker "
            "than the moneyline pitcher blend or the total's park factor). Falls back to the flat "
            "league-average RFI rate when current-season ERA isn't available for both starters."
        )
        combined_era = elo_service_mlb.get_combined_era(
            game.season, game.home_probable_pitcher_id, game.away_probable_pitcher_id,
        )
        if combined_era is not None:
            factors.append(ReasoningFactorOut(label="Combined starter ERA", detail=f"{combined_era:.2f}"))
        else:
            factors.append(ReasoningFactorOut(label="Combined starter ERA", detail="unavailable -- using flat league rate"))
        insight = _rfi_insight_mlb(combined_era, model_prob, market_prob)

    elif game is None and m.team and (m.market_type in FUTURES_SIM_KEY or m.market_type == "win_total"):
        methodology = (
            "Season Monte Carlo simulation (2,000 trials) using current Elo ratings and the real "
            "remaining schedule, TEAM-Elo only (no starting-pitcher blend -- future starters aren't "
            "knowable months out), with the real 2022+ postseason format (6 teams/league, wild card "
            "best-of-3, division/championship series fixed bracket, no reseeding -- verified live)."
        )
        rating = elo_service_mlb.get_team_rating(m.team)
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} current Elo rating", detail=f"{rating:.0f}"))
        sim_results = get_mlb_season_sim_results()
        team_sim = sim_results.get(m.team) or {}
        for sim_key, sim_label in [
            ("division_pct", "Division win probability"),
            ("pennant_pct", "Pennant (league championship) probability"),
            ("playoff_pct", "Playoff probability"),
            ("championship_pct", "World Series championship probability"),
            ("best_record_pct", "Best regular-season record probability"),
        ]:
            if sim_key in team_sim and sim_key != FUTURES_SIM_KEY.get(m.market_type):
                factors.append(ReasoningFactorOut(label=sim_label, detail=f"{team_sim[sim_key] * 100:.1f}%"))
        caveats.append("Playoff seeding uses simplified tiebreakers (win total, then a coin flip), not MLB's real multi-step tiebreaker rules.")
        insight = _futures_sim_insight_mlb(m.team, rating, team_sim, model_prob, market_prob)

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
