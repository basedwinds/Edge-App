import datetime
import hashlib
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.clients import depth_chart_client, espn_client
from app.data.stadiums import NFL_TEAM_TZ
from app.models.qb_ratings import _canonical_key as qb_canonical_key
from app.db.database import get_session
from app.db.models import Market, MarketSnapshot, NflGame
from app.ingestion.market_catalog import (
    get_news_adjustment_cache,
    get_previous_coach,
    latest_snapshot,
    news_cache_to_pydantic,
    upsert_news_adjustment,
)
from app.models.baseline import elo_service
from app.models.baseline.elo import effective_home_field_adv, implied_elo_diff, win_prob
from app.models.combine import combine_probability
from app.models.ladder_sanity import find_resolved_entities
from app.models.news_adjustment import weather_rules
from app.models.news_adjustment.schema import NewsAdjustment
from app.models.news_adjustment.situational import compute_situational_adjustment
from app.models import epa_ratings, game_lines, scoring_ratings_service, season_sim_service
from app.models.awards import (
    build_all_starters_full_name_to_team,
    build_coach_name_to_team,
    build_offensive_skill_full_name_to_team,
    build_qb_rb_full_name_to_team,
    compute_coty_scores,
    compute_dpoy_scores,
    compute_mvp_scores,
    compute_opoy_scores,
    _full_name_key as coach_name_key,
)
from app.models.defensive_ratings import get_defensive_career_scores
from app.models.qb_ratings import get_qb_career_stats
from app.models.skill_position_ratings import get_receiving_career_stats, get_rushing_career_stats
from app.models.division_markets import (
    division_code_to_key,
    division_extreme_model_probs,
    division_order_model_prob,
    division_wins_model_prob,
    h2h_model_prob,
    worst_to_first_model_prob,
)
from app.ingestion.market_matcher import split_teams_blob, KALSHI_TEAM_ABBRS, to_nflverse_abbr
from app.models.stat_leaders import get_stat_leader_totals, compute_leader_scores
from app.models.season_projections import get_prior_season_stats, prob_exceeds_season_total, project_season_total
from app.models.staking import FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars, is_weekly_market_type
from app.models.clv_selection import bucket_clv_stats, gate_kelly
from app.api.routers.settings import get_pool_dollars, get_unit_dollars, get_staking_params, get_flat_params
from app.data.divisions import DIVISIONS
from app.api.schemas import FuturesMarketOut, MarketOut, ReasoningFactorOut, ReasoningOut

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/readiness")
def get_readiness(session: Session = Depends(get_session)):
    """Whether each SEASON sport's season is active/near, so the frontend can
    hide (or badge) 'not ready' futures + far-future games in the Recommended
    and Futures views -- the same rule the Discord alerts already use
    (paper_logger). Season sports gate their futures on `season_active`;
    game-tied rows are gated by the frontend against `game_window_days` using
    the gameday it already has. Event-based sports (tennis/mma/esports/racing)
    aren't season-gated (their futures are tournament-scoped + near-term)."""
    from app.models.paper_logger import (
        _ALERT_MAX_DAYS_TO_EVENT,
        _READINESS_WINDOW_DAYS,
        _SEASON_TABLES,
        _sport_season_active,
    )

    return {
        "game_window_days": _ALERT_MAX_DAYS_TO_EVENT,
        "futures_window_days": _READINESS_WINDOW_DAYS,
        "season_sports": list(_SEASON_TABLES.keys()),
        "season_active": {sp: _sport_season_active(session, sp) for sp in _SEASON_TABLES},
    }


class DivergenceOut(BaseModel):
    model_config = {"protected_namespaces": ()}
    sport: str
    entity_id: str
    market_type: str
    team: str | None
    line: float | None
    side: str | None
    kalshi_prob: float
    polymarket_prob: float
    gap: float
    buy_side: str
    kalshi_market_id: int
    polymarket_market_id: int
    kalshi_volume: float | None
    polymarket_volume: float | None


@router.get("/cross-platform-divergences", response_model=list[DivergenceOut])
def list_cross_platform_divergences(min_gap: float = 0.03, session: Session = Depends(get_session)):
    """Same real-world proposition priced differently on Kalshi vs Polymarket
    -- model-independent candidate edges (see cross_platform_divergence.py).
    Cross-sport; game-tied markets only, both sides required to have traded."""
    from app.models.cross_platform_divergence import find_divergences
    return find_divergences(session, min_gap=min_gap)


NO_PRESEASON_BASELINE_REASON = (
    "No baseline for preseason -- Elo reflects regular-season team strength, and preseason "
    "lineups are a coaching decision (who plays how many snaps), not a fair test of that."
)

# Season-long markets, not tied to a single game -- see season_sim.py.
# Excluded from list_markets (the per-game dashboard) and served instead by
# list_futures below. Maps each market_type to the matching season-sim
# result key.
FUTURES_SIM_KEY = {
    "division_winner": "division_pct",
    "conference_champion": "conf_champ_pct",
    "one_seed": "one_seed_pct",
    "super_bowl_champion": "sb_champ_pct",
    "playoff_qualifier": "playoff_pct",
    "best_record": "best_record_pct",
}

# Season-long but NOT team-keyed (Polymarket's single "will any team finish
# undefeated" market, see polymarket_client.py::get_undefeated_market) --
# priced from season_sim.py's special "_LEAGUE" entry, not a per-team
# lookup, so it can't share FUTURES_SIM_KEY's simple team->pct mapping.
# wins_any (Kalshi's team-less "will ANY team hit N wins" ladder) joins it
# here for the same reason, 2026-07-16.
LEAGUE_FUTURES_TYPES = {"undefeated_season", "wins_any"}

# Week-1 starting-QB categorical markets, added 2026-07-16 -- team-keyed like
# the simple futures dict above, but model_prob isn't a season_sim lookup,
# it's a direct depth-chart comparison (see _week1_qb_model_prob below), so
# it needs its own set rather than joining FUTURES_SIM_KEY.
WEEK1_QB_FUTURES_TYPE = "week1_qb"

WEEK1_QB_STARTER_PROB = 0.85  # depth-chart-listed QB1
WEEK1_QB_BACKUP_PROB = 0.08  # depth-chart-listed QB2
WEEK1_QB_OTHER_PROB = 0.01  # named candidate, not currently QB1 or QB2 on the depth chart


def _week1_qb_model_prob(team: str, candidate_name: str, starters_by_team: dict, qb_backup_by_team: dict) -> float | None:
    """Depth-chart-based estimate for a Week-1-starting-QB market candidate
    -- not a projection in the Elo/season-sim sense, just "does this name
    match who the depth chart currently lists as QB1/QB2 for this team."
    Free, structured, no LLM/news synthesis -- matches this project's
    standing data-source constraint. Real uncertainty is compressed into
    three flat buckets rather than a fitted probability, since there's no
    backtest-able historical relationship between depth-chart rank and
    "who actually starts Week 1" to derive one from."""
    qb1 = starters_by_team.get(team, {}).get("QB")
    qb2 = qb_backup_by_team.get(team)
    key = qb_canonical_key(candidate_name)
    if qb1 and qb_canonical_key(qb1) == key:
        return WEEK1_QB_STARTER_PROB
    if qb2 and qb_canonical_key(qb2) == key:
        return WEEK1_QB_BACKUP_PROB
    return WEEK1_QB_OTHER_PROB

# Win-total ladder markets (per-team over/under + exact win count), added
# 2026-07-16 alongside LEAGUE_FUTURES_TYPES's wins_any -- these need
# season_sim's win_count_pct HISTOGRAM (a list indexed by win count), not a
# single float, so they're handled separately in list_futures rather than
# through FUTURES_SIM_KEY's simple team->pct-field mapping.
WIN_LADDER_FUTURES_TYPES = {"win_total", "exact_win_total"}

# MVP / Coach of the Year / DPOY / OPOY, added 2026-07-16 -- see
# app/models/awards.py for the scoring methodology. Team is resolved at
# INGESTION time (poller.py), but the actual probability distribution
# (compute_mvp_scores/compute_coty_scores/compute_dpoy_scores/
# compute_opoy_scores) is computed here per-request since it needs the full
# set of currently-tracked candidates to normalize across, not just one row.
AWARD_FUTURES_TYPES = {"mvp", "coach_of_year", "dpoy", "opoy"}

# Division wins/order/extremes + worst-to-first + head-to-head win totals,
# added 2026-07-16 -- see app/models/division_markets.py. All derived from
# season_sim's existing output (win_count_pct + the "_DIVISIONS" order/
# total-wins tallies), no new simulation needed.
DIVISION_EXTRA_TYPES = {"division_wins", "division_order", "div_least_wins", "div_most_wins", "worst_to_first", "h2h_wins"}

# League-leader categorical markets (9 raw-counting-stat categories, see
# app/models/stat_leaders.py) + team points-scored/allowed most/least,
# added 2026-07-16.
LEADER_FUTURES_TYPES = {
    "leader_pass_yds", "leader_pass_tds", "leader_pass_int", "leader_rush_yds",
    "leader_rush_tds", "leader_rec_yds", "leader_rec_tds", "leader_def_int", "leader_sacks",
}
TEAM_POINTS_FUTURES_TYPES = {"team_pts_most", "team_pts_least", "team_dpts_most", "team_dpts_least"}

# Season-total threshold ladders (see app/models/season_projections.py for
# the mean/std probability model, derived from real year-over-year
# predictability data, added 2026-07-16). market_type is f"season_{category}".
SEASON_STAT_FUTURES_TYPES = {
    "season_pass_yds", "season_rush_yds", "season_rush_tds",
    "season_rec_yds", "season_rec_tds", "season_rec",
}
SEASON_STAT_CATEGORY_BY_TYPE = {t: t[len("season_"):] for t in SEASON_STAT_FUTURES_TYPES}
SEASON_STAT_QB_CATEGORIES = {"pass_yds"}
SEASON_STAT_RB_CATEGORIES = {"rush_yds", "rush_tds"}

# Player season-stat futures (leader + season over/under) are priced for
# VISIBILITY but NOT staked, as of 2026-07-23. Two honest reasons: (1) the
# projection input is naive (last-season rate only, no age curve / target-share
# / scheme / QB-change), so it can't beat what is the single sharpest,
# most-researched market class in sports (the fantasy/DFS industry prices these
# exact numbers); and (2) the live model was systematically OVER-projecting and
# flat-staking ~$3.8k across ~200 of these on implausible +50-60pp "edges"
# (e.g. a QB at 75% to clear 4,500 pass yds vs a 15% market) -- classic
# staking-your-own-biggest-errors. Same tracking-only posture as the esports
# tournament sim. Flip back on only if a real edge vs closing prices is ever
# demonstrated.
PLAYER_STAT_TRACKING_ONLY = LEADER_FUTURES_TYPES | SEASON_STAT_FUTURES_TYPES
PLAYER_STAT_TRACKING_NOTE = (
    "Player stat projection shown for tracking only, not staked: the input is a "
    "naive last-season-rate projection against a very sharp market, and it has "
    "not been validated to beat closing prices."
)


def _team_points_scores(ratings: dict, mode: str) -> dict[str, float]:
    """Ranks teams by their CURRENT trailing points-scored/allowed rate
    (scoring_ratings.py) and converts the ranking into a rough probability
    distribution via linear distance-from-the-extreme, normalized to sum to
    1. Deliberately NOT a real statistical projection (would need a full
    17-game points Monte Carlo, a bigger lift not attempted this round) --
    an honestly-rough ranking heuristic, same tier as several other
    approximations in this app (e.g. week1_qb's 3-bucket model)."""
    field = "points_scored" if mode.startswith("pts") else "points_allowed"
    projections = {t: r[field] for t, r in ratings.items() if r.get(field) is not None}
    if not projections:
        return {}
    if mode.endswith("least"):
        extreme = max(projections.values())
        raw = {t: (extreme - v + 0.01) for t, v in projections.items()}
    else:
        extreme = min(projections.values())
        raw = {t: (v - extreme + 0.01) for t, v in projections.items()}
    total = sum(raw.values())
    return {t: v / total for t, v in raw.items()} if total > 0 else {}


def _implied_prob(snap):
    """The market's price for this side: the bid/ask MIDPOINT, or last_price when
    there is no book.

    I switched this to the ASK on 2026-08-04 and reverted it the same day. The
    reasoning for the ask is real -- you cross the spread on a market order, and
    at longshot prices a 1.5c difference is worth 13pp of ROI. But the evidence
    did not support applying it here:

      * It was checked against markets that HAVE a two-sided quote, which is 7%
        of tennis bets (149 of 2,166) and 3 of 48 in the hand-picked book. Those
        are the liquid ones -- systematically unlike the ITF/Challenger markets
        where most bets actually live. Generalising from them was wrong.
      * The supporting "both sides sum to 0.825" finding was contaminated: 99 of
        117 pairs were the SAME player quoted on two venues, not opposite sides.
      * On Kalshi you set a limit price. The user verifies the quote before
        placing and gets it, so the midpoint is achievable in this workflow --
        the ask is the worst case, not the expected case.

    The honest treatment is to show the spread alongside the price and let the
    reader judge the fill, not to silently pick a bound for them.
    """
    NO_BOOK_SPREAD = 0.90

    if snap is None:
        return None
    if snap.yes_bid is not None and snap.yes_ask is not None:
        # A >=90c spread is not a wide market, it is the ABSENCE of one: two
        # token orders parked at the extremes. Averaging them yields ~0.50
        # regardless of what the contract is worth, which manufactures a huge
        # edge against any confident model. Found on PHI 80+ wins -- bid 0.01,
        # ask 0.99, last trade 0.90, printed as "market says 50%" for a +46.9pp
        # edge that was the largest on the MLB board. Real edge ~7pp.
        #
        # The threshold is measured, not chosen for neatness. Median |mid -
        # last_price| by spread, over 19,190 active two-sided markets:
        #
        #   spread <5c    0.5pp     30-50c   19.5pp
        #   5-15c         2.0pp     50-70c   35.0pp
        #   15-30c        9.0pp     >=90c     0.0pp (bimodal: 37% off by >20pp)
        #
        # Note what this does NOT do: the 30-70c band has the worst midpoints,
        # but last_price sits inside [bid, ask] only 43.6% of the time there, so
        # neither number is trustworthy and there is no evidence-backed choice
        # between them. Overriding that band would repeat the mid-vs-ask mistake
        # -- generalising a rule from markets it wasn't measured on. Left alone.
        #
        # At >=90c the median difference is 0.0pp, so this is a no-op wherever
        # the two already agree and only bites on the ~37% tail where the book
        # is pure noise and an actual trade is the better evidence.
        # The 1e-9 is not decoration: prices are cents-as-floats, and a literal
        # 5c/95c book gives 0.95 - 0.05 = 0.8999999999999999, which silently
        # fails a bare >= 0.90 and leaves the exact case this guards for broken.
        if snap.yes_ask - snap.yes_bid >= NO_BOOK_SPREAD - 1e-9 and snap.last_price is not None:
            return snap.last_price
        return round((snap.yes_bid + snap.yes_ask) / 2, 4)

    # NO BOOK AT ALL. The >=90c guard above only fires when both sides exist;
    # this is the other shape of the same pathology, and it was the bigger one.
    #
    # Polymarket SEEDS last_price on a market that has never traded (already
    # documented in staking.has_real_trading, which is why volume==0 alone is
    # treated as untraded there). When that seed is exactly 0.500 it is not a
    # price, it is the absence of one -- and a confident model scored against
    # it manufactures a ~30pp edge out of nothing.
    #
    # Measured 2026-08-06 over every settled bet, flat-unit ROI split by
    # whether the bet was booked at exactly 0.500. Phantom-priced bets beat
    # real ones in EVERY sport, which is the signature of a measurement
    # artifact rather than an edge (a "win" at a fake 0.500 books +1.0 unit
    # when the true price might have been 0.80, worth +0.25):
    #
    #   sport    phantom ROI (n)     clean ROI (n)
    #   tennis   +28.1% (704)        +14.5% (2411)
    #   mlb      +29.8% (131)         +1.8% (2817)
    #   f1      +100.0% (18)          +9.5% (503)
    #   wnba    +100.0% (8)           +4.4% (407)
    #   cs2      +33.3% (6)          +10.9% (131)
    #
    # Returning None (no price, so no edge and no stake) rather than a
    # degenerate 50/50 dressed up as a real number -- the same treatment
    # elo_service_*.get_series_distribution already gives an unrated matchup.
    #
    # Deliberately narrow, in the spirit of the 30-70c band left alone above:
    # requires no book AND exactly 0.500 AND no volume. A market that really
    # traded at 50c keeps its price (152 of 6,046 tennis rows), and a seeded
    # last_price at any other value is left to has_real_trading's volume gate.
    if (
        snap.last_price is not None
        and abs(snap.last_price - 0.5) < 1e-9
        and not snap.volume
    ):
        return None
    return snap.last_price


def _to_team_perspective(p_home: float, market: Market, game: NflGame) -> float:
    return round(p_home, 4) if market.team == game.home_team else round(1 - p_home, 4)


def _batch_latest_snapshots(session: Session, market_ids: list[int]) -> dict[int, MarketSnapshot]:
    """Latest MarketSnapshot per market_id, one query for the whole list
    instead of the one-per-row `latest_snapshot()` call this replaced in the
    two hot loops below. Caught while investigating an unrelated slow
    request (line-movement's first per-row version measured 15.7s on
    /markets/futures) -- profiling showed THIS N+1 pattern, not line
    movement, was the real bulk of it (2,242 individual queries). Same
    GROUP BY MAX(ts) + join pattern as _batch_old_implied_probs, made cheap
    by the same (market_id, ts) index."""
    if not market_ids:
        return {}
    from sqlalchemy import func

    # Chunk the IN (...) list: SQLite caps host variables per statement
    # (SQLITE_MAX_VARIABLE_NUMBER -- 999 before 3.32, 32766 after), so passing
    # every market_id at once raises "too many SQL variables". Per-sport
    # callers stay well under it, but the cross-platform divergence scanner
    # feeds this EVERY non-settled market (tens of thousands) -- that's what
    # actually tripped it (2026-07-23). 900 keeps a safe margin even on old
    # SQLite while staying a handful of queries.
    out: dict[int, MarketSnapshot] = {}
    for i in range(0, len(market_ids), 900):
        chunk = market_ids[i:i + 900]
        subq = (
            session.query(MarketSnapshot.market_id, func.max(MarketSnapshot.ts).label("max_ts"))
            .filter(MarketSnapshot.market_id.in_(chunk))
            .group_by(MarketSnapshot.market_id)
            .subquery()
        )
        rows = (
            session.query(MarketSnapshot)
            .join(subq, (MarketSnapshot.market_id == subq.c.market_id) & (MarketSnapshot.ts == subq.c.max_ts))
            .all()
        )
        for snap in rows:
            out[snap.market_id] = snap
    return out


LINE_MOVEMENT_HOURS = 6


def _batch_old_implied_probs(session: Session, market_ids: list[int]) -> dict[int, float | None]:
    """Old implied price (closest snapshot at-or-before LINE_MOVEMENT_HOURS
    ago) per market_id, for every market in ONE query instead of one query
    per row. First cut of this (per-row `session.query(...).first()` inside
    the main loop) measured 15.7s on the 2,192-row /markets/futures endpoint
    -- caught by timing it live before shipping, not assumed fast. Uses a
    GROUP BY MAX(ts) subquery joined back to MarketSnapshot, which the new
    (market_id, ts) index makes cheap, rather than fetching every historical
    snapshot for these markets (which would be much larger than needed)."""
    if not market_ids:
        return {}
    from sqlalchemy import func

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=LINE_MOVEMENT_HOURS)
    # Chunked for the same SQLite host-variable-limit reason as
    # _batch_latest_snapshots. Per-sport callers stay under the cap today, but
    # keep this robust to a large-market sport (or a future all-markets caller).
    out: dict[int, float | None] = {}
    for i in range(0, len(market_ids), 900):
        chunk = market_ids[i:i + 900]
        subq = (
            session.query(MarketSnapshot.market_id, func.max(MarketSnapshot.ts).label("max_ts"))
            .filter(MarketSnapshot.market_id.in_(chunk), MarketSnapshot.ts <= cutoff)
            .group_by(MarketSnapshot.market_id)
            .subquery()
        )
        rows = (
            session.query(MarketSnapshot)
            .join(subq, (MarketSnapshot.market_id == subq.c.market_id) & (MarketSnapshot.ts == subq.c.max_ts))
            .all()
        )
        for snap in rows:
            out[snap.market_id] = _implied_prob(snap)
    return out


def _line_movement_pp(old_implied: float | None, current_implied: float | None) -> float | None:
    """Change in this market's own implied price over the last
    LINE_MOVEMENT_HOURS -- data that was already being collected every
    5-minute poll cycle (MarketSnapshot) but never used for anything beyond
    'the latest price' until now. Positive = price for THIS row's team/side
    has risen (market moving toward it) over the window; None if the market
    hasn't been tracked long enough yet to have a snapshot that old (see
    _batch_old_implied_probs, called once per request)."""
    if current_implied is None or old_implied is None:
        return None
    return round(current_implied - old_implied, 4)


def _spread_model_prob(m: Market, game: NflGame, news: NewsAdjustment | None) -> float | None:
    """Folds the situational/news layer into the margin-space spread model
    by reusing the SAME win-probability blend moneyline uses
    (combine_probability), then inverting the result back to an effective
    elo_diff (elo.py::implied_elo_diff) -- rather than hand-tuning a second,
    separate margin-space version of every situational factor."""
    if m.team is None or m.line is None:
        return None
    home_r = elo_service.get_team_rating(game.home_team)
    away_r = elo_service.get_team_rating(game.away_team)
    if home_r is None or away_r is None:
        return None
    hfa = effective_home_field_adv(game.home_team, game.location)
    p_home_baseline = win_prob(home_r, away_r, hfa)
    p_home_final = combine_probability(p_home_baseline, news, is_divisional=bool(game.div_game))
    elo_diff_effective = implied_elo_diff(p_home_final)
    return round(game_lines.prob_team_covers(m.team == game.home_team, m.line, elo_diff_effective), 4)


def _predict_score(
    game: NflGame, news: NewsAdjustment | None, home_penalty_pp: float = 0.0, away_penalty_pp: float = 0.0
) -> tuple[float, float] | None:
    """Predicted final score (home, away) -- not a new model, just the
    existing spread model's expected MARGIN combined with the existing
    totals model's expected TOTAL, both already computed for their own
    markets. score = (total +/- margin) / 2, the standard way to recover
    two team scores from a total and a margin. Reuses the exact same
    news-blended elo_diff spread already uses (via combine_probability +
    implied_elo_diff) and the exact same weather/structural/EPA shifts
    totals already uses, so this prediction is consistent with what the
    spread/total markets themselves show, not a third independent guess."""
    home_r = elo_service.get_team_rating(game.home_team)
    away_r = elo_service.get_team_rating(game.away_team)
    if home_r is None or away_r is None:
        return None
    hfa = effective_home_field_adv(game.home_team, game.location)
    p_home_baseline = win_prob(home_r, away_r, hfa)
    p_home_final = combine_probability(p_home_baseline, news, is_divisional=bool(game.div_game))
    elo_diff_effective = implied_elo_diff(p_home_final)
    margin = game_lines.expected_margin(elo_diff_effective)

    ratings = scoring_ratings_service.get_ratings()
    total, _ = game_lines.expected_total(ratings.get(game.home_team), ratings.get(game.away_team))
    weather_suppression = weather_rules.compute_total_points_adjustment(game.home_team, game.away_team, game.roof, game.gameday)
    structural_shift = game_lines.structural_total_shift(
        is_divisional=bool(game.div_game),
        is_dome=weather_rules.is_dome_game(game.home_team, game.roof),
        is_turf=weather_rules.is_turf_game(game.surface),
    )
    epa_shift = _epa_mismatch_shift(game.home_team, game.away_team)
    scoring_penalty_shift = _scoring_penalty_points(home_penalty_pp) + _scoring_penalty_points(away_penalty_pp)
    total_adjusted = total - (weather_suppression + structural_shift + epa_shift + scoring_penalty_shift)

    home_score = (total_adjusted + margin) / 2
    away_score = (total_adjusted - margin) / 2
    return round(max(home_score, 0.0), 1), round(max(away_score, 0.0), 1)


def _scoring_penalty_points(pp: float) -> float:
    """Rough, honestly-UNVALIDATED conversion from a win-probability-space
    injury penalty (percentage points -- backup-QB quality + injury
    clustering, see injury_rules.py::offense_scoring_penalty_pp) to points
    of scoring-output suppression for the AFFECTED team's own total. Reuses
    the same logistic-inversion machinery already used to fold pp-space
    signals into margin space (implied_elo_diff -> game_lines.expected_margin),
    then takes HALF the resulting margin-points swing as this team's own
    share of it (a margin shift is partly 'this team scores less', partly
    'the opponent allows more/scores more' -- a conservative, not
    empirically-fitted, 50/50 split). Unlike weather/EPA-mismatch/structural
    shifts in game_lines.py, this has NOT been backtested against real
    total-points outcomes -- same 'honestly flagged as rough' category as
    weather_rules.py's TOTAL_WEATHER_MAX_SUPPRESSION_PTS constant."""
    if pp <= 0:
        return 0.0
    elo_diff_penalty = implied_elo_diff(0.5 - pp / 100.0)  # implied_elo_diff(0.5) == 0.0
    return abs(game_lines.expected_margin(elo_diff_penalty)) / 2.0


def _epa_mismatch_shift(home_team: str, away_team: str) -> float:
    """Shared by _total_model_prob/_team_total_model_prob/_half_total_model_prob
    -- see game_lines.py::epa_mismatch_total_shift's docstring for the real
    two-sided-EPA-intensity-vs-actual-total correlation (r=0.104) this is
    derived from. Backtested end-to-end in backend/scripts/backtest_totals.py."""
    ratings = epa_ratings.get_current_epa_ratings()
    home = ratings.get(home_team)
    away = ratings.get(away_team)
    if home is None or away is None:
        return 0.0
    return game_lines.epa_mismatch_total_shift(
        home.get("off_epa"), home.get("def_epa_allowed"), away.get("off_epa"), away.get("def_epa_allowed")
    )


def _total_model_prob(m: Market, game: NflGame, home_penalty_pp: float = 0.0, away_penalty_pp: float = 0.0) -> float | None:
    """Weather + the EPA-mismatch signal get folded into the totals model,
    not the full situational layer -- most situational factors (injuries,
    rest, playoff motivation, ...) are about WHO wins, not how many total
    points are scored, so blending them into total-points space would
    conflate unrelated effects. Weather's scoring-suppression effect and the
    EPA-mismatch intensity are the two signals with a direct, well-documented
    totals relationship (see weather_rules.py::compute_total_points_adjustment
    and game_lines.py::epa_mismatch_total_shift). The backup-QB-quality/
    injury-clustering penalty is the one exception -- it genuinely IS about
    scoring volume (see injury_rules.py::offense_scoring_penalty_pp), so both
    teams' penalties (converted to points via _scoring_penalty_points) are
    summed into the game total's suppression here."""
    if m.line is None or m.side not in ("over", "under"):
        return None
    ratings = scoring_ratings_service.get_ratings()
    weather_suppression = weather_rules.compute_total_points_adjustment(
        game.home_team, game.away_team, game.roof, game.gameday
    )
    structural_shift = game_lines.structural_total_shift(
        is_divisional=bool(game.div_game),
        is_dome=weather_rules.is_dome_game(game.home_team, game.roof),
        is_turf=weather_rules.is_turf_game(game.surface),
    )
    epa_shift = _epa_mismatch_shift(game.home_team, game.away_team)
    scoring_penalty_shift = _scoring_penalty_points(home_penalty_pp) + _scoring_penalty_points(away_penalty_pp)
    p_over = game_lines.prob_over(
        m.line,
        ratings.get(game.home_team),
        ratings.get(game.away_team),
        points_shift=weather_suppression + structural_shift + epa_shift + scoring_penalty_shift,
    )
    return round(p_over if m.side == "over" else (1.0 - p_over), 4)


def _team_total_model_prob(m: Market, game: NflGame, home_penalty_pp: float = 0.0, away_penalty_pp: float = 0.0) -> float | None:
    """Same weather/structural shift as the game total, but halved -- those
    shifts are calibrated as GAME-total magnitudes (e.g. divisional games
    average 1.5 FEWER combined points); applying the full amount to each
    team's own total separately would double it when summed. The scoring
    penalty is different: it's already ONE team's own signal (backup-QB/
    clustering), so the team being bet on gets ITS OWN penalty at full
    (unhalved) weight -- not halved, and not the opponent's."""
    if m.team is None or m.line is None or m.side not in ("over", "under"):
        return None
    ratings = scoring_ratings_service.get_ratings()
    is_home = m.team == game.home_team
    team_scoring = ratings.get(m.team)
    opponent_scoring = ratings.get(game.away_team if is_home else game.home_team)

    weather_suppression = weather_rules.compute_total_points_adjustment(
        game.home_team, game.away_team, game.roof, game.gameday
    )
    structural_shift = game_lines.structural_total_shift(
        is_divisional=bool(game.div_game),
        is_dome=weather_rules.is_dome_game(game.home_team, game.roof),
        is_turf=weather_rules.is_turf_game(game.surface),
    )
    epa_shift = _epa_mismatch_shift(game.home_team, game.away_team)
    own_penalty_pp = home_penalty_pp if is_home else away_penalty_pp
    scoring_penalty_shift = _scoring_penalty_points(own_penalty_pp)
    p_over = game_lines.prob_team_over(
        m.line,
        team_scoring,
        opponent_scoring,
        points_shift=(weather_suppression + structural_shift + epa_shift) / 2 + scoring_penalty_shift,
    )
    return round(p_over if m.side == "over" else (1.0 - p_over), 4)


def _half_spread_model_prob(m: Market, game: NflGame, news: NewsAdjustment | None, half: int) -> float | None:
    """Half-specific version of _spread_model_prob -- half in {1, 2}."""
    if m.team is None or m.line is None:
        return None
    home_r = elo_service.get_team_rating(game.home_team)
    away_r = elo_service.get_team_rating(game.away_team)
    if home_r is None or away_r is None:
        return None
    hfa = effective_home_field_adv(game.home_team, game.location)
    p_home_baseline = win_prob(home_r, away_r, hfa)
    p_home_final = combine_probability(p_home_baseline, news, is_divisional=bool(game.div_game))
    elo_diff_effective = implied_elo_diff(p_home_final)
    return round(game_lines.prob_team_covers_half(m.team == game.home_team, m.line, elo_diff_effective, half), 4)


def _half_total_model_prob(
    m: Market, game: NflGame, half: int, home_penalty_pp: float = 0.0, away_penalty_pp: float = 0.0
) -> float | None:
    """Half-specific version of _total_model_prob -- half in {1, 2}. The
    weather/structural/scoring-penalty shift is passed at full-game scale;
    prob_over_half scales it down by that half's own share internally."""
    if m.line is None or m.side not in ("over", "under"):
        return None
    ratings = scoring_ratings_service.get_ratings()
    weather_suppression = weather_rules.compute_total_points_adjustment(
        game.home_team, game.away_team, game.roof, game.gameday
    )
    structural_shift = game_lines.structural_total_shift(
        is_divisional=bool(game.div_game),
        is_dome=weather_rules.is_dome_game(game.home_team, game.roof),
        is_turf=weather_rules.is_turf_game(game.surface),
    )
    epa_shift = _epa_mismatch_shift(game.home_team, game.away_team)
    scoring_penalty_shift = _scoring_penalty_points(home_penalty_pp) + _scoring_penalty_points(away_penalty_pp)
    p_over = game_lines.prob_over_half(
        m.line,
        ratings.get(game.home_team),
        ratings.get(game.away_team),
        half,
        points_shift=weather_suppression + structural_shift + epa_shift + scoring_penalty_shift,
    )
    return round(p_over if m.side == "over" else (1.0 - p_over), 4)


@router.get("", response_model=list[MarketOut])
def list_markets(session: Session = Depends(get_session)):
    excluded = (
        set(FUTURES_SIM_KEY.keys())
        | LEAGUE_FUTURES_TYPES
        | WIN_LADDER_FUTURES_TYPES
        | {WEEK1_QB_FUTURES_TYPE}
        | AWARD_FUTURES_TYPES
        | DIVISION_EXTRA_TYPES
        | LEADER_FUTURES_TYPES
        | TEAM_POINTS_FUTURES_TYPES
        | SEASON_STAT_FUTURES_TYPES
    )
    # sport=="nfl" is REQUIRED here: this endpoint predates the multi-sport
    # split and was querying EVERY sport's game markets (34k rows, ~11s),
    # showing e.g. NBA/MLB markets on the NFL page with a null NFL model_prob.
    # Each other sport's router already scopes to its own sport; NFL must too.
    markets = session.query(Market).filter(Market.sport == "nfl", Market.market_type.notin_(excluded), Market.status == "active").all()
    # Skip markets tied to a game that's already final -- once the poller
    # stops refreshing a played game's market, its price is frozen while this
    # endpoint would otherwise keep computing a fresh model_prob off current
    # Elo, "predicting" an already-decided game. Same fix as mlb_markets.py.
    game_ids = {m.nfl_game_id for m in markets if m.nfl_game_id}
    finished_game_ids = (
        {gid for (gid,) in session.query(NflGame.id).filter(NflGame.id.in_(game_ids), NflGame.home_score.isnot(None)).all()}
        if game_ids else set()
    )
    # SECOND, related gap (confirmed real but not yet triggered -- current
    # NFL games are all preseason/off-season with no baseline anyway, same
    # reasoning documented in mlb_markets.py's own version of this fix,
    # fixed here proactively before the regular season makes it bite the way
    # it did for MLB): the check above only excludes games with a recorded
    # FINAL score, but a game that's simply IN PROGRESS has no home_score yet
    # either, so its market would sail through with a live, already-decided
    # price compared against a static PREGAME model_prob. Excluded once the
    # game's real local kickoff instant (gameday + gametime, stadium
    # timezone -- see data/stadiums.py::NFL_TEAM_TZ) is confirmed in the
    # past, same "real instant, not a guess" approach as MLB's fix.
    games_by_id = {g.id: g for g in session.query(NflGame).filter(NflGame.id.in_(game_ids)).all()} if game_ids else {}
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _game_already_started(m: Market) -> bool:
        game = games_by_id.get(m.nfl_game_id) if m.nfl_game_id else None
        # "00:00" is nflverse's own placeholder for "kickoff not yet
        # announced" (see RecommendedBetsTable.tsx's formatGameDate), not a
        # real midnight kickoff -- treated as unknown, same as a blank gametime.
        if game is None or not game.gameday or not game.gametime or game.gametime == "00:00":
            return False
        tz_name = NFL_TEAM_TZ.get(game.home_team)
        if tz_name is None:
            return False
        try:
            local_naive = datetime.datetime.strptime(f"{game.gameday} {game.gametime}", "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        kickoff_utc = local_naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(datetime.timezone.utc)
        return now_utc >= kickoff_utc

    # THIRD gap (2026-07-19, found while chasing the same class of bug for
    # Tennis/MMA/MLB/NBA -- see ladder_sanity.py): the checks above depend
    # on this app's own schedule data being right about the game's real
    # kickoff instant. Detected structurally instead, as a second layer: a
    # real, still-live pregame ladder (spread/total/team_total, any half-
    # variant) never prices two DIFFERENT thresholds at the same extreme
    # value -- seeing that happen is a direct tell the real outcome is
    # already locked in, independent of any timestamp this app stores.
    all_snapshots_pre = _batch_latest_snapshots(session, [m.id for m in markets])
    _LADDER_TYPES = {"spread", "total", "team_total", "spread_1h", "spread_2h", "total_1h", "total_2h"}
    ladder_groups: dict[tuple, list[tuple[float, float]]] = {}
    for m in markets:
        if m.line is None or m.nfl_game_id is None or m.market_type not in _LADDER_TYPES:
            continue
        implied = _implied_prob(all_snapshots_pre.get(m.id))
        if implied is None:
            continue
        ladder_groups.setdefault((m.nfl_game_id, m.market_type, m.team), []).append((m.line, implied))
    resolved_group_keys = find_resolved_entities(ladder_groups)
    games_with_resolved_ladder = {key[0] for key in resolved_group_keys}

    def _game_ladder_resolved(m: Market) -> bool:
        return m.nfl_game_id in games_with_resolved_ladder

    markets = [
        m for m in markets
        if m.nfl_game_id not in finished_game_ids
        and not _game_already_started(m)
        and not _game_ladder_resolved(m)
    ]
    weekly_pool, futures_pool = get_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)
    old_implied_by_market = _batch_old_implied_probs(session, [m.id for m in markets])
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    # _predict_score is identical for every row belonging to the same game
    # (both team-perspective rows, both sources) -- memoized per game so it
    # only actually runs once each, not up to 4x redundantly.
    score_prediction_cache: dict[str, tuple[float, float] | None] = {}
    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        game = session.get(NflGame, m.nfl_game_id) if m.nfl_game_id else None
        implied = _implied_prob(snap)

        model_prob = None
        final_prob = None
        news_cache = None
        no_baseline_reason = None
        predicted_home_score = None
        predicted_away_score = None
        if game is not None:
            if game.game_type == "PRE":
                no_baseline_reason = NO_PRESEASON_BASELINE_REASON
            else:
                news_cache = get_news_adjustment_cache(session, game.id)
                news = news_cache_to_pydantic(news_cache) if news_cache else None
                if m.market_type == "moneyline" and m.team is not None:
                    p_home_baseline = elo_service.get_home_win_prob(game.home_team, game.away_team, game.location)
                    if p_home_baseline is not None:
                        model_prob = _to_team_perspective(p_home_baseline, m, game)
                        p_home_final = combine_probability(p_home_baseline, news, is_divisional=bool(game.div_game))
                        final_prob = _to_team_perspective(p_home_final, m, game)
                    cache_key = f"{game.id}:{m.source}"
                    if cache_key not in score_prediction_cache:
                        score_prediction_cache[cache_key] = _predict_score(
                            game, news,
                            home_penalty_pp=(news_cache.home_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                            away_penalty_pp=(news_cache.away_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                        )
                    score_prediction = score_prediction_cache[cache_key]
                    if score_prediction is not None:
                        predicted_home_score, predicted_away_score = score_prediction
                elif m.market_type == "spread":
                    # model_prob already reflects the news blend (folded in via
                    # implied_elo_diff) -- final_prob stays unused for spread.
                    model_prob = _spread_model_prob(m, game, news)
                elif m.market_type == "total":
                    model_prob = _total_model_prob(
                        m, game,
                        home_penalty_pp=(news_cache.home_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                        away_penalty_pp=(news_cache.away_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                    )
                elif m.market_type == "team_total":
                    model_prob = _team_total_model_prob(
                        m, game,
                        home_penalty_pp=(news_cache.home_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                        away_penalty_pp=(news_cache.away_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                    )
                elif m.market_type in ("spread_1h", "spread_2h"):
                    model_prob = _half_spread_model_prob(m, game, news, 1 if m.market_type == "spread_1h" else 2)
                elif m.market_type in ("total_1h", "total_2h"):
                    model_prob = _half_total_model_prob(
                        m, game, 1 if m.market_type == "total_1h" else 2,
                        home_penalty_pp=(news_cache.home_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                        away_penalty_pp=(news_cache.away_scoring_penalty_pp or 0.0) if news_cache else 0.0,
                    )

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "nfl", m.market_type)
        pool = weekly_pool if is_weekly_market_type(m.market_type) else futures_pool
        # Futures get the same reduced unit every other sport's futures use. This
        # router was the only one that never passed it, so NFL season-long bets
        # were sized at a full unit while identical bets in mlb/nba/soccer/tennis/
        # cfb/esports were sized at 0.25 -- 4x the intended exposure on the
        # longest-dated, least-validated markets in the app. Measured before the
        # fix: /markets/futures returned 99 rows at 1.0u and 15 at 0.5u, against
        # 0.25/0.125 everywhere else.
        _uscale = FUTURES_UNIT_SCALE if pool is futures_pool else 1.0
        # Same conditional as _uscale: the price floor is a FUTURES rule, and
        # this one function serves both pools. See FUTURES_MIN_MARKET_PRICE.
        _minpx = FUTURES_MIN_MARKET_PRICE if pool is futures_pool else 0.0
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=_uscale, min_market_price=_minpx)
        out.append(
            MarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                nfl_game_id=m.nfl_game_id,
                game_label=f"{game.away_team} @ {game.home_team}" if game else None,
                gameday=game.gameday if game else None,
                gametime=game.gametime if game else None,
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
                news_adjustment_pct=news_cache.adjustment_pct if news_cache else None,
                news_confidence=news_cache.confidence if news_cache else None,
                news_requires_review=bool(news_cache.requires_review) if news_cache else False,
                final_prob=final_prob,
                no_baseline_reason=no_baseline_reason,
                predicted_home_score=predicted_home_score,
                predicted_away_score=predicted_away_score,
                line_move_pp=_line_movement_pp(old_implied_by_market.get(m.id), implied),
                kelly_fraction=kelly,
                suggested_stake_dollars=stake_dollars,
                suggested_stake_units=round(stake_dollars / unit_dollars, 3) if (stake_dollars is not None and unit_dollars > 0) else None,
                stake_pool=("weekly" if is_weekly_market_type(m.market_type) else "futures") if kelly is not None else None,
            )
        )
    out.sort(key=lambda m: (m.gameday or "9999", m.game_label or ""))
    return out


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_futures(session: Session = Depends(get_session)):
    """Division winner / conference champion / 1-seed / Super Bowl champion /
    playoff qualifier -- season-long markets priced against the season Monte
    Carlo simulator (season_sim.py), not a single game's Elo matchup. Same
    model_validated: false honesty as the per-game dashboard -- this hasn't
    been backtested any more than Elo itself has."""
    sim_results = season_sim_service.get_results()
    included = (
        set(FUTURES_SIM_KEY.keys())
        | LEAGUE_FUTURES_TYPES
        | WIN_LADDER_FUTURES_TYPES
        | {WEEK1_QB_FUTURES_TYPE, "stage_of_elimination"}
        | AWARD_FUTURES_TYPES
        | DIVISION_EXTRA_TYPES
        | LEADER_FUTURES_TYPES
        | TEAM_POINTS_FUTURES_TYPES
        | SEASON_STAT_FUTURES_TYPES
    )
    # sport=="nfl" required for the same reason as list_markets above -- this
    # futures endpoint was returning every sport's futures (2,779 rows, ~18s).
    markets = session.query(Market).filter(Market.sport == "nfl", Market.market_type.in_(included), Market.status == "active").all()
    weekly_pool, futures_pool = get_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)
    old_implied_by_market = _batch_old_implied_probs(session, [m.id for m in markets])
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])

    max_season = session.query(NflGame.season).order_by(NflGame.season.desc()).limit(1).scalar()
    try:
        starters_by_team = depth_chart_client.get_skill_position_starters(max_season, fetch_if_missing=False) if max_season else {}
        qb_backup_by_team = depth_chart_client.get_qb_backup(max_season, fetch_if_missing=False) if max_season else {}
    except Exception:
        starters_by_team, qb_backup_by_team = {}, {}

    # Award scoring (see app/models/awards.py) needs the full candidate list
    # up front to normalize probabilities across all of them, unlike every
    # other market_type here which is scored row-by-row independently.
    mvp_rows = [m for m in markets if m.market_type == "mvp"]
    coty_rows = [m for m in markets if m.market_type == "coach_of_year"]
    opoy_rows = [m for m in markets if m.market_type == "opoy"]
    dpoy_rows = [m for m in markets if m.market_type == "dpoy"]
    mvp_scores: dict[str, float] = {}
    coty_scores: dict[str, float] = {}
    opoy_scores: dict[str, float] = {}
    dpoy_scores: dict[str, float] = {}
    if mvp_rows or coty_rows or opoy_rows or dpoy_rows:
        qb_rb_name_to_team = build_qb_rb_full_name_to_team(starters_by_team)
        offensive_skill_name_to_team = build_offensive_skill_full_name_to_team(starters_by_team)
        qb_stats = get_qb_career_stats()
        rush_stats = get_rushing_career_stats()
        if mvp_rows:
            mvp_scores = compute_mvp_scores(
                [m.group_label for m in mvp_rows if m.group_label], qb_rb_name_to_team, sim_results, qb_stats, rush_stats
            )
        if opoy_rows:
            recv_stats = get_receiving_career_stats()
            opoy_scores = compute_opoy_scores(
                [m.group_label for m in opoy_rows if m.group_label],
                offensive_skill_name_to_team,
                sim_results,
                qb_stats,
                rush_stats,
                recv_stats,
            )
        if dpoy_rows:
            try:
                pooled_starters = depth_chart_client.get_current_starters(max_season, fetch_if_missing=False) if max_season else {}
            except Exception:
                pooled_starters = {}
            all_starters_name_to_team = build_all_starters_full_name_to_team(pooled_starters)
            defensive_scores = get_defensive_career_scores()
            dpoy_scores = compute_dpoy_scores(
                [m.group_label for m in dpoy_rows if m.group_label], all_starters_name_to_team, sim_results, defensive_scores
            )
        if coty_rows:
            coach_by_team: dict[str, str] = {}
            last_season_wins: dict[str, int] = {}
            if max_season:
                for g in session.query(NflGame).filter(NflGame.season == max_season).all():
                    if g.home_coach:
                        coach_by_team.setdefault(g.home_team, g.home_coach)
                    if g.away_coach:
                        coach_by_team.setdefault(g.away_team, g.away_coach)
                for g in session.query(NflGame).filter(
                    NflGame.season == max_season - 1, NflGame.game_type == "REG", NflGame.home_score.isnot(None)
                ):
                    if g.home_score > g.away_score:
                        last_season_wins[g.home_team] = last_season_wins.get(g.home_team, 0) + 1
                    elif g.away_score > g.home_score:
                        last_season_wins[g.away_team] = last_season_wins.get(g.away_team, 0) + 1
            coach_name_to_team = build_coach_name_to_team(coach_by_team)
            coty_scores = compute_coty_scores(
                [m.group_label for m in coty_rows if m.group_label], coach_name_to_team, sim_results, last_season_wins
            )

    # Division wins/order/extremes + worst-to-first + H2H (see
    # app/models/division_markets.py) -- div_least_wins/div_most_wins need
    # ALL 8 divisions' distributions compared at once, so computed here
    # rather than per-row like every other market_type.
    sim_divisions = sim_results.get("_DIVISIONS", {})
    div_least_scores: dict[str, float] = {}
    div_most_scores: dict[str, float] = {}
    worst_to_first_prob: float | None = None
    if any(m.market_type in ("div_least_wins", "div_most_wins") for m in markets) and sim_divisions:
        div_least_scores = division_extreme_model_probs(sim_divisions, "least")
        div_most_scores = division_extreme_model_probs(sim_divisions, "most")
    if any(m.market_type == "worst_to_first" for m in markets) and max_season:
        last_season_wins_by_team: dict[str, int] = {}
        for g in session.query(NflGame).filter(
            NflGame.season == max_season - 1, NflGame.game_type == "REG", NflGame.home_score.isnot(None)
        ):
            if g.home_score > g.away_score:
                last_season_wins_by_team[g.home_team] = last_season_wins_by_team.get(g.home_team, 0) + 1
            elif g.away_score > g.home_score:
                last_season_wins_by_team[g.away_team] = last_season_wins_by_team.get(g.away_team, 0) + 1
        last_season_worst_by_division: dict[str, str] = {}
        for div, teams in DIVISIONS.items():
            rated = [(t, last_season_wins_by_team[t]) for t in teams if t in last_season_wins_by_team]
            if rated:
                last_season_worst_by_division[div] = min(rated, key=lambda x: x[1])[0]
        worst_to_first_prob = worst_to_first_model_prob(last_season_worst_by_division, sim_results)

    # League-leader stat markets (see app/models/stat_leaders.py) -- each
    # category normalized independently, same "sum to 1 across only
    # resolvable candidates" convention as the awards scoring above.
    leader_scores: dict[str, dict[str, float]] = {}
    leader_rows_by_type = {lt: [m for m in markets if m.market_type == lt] for lt in LEADER_FUTURES_TYPES}
    if any(leader_rows_by_type.values()):
        stat_totals = get_stat_leader_totals()
        category_by_type = {
            "leader_pass_yds": "pass_yds", "leader_pass_tds": "pass_tds", "leader_pass_int": "pass_int",
            "leader_rush_yds": "rush_yds", "leader_rush_tds": "rush_tds",
            "leader_rec_yds": "rec_yds", "leader_rec_tds": "rec_tds",
            "leader_def_int": "def_int", "leader_sacks": "sacks",
        }
        for lt, rows in leader_rows_by_type.items():
            if not rows:
                continue
            category = category_by_type[lt]
            leader_scores[lt] = compute_leader_scores(
                [m.group_label for m in rows if m.group_label], stat_totals.get(category, {})
            )

    # Team points-scored/allowed most/least (see _team_points_scores above).
    team_points_scores: dict[str, dict[str, float]] = {}
    if any(m.market_type in TEAM_POINTS_FUTURES_TYPES for m in markets):
        scoring_ratings = scoring_ratings_service.get_ratings()
        for mode in ("pts_most", "pts_least", "dpts_most", "dpts_least"):
            team_points_scores[f"team_{mode}"] = _team_points_scores(scoring_ratings, mode)

    # Season-total threshold ladders (see app/models/season_projections.py).
    # QB/RB role-continuity check (real starter this season vs. last) needs
    # the current-season depth chart -- reuses starters_by_team, already
    # fetched above for week1_qb/OPOY.
    prior_season_stats: dict[str, dict] = {}
    if any(m.market_type in SEASON_STAT_FUTURES_TYPES for m in markets):
        prior_season_stats = get_prior_season_stats()

    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)

        if m.market_type == "undefeated_season":
            league = sim_results.get("_LEAGUE") or {}
            model_prob = round(league["any_undefeated_pct"], 4) if "any_undefeated_pct" in league else None
        elif m.market_type == "wins_any":
            # league-wide "will ANY team hit >= line wins" -- any_wins_ge_pct
            # is already P(at least one team >= i wins) at index i, so this
            # is a direct lookup, no summing needed (unlike win_total below).
            league = sim_results.get("_LEAGUE") or {}
            any_wins_ge = league.get("any_wins_ge_pct")
            idx = int(m.line) if m.line is not None else None
            model_prob = round(any_wins_ge[idx], 4) if (any_wins_ge and idx is not None and 0 <= idx < len(any_wins_ge)) else None
        elif m.market_type in WIN_LADDER_FUTURES_TYPES:
            team_sim = sim_results.get(m.team) if m.team else None
            win_count_pct = team_sim.get("win_count_pct") if team_sim else None
            idx = int(m.line) if m.line is not None else None
            if win_count_pct and idx is not None and 0 <= idx < len(win_count_pct):
                if m.market_type == "win_total":
                    # over/under ladder: floor_strike = "at least N wins" -- sum the tail of the histogram.
                    model_prob = round(sum(win_count_pct[idx:]), 4)
                else:
                    # exact_win_total: floor_strike = the exact win count itself.
                    model_prob = round(win_count_pct[idx], 4)
            else:
                model_prob = None
        elif m.market_type == "mvp":
            model_prob = mvp_scores.get(qb_canonical_key(m.group_label)) if m.group_label else None
        elif m.market_type == "coach_of_year":
            model_prob = coty_scores.get(coach_name_key(m.group_label)) if m.group_label else None
        elif m.market_type == "opoy":
            model_prob = opoy_scores.get(qb_canonical_key(m.group_label)) if m.group_label else None
        elif m.market_type == "dpoy":
            model_prob = dpoy_scores.get(qb_canonical_key(m.group_label)) if m.group_label else None
        elif m.market_type == "division_wins":
            model_prob = division_wins_model_prob(m.team, m.line, sim_divisions) if (m.team and m.line is not None) else None
        elif m.market_type == "division_order":
            model_prob = division_order_model_prob(m.team, m.side, sim_divisions) if (m.team and m.side) else None
        elif m.market_type == "div_least_wins":
            div_key = division_code_to_key(m.team) if m.team else None
            model_prob = div_least_scores.get(div_key) if div_key else None
        elif m.market_type == "div_most_wins":
            div_key = division_code_to_key(m.team) if m.team else None
            model_prob = div_most_scores.get(div_key) if div_key else None
        elif m.market_type == "worst_to_first":
            model_prob = worst_to_first_prob
        elif m.market_type in LEADER_FUTURES_TYPES:
            model_prob = (
                leader_scores.get(m.market_type, {}).get(qb_canonical_key(m.group_label))
                if m.group_label
                else None
            )
        elif m.market_type in TEAM_POINTS_FUTURES_TYPES:
            model_prob = team_points_scores.get(m.market_type, {}).get(m.team) if m.team else None
        elif m.market_type == "h2h_wins":
            # source_event_id e.g. "KXNFLH2HWINS-27TBPIT" -- everything after
            # the "-27" award-year marker is the two teams' Kalshi codes
            # concatenated with no separator, same blob-split approach
            # already used for game tickers elsewhere in this app.
            model_prob = None
            if m.source_event_id and m.team and "-27" in m.source_event_id:
                blob = m.source_event_id.split("-27", 1)[-1]
                split = split_teams_blob(blob, KALSHI_TEAM_ABBRS)
                if split:
                    a, b = to_nflverse_abbr(split[0]), to_nflverse_abbr(split[1])
                    opponent = b if a == m.team else (a if b == m.team else None)
                    if opponent:
                        model_prob = h2h_model_prob(m.team, opponent, sim_results)
        elif m.market_type in SEASON_STAT_FUTURES_TYPES:
            model_prob = None
            if m.group_label and m.line is not None:
                category = SEASON_STAT_CATEGORY_BY_TYPE[m.market_type]
                key = qb_canonical_key(m.group_label)
                prior_entry = prior_season_stats.get(category, {}).get(key)
                # is_current_starter must stay None when the depth chart has NO
                # entry for this team (unknown -> don't discount), and only be
                # True/False when a starter is actually known. Fixed 2026-07-23:
                # the old `bool(qb1 and ...)` collapsed a MISSING depth chart to
                # False, applying the 0.4x not-the-starter discount to
                # established starters and crushing their projection to ~0 (e.g.
                # a whole offseason where the upcoming-season depth chart isn't
                # published yet -> every QB looked benched). Missing != benched.
                is_current_starter = None
                if category in SEASON_STAT_QB_CATEGORIES:
                    qb1 = starters_by_team.get(m.team, {}).get("QB") if m.team else None
                    is_current_starter = (qb_canonical_key(qb1) == key) if qb1 else None
                elif category in SEASON_STAT_RB_CATEGORIES:
                    rb1 = starters_by_team.get(m.team, {}).get("RB") if m.team else None
                    is_current_starter = (qb_canonical_key(rb1) == key) if rb1 else None
                model_prob = prob_exceeds_season_total(category, m.line, prior_entry, is_current_starter)
        elif m.market_type == WEEK1_QB_FUTURES_TYPE:
            # group_label holds the candidate player's name here, not a
            # market description -- see upsert_polymarket_week1_qb_market.
            model_prob = (
                _week1_qb_model_prob(m.team, m.group_label, starters_by_team, qb_backup_by_team)
                if (m.team and m.group_label)
                else None
            )
        elif m.market_type == "stage_of_elimination":
            # side = reg|wc|div|conf|sb_loss|sb_win -> the matching slice of the
            # season sim's per-team stage_exit_pct (the six sum to 1).
            team_sim = sim_results.get(m.team) if m.team else None
            stage_pct = (team_sim or {}).get("stage_exit_pct") if team_sim else None
            model_prob = round(stage_pct[m.side], 4) if (stage_pct and m.side in stage_pct) else None
        else:
            sim_key = FUTURES_SIM_KEY.get(m.market_type)
            team_sim = sim_results.get(m.team) if m.team else None
            model_prob = round(team_sim[sim_key], 4) if (sim_key and team_sim) else None

        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "nfl", m.market_type)
        pool = weekly_pool if is_weekly_market_type(m.market_type) else futures_pool
        # Futures get the same reduced unit every other sport's futures use. This
        # router was the only one that never passed it, so NFL season-long bets
        # were sized at a full unit while identical bets in mlb/nba/soccer/tennis/
        # cfb/esports were sized at 0.25 -- 4x the intended exposure on the
        # longest-dated, least-validated markets in the app. Measured before the
        # fix: /markets/futures returned 99 rows at 1.0u and 15 at 0.5u, against
        # 0.25/0.125 everywhere else.
        _uscale = FUTURES_UNIT_SCALE if pool is futures_pool else 1.0
        # Same conditional as _uscale: the price floor is a FUTURES rule, and
        # this one function serves both pools. See FUTURES_MIN_MARKET_PRICE.
        _minpx = FUTURES_MIN_MARKET_PRICE if pool is futures_pool else 0.0
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=_uscale, min_market_price=_minpx)
        # Player stat-projection futures: show the model_prob/edge but never
        # stake (see PLAYER_STAT_TRACKING_ONLY). Zero out the stake AFTER it's
        # computed so the edge/model number still surfaces for tracking.
        tracking_only = m.market_type in PLAYER_STAT_TRACKING_ONLY
        if tracking_only:
            kelly = None
            stake_dollars = None
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
                stake_pool=("weekly" if is_weekly_market_type(m.market_type) else "futures") if kelly is not None else None,
                line_move_pp=_line_movement_pp(old_implied_by_market.get(m.id), implied),
                model_note=PLAYER_STAT_TRACKING_NOTE if tracking_only and model_prob is not None else None,
            )
        )
    out.sort(key=lambda m: (m.market_type, m.group_label or "", m.team or "", m.line or 0, -(m.implied_prob or 0)))
    return out


@router.post("/{nfl_game_id}/refresh-news")
def refresh_news(nfl_game_id: str, session: Session = Depends(get_session)):
    """Free (ESPN injuries + rest/travel/coach + depth charts + weather, no
    paid API) -- this also already runs automatically on every market-poll
    cycle; this endpoint just lets the UI force an immediate recheck for one
    game."""
    game = session.get(NflGame, nfl_game_id)
    if game is None:
        raise HTTPException(404, "game not found")
    injuries_by_team = espn_client.fetch_all_injuries()
    try:
        starters_by_team = depth_chart_client.get_current_starters(game.season, fetch_if_missing=False)
    except Exception:
        starters_by_team = {}
    away_previous_coach = get_previous_coach(session, game.away_team, game.season, game.week)
    home_previous_coach = get_previous_coach(session, game.home_team, game.season, game.week)

    adjustment, home_scoring_penalty_pp, away_scoring_penalty_pp = compute_situational_adjustment(
        away_team=game.away_team,
        home_team=game.home_team,
        away_qb_name=game.away_qb_name,
        home_qb_name=game.home_qb_name,
        away_injuries=injuries_by_team.get(game.away_team, []),
        home_injuries=injuries_by_team.get(game.home_team, []),
        away_rest=game.away_rest,
        home_rest=game.home_rest,
        roof=game.roof,
        game_date_iso=game.gameday,
        gametime=game.gametime,
        away_coach_current=game.away_coach,
        away_coach_previous=away_previous_coach,
        home_coach_current=game.home_coach,
        home_coach_previous=home_previous_coach,
        away_starters=starters_by_team.get(game.away_team),
        home_starters=starters_by_team.get(game.home_team),
    )
    if adjustment is None:
        return {"status": "no situational factors found", "nfl_game_id": nfl_game_id}
    upsert_news_adjustment(
        session, nfl_game_id, adjustment, research_text="",
        home_scoring_penalty_pp=home_scoring_penalty_pp,
        away_scoring_penalty_pp=away_scoring_penalty_pp,
    )
    return {"status": "news refreshed", "nfl_game_id": nfl_game_id, "adjustment_pct": adjustment.adjustment_pct}


# Methodology text for market types that don't get a live recomputed fact
# breakdown below (awards, division extras, league-leaders, week1_qb,
# league-wide win-ladders) -- still honest and useful (explains HOW the
# number was derived) without duplicating every scoring function's full
# internals into this endpoint too. See app/models/awards.py,
# division_markets.py, and stat_leaders.py for the real implementations.
_FALLBACK_METHODOLOGY = {
    "mvp": "Composite score: team's season-sim best-record probability x the candidate's career EPA/play rate (QB/RB), normalized across all currently-tracked MVP candidates.",
    "opoy": "Same composite as MVP, pooled across QB/RB/WR/TE career EPA rates.",
    "dpoy": "Same composite shape as MVP/OPOY, but player_quality comes from a weighted counting-stat score (sacks/INTs/forced fumbles/TFLs) since no clean EPA-style rate exists for defense.",
    "coach_of_year": "Team's projected win total (season Monte Carlo) minus last season's actual win total, normalized across all currently-tracked candidates -- rewards the biggest year-over-year improvement.",
    "division_wins": "Read directly from the season Monte Carlo's per-division combined-win-count tally (2,000 trials).",
    "division_order": "Read directly from the season Monte Carlo's per-division finishing-order tally (2,000 trials).",
    "div_least_wins": "Ranks divisions by the season Monte Carlo's simulated total wins, normalized across all four divisions in the conference.",
    "div_most_wins": "Ranks divisions by the season Monte Carlo's simulated total wins, normalized across all four divisions in the conference.",
    "worst_to_first": "Season Monte Carlo: probability that a team finishing last in its division last season wins the division this season, any team.",
    "h2h_wins": "Season Monte Carlo: probability team A out-wins team B across the full simulated season (not just their head-to-head game).",
    "week1_qb": "Depth-chart-based: 85% if this candidate is the team's current listed QB1, 8% if QB2, 1% otherwise -- a flat bucket estimate, not a fitted probability (no historical depth-chart-rank-to-Week-1-starter relationship was available to fit one from).",
    "wins_any": "League-wide version of the win-total ladder: probability that AT LEAST ONE of the 32 teams reaches this win threshold, read from the season Monte Carlo.",
    "undefeated_season": "League-wide: probability that ANY team finishes the regular season undefeated, read from the season Monte Carlo.",
}
for _lt in ("leader_pass_yds", "leader_pass_tds", "leader_pass_int", "leader_rush_yds", "leader_rush_tds", "leader_rec_yds", "leader_rec_tds"):
    _FALLBACK_METHODOLOGY[_lt] = "Ranks all tracked candidates by career counting-stat totals (nflverse PBP-derived), converted to a probability via normalized ranking -- not a fitted statistical model."
for _lt in ("leader_def_int", "leader_sacks"):
    _FALLBACK_METHODOLOGY[_lt] = "Ranks all tracked defensive candidates by career counting-stat totals, converted to a probability via normalized ranking."
for _lt in ("team_pts_most", "team_pts_least", "team_dpts_most", "team_dpts_least"):
    _FALLBACK_METHODOLOGY[_lt] = "Ranks all 32 teams by current trailing points-scored/allowed rate, converted to a probability via linear distance-from-the-extreme -- an honestly-rough heuristic, not a full points Monte Carlo."


_WEIGHT_SCORE = {"major": 3, "moderate": 2, "minor": 1}


def _seeded_choice(seed, options: list[str]) -> str:
    """Deterministically pick one phrasing from `options` using a stable hash
    of `seed`. Same seed -> same choice every time (cache-safe, and a given
    market always reads identically across refreshes), but different markets
    land on different wordings. This is how the reasoning gets variety WITHOUT
    an LLM (the project's standing rule-based constraint) -- it's still
    template text, just not the SAME template every time. Uses md5 rather than
    the builtin hash() because hash() is salted per-process (would reshuffle
    every restart)."""
    if not options:
        return ""
    h = int(hashlib.md5(str(seed).encode()).hexdigest(), 16)
    return options[h % len(options)]


def _edge_sentence(model_prob: float | None, market_prob: float | None) -> str:
    """Shared closing sentence for every market-type branch below -- plain-
    English read on the size of the model/market gap plus the standing
    honesty caveat, rule-based (no LLM -- this project's standing
    constraint, see project memory). Keyed off the actual numbers, and
    varied across a few equivalent phrasings (seeded on the two probs so it's
    stable per market) so every bet in the app doesn't close on the exact
    same sentence."""
    if model_prob is None or market_prob is None:
        return _seeded_choice(model_prob, [
            "There's no market price to compare the model against here yet.",
            "No market price has landed for this one yet, so there's no gap to read.",
            "Nothing to compare against yet -- the market hasn't priced this specific bet.",
        ])
    seed = f"{model_prob:.4f}|{market_prob:.4f}"
    m, mk = model_prob * 100, market_prob * 100
    diff = model_prob - market_prob
    if abs(diff) < 0.02:
        return _seeded_choice(seed, [
            f"Model and market land in basically the same place ({m:.0f}% vs {mk:.0f}%) -- no real disagreement to act on.",
            f"The model ({m:.0f}%) and the market ({mk:.0f}%) are within a hair of each other, so this isn't a spot it's flagging.",
            f"There's almost nothing between the model's {m:.0f}% and the market's {mk:.0f}% here -- effectively agreement.",
        ])
    size = "sizable" if abs(diff) >= 0.15 else "modest"
    caveat = _seeded_choice(seed, [
        "None of these models is proven to beat the market, though, so read it as a prompt to look closer, not a green light.",
        "That said, nothing here has beaten the closing line in backtests -- treat it as a lead to dig into, not a settled edge.",
        "Worth remembering the models have never actually out-predicted the market in testing, so it's a nudge to investigate rather than a signal.",
    ])
    if diff > 0:
        opener = _seeded_choice(seed, [
            f"Put it together and the model lands at {m:.0f}% where the market has {mk:.0f}% -- a {size} gap in this side's favor.",
            f"Weigh it all up and the model sits at {m:.0f}% against the market's {mk:.0f}%, a {size} lean toward this bet.",
            f"That all shakes out to {m:.0f}% for the model versus {mk:.0f}% on the market -- a {size} edge pointed at this side.",
        ])
    else:
        opener = _seeded_choice(seed, [
            f"The model actually lands below the market here ({m:.0f}% vs {mk:.0f}%) -- a {size} gap the other way, so if anything it leans against this side.",
            f"Model comes in under the market ({m:.0f}% against {mk:.0f}%), a {size} lean away from this bet.",
            f"The model's {m:.0f}% sits below the market's {mk:.0f}% -- a {size} gap, tilting against this side rather than for it.",
        ])
    return f"{opener} {caveat}"


def _moneyline_insight(
    home_team: str, away_team: str, home_r: float | None, away_r: float | None,
    hfa: float, is_neutral: bool, factors_raw: list[dict], model_prob: float | None, market_prob: float | None,
) -> str:
    seed = f"{home_team}|{away_team}|{home_r}|{away_r}"

    # situational picture first -- it shapes how the story develops
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
                f"From there, the situational picture -- injuries, rest, travel, weather -- tilts {sit_lean}'s way, led by {top_name}{rat}.",
                f"On top of that, the situational side leans {sit_lean}, with {top_name}{rat} the driver.",
                f"The off-field factors then nudge {sit_lean}'s way, most notably {top_name}{rat}.",
                f"Layer in the situational read and it favors {sit_lean} a touch, headlined by {top_name}{rat}.",
            ])
        if sit_wash:
            return _seeded_choice(seed + "s", [
                f"The situational layer -- injuries, rest, weather -- nets out about even; {top_name} is worth a glance but doesn't really move it.",
                f"Off the field it's roughly a wash, {top_name} aside, so nothing there shifts the read much.",
            ])
        return _seeded_choice(seed + "s", [
            "There's nothing on the situational sheet yet -- no injury, rest, or weather flags for this one -- so the rating carries the whole read.",
            "No injury, rest, or weather notes are on file, so this comes down to the ratings alone.",
        ])

    story = ""
    if home_r is not None and away_r is not None:
        gap = home_r - away_r
        hfa_note = ("and on a neutral field home advantage is off the table" if is_neutral
                    else f"with {home_team} adding a {hfa:.0f}-point home-field bump on top")
        stronger, s_r, weaker, w_r = (home_team, home_r, away_team, away_r) if gap > 0 else (away_team, away_r, home_team, home_r)
        if abs(gap) < 25:
            setup = _seeded_choice(seed, [
                f"This one projects tight -- Elo has {home_team} and {away_team} nearly level ({home_r:.0f} to {away_r:.0f}), {hfa_note}.",
                f"There's little between these two on the ratings ({home_r:.0f} to {away_r:.0f}), {hfa_note}.",
                f"About as even as the ratings get: {home_team} and {away_team} sit close ({home_r:.0f} to {away_r:.0f}), {hfa_note}.",
                f"Elo can barely split them ({home_r:.0f} to {away_r:.0f}), {hfa_note}.",
            ])
        elif abs(gap) >= 45:
            setup = _seeded_choice(seed, [
                f"This is {stronger}'s game to lose on paper -- Elo has them a clear {abs(gap):.0f} points over {weaker} ({s_r:.0f} to {w_r:.0f}), {hfa_note}.",
                f"{stronger} is the decided side here, sitting a full {abs(gap):.0f} Elo points above {weaker} ({s_r:.0f} to {w_r:.0f}), {hfa_note}.",
                f"The ratings make {stronger} the class of this one, {abs(gap):.0f} clear of {weaker} ({s_r:.0f} to {w_r:.0f}), {hfa_note}.",
            ])
        else:
            setup = _seeded_choice(seed, [
                f"{stronger} comes in as the side Elo prefers, {abs(gap):.0f} points up on {weaker} ({s_r:.0f} to {w_r:.0f}), {hfa_note}.",
                f"The lean is {stronger}, who rate {abs(gap):.0f} points ahead of {weaker} ({s_r:.0f} to {w_r:.0f}), {hfa_note}.",
                f"Elo gives the edge to {stronger}, {abs(gap):.0f} to the good over {weaker} ({s_r:.0f} to {w_r:.0f}), {hfa_note}.",
            ])
        story = f"{setup} {situational()}"
    else:
        story = situational()

    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _margin_insight(home_team: str, away_team: str, home_r: float | None, away_r: float | None, is_divisional: bool, model_prob: float | None, market_prob: float | None) -> str:
    if home_r is not None and away_r is not None:
        gap = home_r - away_r
        seed = f"{home_team}|{away_team}|{home_r}|{away_r}|m"
        stronger, s_r, weaker, w_r = (home_team, home_r, away_team, away_r) if gap >= 0 else (away_team, away_r, home_team, home_r)
        base = _seeded_choice(seed, [
            f"The number's fair value comes mostly from the {abs(gap):.0f}-point Elo edge {stronger} holds over {weaker}",
            f"Most of this line traces back to the {abs(gap):.0f}-point rating gap between {stronger} and {weaker}",
            f"What sets the spread here is the {abs(gap):.0f}-Elo cushion {stronger} carries on {weaker}",
        ])
        if is_divisional:
            story = base + _seeded_choice(seed, [
                ", but it's a divisional game, and those tend to play tighter than the raw gap suggests, so the model reels the number back toward the middle.",
                " -- though division rivals usually play closer than the ratings imply, so the model trims the margin toward even.",
            ])
        else:
            story = base + "."
    elif is_divisional:
        story = "It's a divisional game, and those tend to play tighter than the ratings alone imply, so the model pulls the number toward the middle."
    else:
        story = ""
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _total_insight(is_divisional: bool, is_dome: bool, model_prob: float | None, market_prob: float | None) -> str:
    seed = f"{is_divisional}|{is_dome}|{model_prob}|{market_prob}|t"
    lead = _seeded_choice(seed, [
        "This one's built off each team's own recent scoring pace rather than a single strength number.",
        "The total leans on how the two sides have actually been scoring lately, not on one overall rating.",
        "Here the model works from each team's recent scoring rate rather than a head-to-head strength read.",
    ])
    if is_divisional and is_dome:
        env = " Two environmental tugs pull opposite ways: divisional games historically run about 1.5 points lower, a dome about 3.5 higher -- both fold in."
    elif is_divisional:
        env = " Divisional matchups have historically gone about 1.5 points lower-scoring than the same teams would elsewhere, and that's baked in."
    elif is_dome:
        env = " Indoors, games tend to run about 3.5 points higher than outside, which nudges the number up."
    else:
        env = ""
    return f"{lead}{env} {_edge_sentence(model_prob, market_prob)}".strip()


def _futures_sim_insight(team: str, rating: float | None, team_sim: dict, model_prob: float | None, market_prob: float | None) -> str:
    if rating is not None:
        playoff_pct = team_sim.get("playoff_pct")
        sb_pct = team_sim.get("sb_champ_pct")
        seed = f"{team}|{rating}|nflfut"
        if playoff_pct is not None and sb_pct is not None:
            story = _seeded_choice(seed, [
                f"This comes out of a full season simulation anchored on {team}'s current Elo ({rating:.0f}) -- run it forward and they land around a {playoff_pct * 100:.0f}% chance at the playoffs and {sb_pct * 100:.0f}% at the Super Bowl overall.",
                f"{team}'s rating ({rating:.0f}) is carried through a rest-of-season sim to get here, which also works out to roughly {playoff_pct * 100:.0f}% to reach the playoffs and {sb_pct * 100:.0f}% to win it all.",
                f"Behind this number is a season-long simulation off {team}'s Elo ({rating:.0f}); the same run gives them about a {playoff_pct * 100:.0f}% playoff shot and {sb_pct * 100:.0f}% at the Lombardi.",
            ])
        else:
            story = _seeded_choice(seed, [
                f"This is a season simulation built off {team}'s current Elo ({rating:.0f}), carried across the rest of the schedule.",
                f"{team}'s rating ({rating:.0f}) drives this through a rest-of-season simulation.",
            ])
    else:
        story = ""
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


_STAGE_PHRASE = {
    "reg": "missing the playoffs entirely",
    "wc": "bowing out in the Wild Card round",
    "div": "losing in the Divisional round",
    "conf": "losing the Conference Championship",
    "sb_loss": "reaching the Super Bowl but losing it",
    "sb_win": "winning the Super Bowl",
}


def _stage_of_elim_insight(team: str, stage: str | None, rating: float | None, model_prob: float | None, market_prob: float | None) -> str:
    phrase = _STAGE_PHRASE.get(stage, "exiting at this stage")
    rt = f" (current Elo {rating:.0f})" if rating is not None else ""
    story = _seeded_choice(f"{team}|{stage}|soe", [
        f"This one reads straight off the season Monte Carlo -- the same bracket sim that prices {team}'s{rt} division and Super Bowl futures, just at a finer grain. It's the odds of {team} {phrase}, one of six mutually-exclusive exit stages that together sum to 100%.",
        f"{team}'s{rt} exit-stage number comes from the full-season simulation, tracking which round they bow out in across every trial. This line is specifically the chance of {team} {phrase} -- one rung of a six-way split that adds up to the whole.",
        f"Priced from the season bracket sim: it runs {team}'s{rt} rest-of-season a few thousand times and tallies where they get knocked out each time. Here that's the share of runs ending in {team} {phrase}.",
    ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _season_stat_insight(player_name: str, threshold: float | None, prior_entry: dict | None, mean: float | None, std: float | None, model_prob: float | None, market_prob: float | None) -> str:
    if prior_entry is None:
        return f"No qualifying prior-season data was found for {player_name} (needs at least 8 games played last season), so this projection isn't well-grounded here -- treat the model number cautiously."
    sentences = []
    if mean is not None and threshold is not None:
        pace_word = "well above" if mean > threshold * 1.15 else ("well below" if mean < threshold * 0.85 else "close to")
        sentences.append(
            f"{player_name} averaged {prior_entry['rate']:.1f}/game last season over {prior_entry['games']} games, which "
            f"projects to {mean:.0f} for a full season -- {pace_word} this market's {threshold:.0f} threshold."
        )
    if std is not None and mean is not None and mean > 0 and std > mean * 0.5:
        sentences.append(f"This category is historically noisy for most players (projected std of {std:.0f} against a mean of {mean:.0f}), so the real range of outcomes is wide.")
    sentences.append(_edge_sentence(model_prob, market_prob))
    return " ".join(sentences)


_AWARD_LABEL = {"mvp": "MVP", "coach_of_year": "Coach of the Year", "opoy": "Offensive Player of the Year", "dpoy": "Defensive Player of the Year"}
_AWARD_METRIC = {
    "mvp": "how good their team is projected to be (from the season sim) times the candidate's own career production, EPA per play for a QB or RB",
    "opoy": "the same team-strength-times-production blend as MVP, pooled across QB/RB/WR/TE career EPA rates",
    "dpoy": "team strength times a defensive counting-stat score (sacks, INTs, forced fumbles, TFLs), since defense has no clean EPA-style rate",
}
_LEADER_STAT = {
    "leader_pass_yds": "passing yards", "leader_pass_tds": "passing touchdowns", "leader_pass_int": "interceptions thrown",
    "leader_rush_yds": "rushing yards", "leader_rush_tds": "rushing touchdowns",
    "leader_rec_yds": "receiving yards", "leader_rec_tds": "receiving touchdowns",
    "leader_def_int": "interceptions", "leader_sacks": "sacks",
}


def _award_insight(market_type: str, name: str, model_prob: float | None, market_prob: float | None) -> str:
    award = _AWARD_LABEL.get(market_type, "this award")
    seed = f"{name}|{market_type}|award"
    if market_type == "coach_of_year":
        body = _seeded_choice(seed, [
            f"{name}'s Coach-of-the-Year number is really a story about improvement: the model takes the team's projected win total from the season sim, subtracts last year's actual wins, and rewards the biggest jump -- then weighs that against every other candidate.",
            f"This rides on how much better {name}'s team is projected to be than last season -- the award tends to follow the biggest year-over-year turnaround, so that gap (season-sim wins minus last year's) drives the number, scaled against the field.",
        ])
    else:
        metric = _AWARD_METRIC.get(market_type, "team strength and player production")
        body = _seeded_choice(seed, [
            f"The {award} read for {name} is a composite of {metric}, normalized so all the tracked candidates' odds line up sensibly.",
            f"{name}'s {award} price blends {metric}, then squares it against every other name on the board.",
            f"For {award}, the model scores {name} on {metric} and ranks that against the rest of the field.",
        ])
    return f"{body} {_edge_sentence(model_prob, market_prob)}"


def _leader_insight(market_type: str, name: str, model_prob: float | None, market_prob: float | None) -> str:
    stat = _LEADER_STAT.get(market_type, "this category")
    seed = f"{name}|{market_type}|leader"
    body = _seeded_choice(seed, [
        f"This is {name}'s shot at leading the league in {stat}. It's a ranking rather than a fitted model -- the app lines every tracked candidate up by career {stat} totals and turns that order into a probability.",
        f"{name} pacing the league in {stat} comes from a simple ranking: sort the candidates by career {stat} and convert the standings into odds, with no statistical projection underneath.",
        f"For the {stat} lead, the model ranks {name} against the field on career {stat} totals and reads the number straight off that order.",
    ])
    return f"{body} {_edge_sentence(model_prob, market_prob)}"


def _division_futures_insight(market_type: str, target: str | None, model_prob: float | None, market_prob: float | None) -> str:
    t = target or "this group"
    seed = f"{target}|{market_type}|divf"
    options = {
        "division_wins": [
            f"This reads straight off the season simulation -- run the year out thousands of times and count how often {t}'s four teams combine for a win total in this range.",
            f"The number comes from the season Monte Carlo, tallying {t}'s combined division wins across every simulated season.",
        ],
        "division_order": [
            f"This is the exact order of finish for {t}: the season sim sorts the division in each of thousands of simulated years and counts how often it lands in precisely this 1-2-3-4.",
            f"Priced from the season Monte Carlo -- how often {t} finishes in exactly this order across thousands of runs.",
        ],
        "div_most_wins": [
            f"This asks which division piles up the most wins -- the season sim adds up each one's total across thousands of runs and ranks them, with {t} the pick here.",
            f"From the season Monte Carlo: {t}'s share of simulated seasons where it's the winningest division in the conference.",
        ],
        "div_least_wins": [
            f"The flip side of most-wins -- the season sim ranks divisions by fewest combined wins across thousands of runs, and this is {t}'s share of them.",
            f"Priced off the season Monte Carlo: how often {t} comes out the weakest division by total wins.",
        ],
        "worst_to_first": [
            "This is the classic worst-to-first -- the chance some team that finished last in its division a year ago wins it this season, read off the season simulation.",
            "From the season Monte Carlo: how often a prior-year division cellar-dweller takes the crown this season.",
        ],
        "h2h_wins": [
            f"This pits two teams over the whole season, not just their head-to-head game -- the sim counts how often {t} finishes with more wins than the other across thousands of simulated years.",
            f"Priced from the season Monte Carlo: {t}'s share of simulated seasons ending with the better record of the pair.",
        ],
    }.get(market_type)
    body = _seeded_choice(seed, options) if options else ""
    return f"{body} {_edge_sentence(model_prob, market_prob)}".strip()


@router.get("/{market_id}/reasoning", response_model=ReasoningOut)
def get_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    """Click-through 'why this bet' breakdown -- model_prob/market_prob are
    passed in by the frontend (it already has them from the list endpoints
    this row came from) rather than recomputed here, so this endpoint's job
    is purely to explain HOW that number was derived and surface whatever
    underlying facts are cheaply available, not to be a second source of
    truth for the probability itself."""
    m = session.get(Market, market_id)
    if m is None:
        raise HTTPException(404, "market not found")
    game = session.get(NflGame, m.nfl_game_id) if m.nfl_game_id else None
    label = f"{game.away_team} @ {game.home_team}" if game else (m.group_label or m.market_type)
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    caveats = ["model_validated: false -- no model in this app has been shown to beat the market in backtesting."]
    methodology = _FALLBACK_METHODOLOGY.get(m.market_type, "No detailed methodology available for this market type yet.")
    insight = ""

    if m.market_type == "moneyline" and game is not None:
        home_r = elo_service.get_team_rating(game.home_team)
        away_r = elo_service.get_team_rating(game.away_team)
        methodology = (
            "Elo rating model, blended with a free rule-based news/situational layer (injuries, "
            "rest, weather, playoff motivation, etc.) when a factor is on file. Walk-forward "
            "backtested against 10 seasons of historical closing lines and did NOT beat the "
            "market's own pricing on NFL moneylines."
        )
        if home_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} Elo rating", detail=f"{home_r:.0f}"))
        if away_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} Elo rating", detail=f"{away_r:.0f}"))
        hfa = effective_home_field_adv(game.home_team, game.location)
        is_neutral = game.location == "Neutral"
        hfa_detail = f"{hfa:.0f} Elo points"
        if is_neutral:
            hfa_detail += " (neutral site -- zeroed out)"
        factors.append(ReasoningFactorOut(label="Home-field advantage applied", detail=hfa_detail))
        news_cache = get_news_adjustment_cache(session, game.id)
        factors_raw: list[dict] = []
        if news_cache:
            import json as _json

            factors_raw = _json.loads(news_cache.factors_json)
            for f in factors_raw:
                # Factor schema: {factor, direction: favor_home|favor_away|neutral,
                # weight: minor|moderate|major, rationale} -- see
                # app/models/news_adjustment/schema.py::Factor.
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
        insight = _moneyline_insight(game.home_team, game.away_team, home_r, away_r, hfa, is_neutral, factors_raw, model_prob, market_prob)

    elif m.market_type in ("spread", "team_total", "spread_1h", "spread_2h") and game is not None:
        methodology = (
            "Margin-space probability model: Normal-distribution approximation, mean derived from "
            "the Elo rating difference (margin ~= 0.0415 x elo_diff, residual std 13.52 pts from "
            "6,967 real games 2012-2025), blended with the same news/situational layer as moneyline "
            "via an inverted-Elo-diff bridge."
        )
        if m.market_type in ("spread_1h", "spread_2h"):
            methodology += " Half-scoring split derived from real halftime PBP splits (game_half column, 2012-2025)."
        home_r = elo_service.get_team_rating(game.home_team)
        away_r = elo_service.get_team_rating(game.away_team)
        if home_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.home_team} Elo rating", detail=f"{home_r:.0f}"))
        if away_r is not None:
            factors.append(ReasoningFactorOut(label=f"{game.away_team} Elo rating", detail=f"{away_r:.0f}"))
        if game.div_game:
            factors.append(ReasoningFactorOut(label="Divisional game", detail="probability squeezed 10% toward 50/50"))
        insight = _margin_insight(game.home_team, game.away_team, home_r, away_r, bool(game.div_game), model_prob, market_prob)

    elif m.market_type in ("total", "total_1h", "total_2h") and game is not None:
        methodology = (
            "Points-total probability model: Normal-distribution approximation over each team's "
            "trailing scoring rate, with structural shifts for weather, divisional games, and "
            "dome/turf, plus a small EPA-mismatch signal (backtest-confirmed real but modest, "
            "r=0.104)."
        )
        if m.market_type in ("total_1h", "total_2h"):
            methodology += " Half-scoring split derived from real halftime PBP splits (game_half column, 2012-2025)."
        is_dome = game.roof in ("dome", "closed")
        if game.div_game:
            factors.append(ReasoningFactorOut(label="Divisional game", detail="total suppressed ~1.5pts (real games average 1.5 fewer combined points)"))
        if is_dome:
            factors.append(ReasoningFactorOut(label="Dome/closed roof", detail="total boosted ~1.75pts (real games average 3.5 more combined points in domes)"))
        insight = _total_insight(bool(game.div_game), is_dome, model_prob, market_prob)

    elif game is None and m.team and m.market_type in FUTURES_SIM_KEY:
        methodology = (
            "Season Monte Carlo simulation (2,000 trials) using current Elo ratings and the real "
            "remaining schedule, with a full 7-team-per-conference playoff bracket and correct "
            "reseeding after each round."
        )
        rating = elo_service.get_team_rating(m.team)
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} current Elo rating", detail=f"{rating:.0f}"))
        sim_results = season_sim_service.get_results()
        team_sim = sim_results.get(m.team) or {}
        for sim_key, sim_label in [
            ("division_pct", "Division win probability"),
            ("conf_champ_pct", "Conference championship probability"),
            ("playoff_pct", "Playoff probability"),
            ("sb_champ_pct", "Super Bowl probability"),
            ("best_record_pct", "Best regular-season record probability"),
        ]:
            if sim_key in team_sim and sim_key != FUTURES_SIM_KEY.get(m.market_type):
                factors.append(ReasoningFactorOut(label=sim_label, detail=f"{team_sim[sim_key] * 100:.1f}%"))
        caveats.append("Playoff seeding uses simplified tiebreakers (win total, then head-to-head, then random), not the NFL's real tiebreaker rules.")
        insight = _futures_sim_insight(m.team, rating, team_sim, model_prob, market_prob)

    elif m.market_type == "stage_of_elimination" and m.team:
        methodology = (
            "Season Monte Carlo (same 7-team-per-conference bracket with reseeding used for the other "
            "futures) read at the round level: each trial records the exact round this team is eliminated "
            "in, giving six mutually-exclusive stages (miss playoffs / wild card / divisional / conference "
            "/ Super Bowl loss / Super Bowl win) that sum to 100%."
        )
        rating = elo_service.get_team_rating(m.team)
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} current Elo rating", detail=f"{rating:.0f}"))
        sim_results = season_sim_service.get_results()
        stage_pct = (sim_results.get(m.team) or {}).get("stage_exit_pct") or {}
        for sk, slabel in [("reg", "Miss playoffs"), ("wc", "Out in Wild Card"), ("div", "Out in Divisional"),
                           ("conf", "Lose Conf. Championship"), ("sb_loss", "Lose Super Bowl"), ("sb_win", "Win Super Bowl")]:
            if sk in stage_pct:
                factors.append(ReasoningFactorOut(label=slabel, detail=f"{stage_pct[sk] * 100:.1f}%"))
        caveats.append("Playoff seeding uses simplified tiebreakers (win total, then head-to-head, then random), not the NFL's real tiebreaker rules.")
        insight = _stage_of_elim_insight(m.team, m.side, rating, model_prob, market_prob)

    elif m.market_type in WIN_LADDER_FUTURES_TYPES and m.team:
        methodology = _FALLBACK_METHODOLOGY.get(m.market_type) or "Read directly from the season Monte Carlo's per-team win-count histogram (2,000 trials)."
        rating = elo_service.get_team_rating(m.team)
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} current Elo rating", detail=f"{rating:.0f}"))
        insight = _futures_sim_insight(m.team, rating, {}, model_prob, market_prob)

    elif m.market_type in SEASON_STAT_FUTURES_TYPES and m.group_label and m.line is not None:
        category = SEASON_STAT_CATEGORY_BY_TYPE[m.market_type]
        key = qb_canonical_key(m.group_label)
        prior_entry = get_prior_season_stats().get(category, {}).get(key)
        methodology = (
            "Season-total projection: last season's own per-game rate x games played (a TD-category "
            "regression-to-mean correction is applied where real data showed one), projected with a "
            "Normal-distribution std derived from real year-over-year variance (n=325-2067 "
            "player-seasons, 2012-2025)."
        )
        mean = std = None
        if prior_entry:
            factors.append(ReasoningFactorOut(label="Last season's rate", detail=f"{prior_entry['rate']:.1f}/game over {prior_entry['games']} games ({prior_entry['total']:.0f} total)"))
            mean, std = project_season_total(category, prior_entry, None)
            factors.append(ReasoningFactorOut(label="Projected season total", detail=f"{mean:.0f} (std {std:.0f})"))
            caveats.append("This mean/std does not include the role-continuity discount (traded/benched/retired check) applied to the live displayed probability for QB/RB -- shown here for transparency, may differ slightly from the number above.")
        else:
            factors.append(ReasoningFactorOut(label="Last season's rate", detail="no qualifying prior-season data found for this candidate (< 8 games played, or not in the cached PBP)"))
        insight = _season_stat_insight(m.group_label, m.line, prior_entry, mean, std, model_prob, market_prob)

    elif m.market_type in AWARD_FUTURES_TYPES and m.group_label:
        insight = _award_insight(m.market_type, m.group_label, model_prob, market_prob)

    elif m.market_type in LEADER_FUTURES_TYPES and m.group_label:
        insight = _leader_insight(m.market_type, m.group_label, model_prob, market_prob)

    elif m.market_type in DIVISION_EXTRA_TYPES:
        insight = _division_futures_insight(m.market_type, m.team, model_prob, market_prob)

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

@router.get("/futures-history/{market_id}")
def futures_history(market_id: int, session: Session = Depends(get_session)):
    """How the MODEL and the MARKET moved on one futures leg, over time.

    Two series from two places on purpose. The market price comes from
    MarketSnapshot, which the pollers have always written; the model probability
    comes from FuturesProbHistory, sampled hourly, because model_prob is computed
    on the read path and was otherwise discarded (see models/futures_history.py).

    A futures position settles months out, so the only thing that happens in the
    meantime is that opinion moves -- being able to see whether the MODEL moved
    or only the market did is the whole point of the chart.
    """
    from app.db.models import FuturesProbHistory, MarketSnapshot

    market = session.get(Market, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    # Hourly buckets: futures move on news, not minute to minute, and the raw
    # snapshot stream is one row every few minutes for weeks.
    seen: set[str] = set()
    market_series = []
    for snap in (
        session.query(MarketSnapshot)
        .filter(MarketSnapshot.market_id == market_id)
        .order_by(MarketSnapshot.ts)
        .all()
    ):
        bucket = snap.ts.strftime("%Y-%m-%dT%H")
        if bucket in seen:
            continue
        seen.add(bucket)
        price = _implied_prob(snap)
        if price is not None:
            market_series.append({"ts": snap.ts.isoformat() + "Z", "prob": price})

    model_series = [
        {"ts": row.ts.isoformat() + "Z", "prob": row.model_prob}
        for row in session.query(FuturesProbHistory)
        .filter(FuturesProbHistory.market_id == market_id,
                FuturesProbHistory.model_prob.isnot(None))
        .order_by(FuturesProbHistory.ts)
        .all()
    ]
    return {
        "market_id": market_id,
        "team": market.team,
        "group_label": market.group_label,
        "market": market_series,
        "model": model_series,
    }
