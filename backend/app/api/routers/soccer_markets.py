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
from app.clients import kalshi_soccer_client, polymarket_soccer_client
from app.clients.football_data_client import PROMOTION_SOURCE_DIVISION
from app.db.database import get_session
from app.db.models import Market, MarketSnapshot, PlacedBet, SoccerMatch, SoccerNewsAdjustmentCache
from app.ingestion import soccer_data
from app.ingestion.market_catalog_soccer import get_soccer_news_adjustment_cache, soccer_news_cache_to_pydantic
from app.ingestion.market_matcher_soccer import canonical_team_key, team_names_match
from app.api.cross_key import soccer_game_cross_key
from app.models.baseline.soccer_pool_resolver import resolve_to_pool
from app.models.baseline import elo_service_soccer
from app.models.cup_match import predict_cup_tie
from app.models.conmebol_match import predict_conmebol_match
from app.models.uefa_match import predict_uefa_match
from app.models.leagues_cup_match import predict_leagues_cup_match
from app.models.national_match import predict_national_match
from app.models.combine import combine_probability_3way
from app.models.ladder_sanity import (
    SOCCER_LIVE_TRADING_MIN_PRICE_SWING,
    SOCCER_LIVE_TRADING_MIN_VOLUME_DELTA,
    find_resolved_entities,
    looks_already_live_by_trading,
)
from app.models.news_adjustment.schema import NewsAdjustment
from app.models import playoff_sim_service_ligamx
from app.models import playoff_sim_service_mls
from app.models.season_sim_soccer import (SeasonSimResult, prob_points_at_least, simulate_season,
                                          current_season_table, season_progress, season_progress_ok,
                                          season_progress_note)
from app.models.staking import apply_duplicate_listing_cap, apply_ladder_futures_cap, FUTURES_MAX_SPREAD, FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

router = APIRouter(prefix="/soccer", tags=["soccer"])

# {"data": {league: SeasonSimResult}} from the most recent futures pass. Populated
# write-only inside list_soccer_futures purely so integrity_checks can assert the
# per-league coherence invariants without re-running the Monte Carlo. Empty until
# that endpoint has been hit once.
_LAST_SIM_BY_LEAGUE: dict = {}

GAME_MARKET_TYPES = {
    "moneyline_3way", "game_spread", "game_total", "btts",
    # Second batch (added 2026-07-19) -- see module docstring.
    "team_total", "ftts", "correct_score",
    "first_half_winner", "first_half_spread", "first_half_total", "first_half_team_total", "first_half_btts",
    "second_half_winner", "second_half_spread", "second_half_total", "second_half_team_total", "second_half_btts",
    # Domestic cups (2026-08-08) -- game-level, not futures.
    "cup_moneyline_3way", "cup_advance", "cup_total", "cup_spread",
    # UEFA club competitions (2026-08-08) -- cross-country, offsets-based.
    "uefa_moneyline_3way", "uefa_total", "uefa_spread",
    "conmebol_moneyline_3way", "conmebol_total", "conmebol_spread",
    "leagues_cup_moneyline_3way", "leagues_cup_total",
    "leagues_cup_spread", "leagues_cup_btts", "leagues_cup_advance",
    "national_moneyline_3way", "national_total",
    "national_spread", "national_btts",
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


# --- DOMESTIC CUPS ---------------------------------------------------------
# A cup tie is stored with the COMPETITION as its league code, so it never
# reaches the per-league pricing above (there is no "COPPA_ITALIA" rating pool,
# by design). Each club's real division is resolved here instead, at pricing
# time, by elo_service_soccer.resolve_league -- which is also what prevents a
# club being priced off a division it left years ago.
CUP_TIERS = {"COPPA_ITALIA": ("I1", "I2"), "DFB_POKAL": ("D1", "D2"),
             "EFL_CUP": ("E0", "E1"),
             "FRA_SUPER_CUP": ("F1", "F2"), "GER_SUPER_CUP": ("D1", "D2")}
CUP_MARKET_TYPES = {"cup_moneyline_3way", "cup_advance", "cup_total", "cup_spread"}

# TRACKING-ONLY: priced and shown, never staked.
#
# cup_advance settles on who PROGRESSED -- extra time, penalties, and for a
# two-legged tie an aggregate across both matches. bet_settlement deliberately
# registers NO grader for it, because none of that is stored. Pricing it while
# leaving it stakeable would produce a bet that can never settle: exactly the
# "pending forever" failure this app already hit with racing bets and with
# cancelled matchups (#84). Ungraded AND unstakeable is coherent; ungraded but
# stakeable is a trap.
#
# Same posture and mechanism as the esports map_winner markets and the player
# stat-projection futures (see PLAYER_STAT_TRACKING_ONLY in routers/markets.py):
# the stake is zeroed AFTER it is computed, so model_prob and edge still surface
# and the row keeps accruing forward evidence.
# leagues_cup_advance was added here 2026-08-26 and REMOVED the same day. The
# reason given was the one the comment above gives for cup_advance -- "no grader
# exists, so a bet on it could never settle" -- and that reason is FALSE for both
# of them. Measured: 35 of cup_advance's 41 bets are already settled, all from
# Kalshi's own market resolution (18 won, 17 lost), and 2 of leagues_cup_advance's
# 5 the same way. app/ingestion/market_resolution_settlement.py grades every
# pending Kalshi bet whose market finalizes, with no market_type filter, so
# nothing about extra time or penalties keeps these unsettleable.
#
# cup_advance is LEFT SUPPRESSED pending a decision, because a second and
# genuinely different concern survives: it settles on progression -- aggregate
# over two legs, extra time, penalties -- while _cup_prediction prices it from a
# 90-minute model. That is a PRICING objection, not a settlement one, and its
# record (18-17) neither confirms nor refutes it. Do not lift it on the strength
# of this comment alone; measure the pricing.
TRACKING_ONLY_MARKET_TYPES = {"cup_advance"}


def _cup_prediction(match: SoccerMatch | None):
    if match is None or match.league not in CUP_TIERS:
        return None
    top, second = CUP_TIERS[match.league]
    states = elo_service_soccer._cache.get("states_by_league") or {}
    if top not in states:
        return None
    resolved = []
    for name in (match.home_team, match.away_team):
        league = elo_service_soccer.resolve_league(name)
        if league not in (top, second):
            return None  # unrated, or in a division this app does not model
        resolved.append((canonical_team_key(name), league))
    (hk, hl), (ak, al) = resolved
    second_teams = {k for k, lg in resolved if lg == second}
    return predict_cup_tie(hk, ak, states[top], states.get(second), second_teams)


# UEFA competitions are stored under their own league code for the same reason
# cups are: a UEFA tie is not a fixture in either club's league. Unlike a cup
# there is no top/second tier -- each club's league is resolved individually and
# the fitted strength offsets do the conversion (models/uefa_match.py).
UEFA_LEAGUES = {"UCL", "UEL", "UECL", "UEFA_SUPER_CUP"}
UEFA_MARKET_TYPES = {"uefa_moneyline_3way", "uefa_total", "uefa_spread"}

# --- CONMEBOL (2026-08-18) -------------------------------------------------
# Its own competition codes and its own predictor. NOT folded into the UEFA
# handlers: conmebol_match.py carries offsets fitted on South American results
# against a BRA1-pinned baseline, and the UEFA path would apply European
# offsets to a Brazilian club.
CONMEBOL_LEAGUES = {"LIBERTADORES", "SUDAMERICANA"}
CONMEBOL_MARKET_TYPES = {"conmebol_moneyline_3way", "conmebol_total", "conmebol_spread"}


def _conmebol_prediction(match: SoccerMatch | None):
    if match is None or (match.league or "") not in CONMEBOL_LEAGUES:
        return None
    states = elo_service_soccer._cache.get("states_by_league") or {}
    resolved = []
    for name in (match.home_team, match.away_team):
        league = elo_service_soccer.resolve_league(name)
        if league is None:
            return None    # Chile/Paraguay/Bolivia/Peru have no pool -- correct refusal
        resolved.append((canonical_team_key(name), league))
    (hk, hl), (ak, al) = resolved
    # predict_conmebol_match refuses a league with no fitted offset rather than
    # pricing it off an assumed average.
    return predict_conmebol_match(hk, hl, ak, al, states)


def _conmebol_moneyline_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _conmebol_prediction(match)
    if pred is None:
        return None
    return {"home": pred.prob_home_win, "draw": pred.prob_draw,
            "away": pred.prob_away_win}.get(market.side, lambda: None)()


def _conmebol_total_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _conmebol_prediction(match)
    if pred is None or market.line is None:
        return None
    return pred.prob_total_over(market.line)


def _conmebol_spread_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """Complement, not mirror, on the away side -- same note as the UEFA one."""
    pred = _conmebol_prediction(match)
    if pred is None or market.line is None:
        return None
    if market.side == "home":
        return pred.distribution.prob_home_spread_cover(-market.line)
    if market.side == "away":
        return 1.0 - pred.distribution.prob_home_spread_cover(market.line)
    return None

# HOW WRONG A STORED KICKOFF MAY BE, by fixture source (2026-08-13).
#
# THE OBSERVED FAILURE. Hearts v Benfica (UEL qualifying) was recommended live:
# stored kickoff 2026-08-13T21:45Z, real kickoff 18:45Z, and ESPN had it at
# "First Half, 42'" while this app still thought it was two hours away. Exactly
# +3h, which is the known Kalshi soccer signature -- occurrence_datetime is the
# market EXPIRATION, not the start (see the soccer occurrence memo).
#
# WHY THE EXISTING PRECEDENCE LADDER DID NOT SAVE IT. That ladder prefers an
# ESPN kickoff over the platform's, but UEFA QUALIFYING has no ESPN row on the
# main uefa.champions / uefa.europa / uefa.europa.conf scoreboards -- all three
# return ZERO events in August. The fixture is therefore created from the Kalshi
# listing alone (source="live"), so there is nothing better to prefer and the
# +3h value is used verbatim.
#
# THIS IS A STOPGAP, NOT THE FIX. It widens the already-started test by the
# known error so a UEFA tie stops being recommended at its REAL kickoff rather
# than three hours later. It costs three hours of pre-match recommending on
# these ties, which is the right side to err on.
#
# THE REAL FIX is to feed the qualifying scoreboards, which DO carry the true
# kickoff and live status: uefa.europa_qual (11 events today, including this
# match) and uefa.europa.conf_qual (25). Then the ladder has a real ESPN time to
# prefer and this constant can go back to zero.
#
# SCOPED TO UEFA, AND THE FIRST ATTEMPT WAS SCOPED WRONG. Keying this on
# SoccerMatch.source ("was it built from a platform listing only?") reads as the
# principled test, and is useless: ALL 583 soccer fixtures carry source="live",
# so it is a constant. That version would have applied the 3h cut to every
# soccer match in the app and silently stopped recommending the entire sport
# three hours before kickoff -- a far bigger change than the bug it fixes.
#
# UEFA is used instead because it is what was actually verified: the main UEFA
# scoreboards return zero events in August, so these are the fixtures with no
# ESPN kickoff to prefer. Domestic leagues do have ESPN rows and are corrected
# by the existing ladder, which is why they are excluded here.
_UEFA_START_UNCERTAINTY = datetime.timedelta(hours=3)


def _start_time_uncertainty(match: SoccerMatch) -> datetime.timedelta:
    """How much EARLIER than the stored value this kickoff might really be."""
    if (match.league or "") in UEFA_LEAGUES:
        return _UEFA_START_UNCERTAINTY
    return datetime.timedelta(0)


def _uefa_prediction(match: SoccerMatch | None):
    if match is None or match.league not in UEFA_LEAGUES:
        return None
    states = elo_service_soccer._cache.get("states_by_league") or {}
    resolved = []
    for name in (match.home_team, match.away_team):
        league = elo_service_soccer.resolve_league(name)
        if league is None:
            return None  # club is not in any league this app rates
        resolved.append((canonical_team_key(name), league))
    (hk, hl), (ak, al) = resolved
    # predict_uefa_match itself refuses a league with no fitted offset, rather
    # than pricing it off an assumed average.
    return predict_uefa_match(hk, hl, ak, al, states)


def _uefa_moneyline_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _uefa_prediction(match)
    if pred is None:
        return None
    return {"home": pred.prob_home_win, "draw": pred.prob_draw,
            "away": pred.prob_away_win}.get(market.side, lambda: None)()


def _uefa_total_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _uefa_prediction(match)
    if pred is None or market.line is None:
        return None
    return pred.prob_total_over(market.line)


def _uefa_spread_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """P(<team> wins by MORE than line), on the single leg.

    THE AWAY CASE IS A COMPLEMENT, NOT A MIRROR, and the symmetric-looking
    version is wrong -- see _leagues_cup_spread_model_prob, which carries the
    same two expressions and the same warning. Kalshi's line is a positive
    "wins by more than X"; prob_home_spread_cover's convention is a negative
    line for the favoured side."""
    pred = _uefa_prediction(match)
    if pred is None or market.line is None:
        return None
    if market.side == "home":
        return pred.distribution.prob_home_spread_cover(-market.line)
    if market.side == "away":
        return 1.0 - pred.distribution.prob_home_spread_cover(market.line)
    return None


# --- LEAGUES CUP (2026-08-08) ----------------------------------------------
# Deliberately NOT folded into the UEFA handlers. predict_leagues_cup_match
# carries its own fitted MLS/Liga MX offset and its own venue term (~0, because
# the competition is played at neutral or near-neutral sites); the UEFA path
# would apply European offsets and a full domestic home advantage instead.
LEAGUES_CUP_LEAGUES = {"LEAGUES_CUP"}
LEAGUES_CUP_MARKET_TYPES = {
    "leagues_cup_moneyline_3way", "leagues_cup_total",
    "leagues_cup_spread", "leagues_cup_btts", "leagues_cup_advance",
}


def _leagues_cup_prediction(match: SoccerMatch | None):
    if match is None or match.league not in LEAGUES_CUP_LEAGUES:
        return None
    states = elo_service_soccer._cache.get("states_by_league") or {}
    resolved = []
    for name in (match.home_team, match.away_team):
        league = elo_service_soccer.resolve_league(name)
        if league is None:
            return None  # club is not in any league this app rates
        resolved.append((canonical_team_key(name), league))
    (hk, hl), (ak, al) = resolved
    # predict_leagues_cup_match refuses anything that is not a genuine
    # MLS-vs-Liga MX pairing, including same-league fixtures -- those are
    # ordinary domestic matches at real venues and must not get the
    # neutral-venue treatment.
    return predict_leagues_cup_match(hk, hl, ak, al, states)


# How far above this competition's REAL scoring a prediction has to sit before
# the drawer says so. 25% is not a fitted constant -- it is a "look at this
# twice" line, chosen because the Miami/Leon case that prompted it was +57%
# while the rest of the slate sat inside +/-20% of the realised mean.
_GOAL_OUTLIER_RATIO = 1.25
_MIN_COMPLETED_FOR_PLAUSIBILITY = 8


def _competition_goal_plausibility(session: Session, match: SoccerMatch, dist):
    """Compare the model's expected goals to what this competition ACTUALLY
    produces, and say so in the drawer.

    WHY. A cross-league model can be correctly fitted and still be untrustworthy
    at a particular point. Miami vs Leon (2026-08-13) priced at 4.39 expected
    goals -- the highest in the competition, against a slate mean of 3.10 and a
    realised mean of 2.80 over completed matches -- and produced a +28.9pp
    moneyline edge and a +34.2pp spread edge that the app staked. Nothing in the
    app contradicted it: `implausible_disagreement` is an odds-RATIO guard (10x)
    and 48.5% -> 77.4% is only 1.6x, so it sails through the mid-price blind
    spot.

    This does NOT gate the bet. It is deliberately informational: the model may
    well be right that a given fixture is a shootout, and silently refusing to
    price the tail of a fitted model is its own error. But the person deciding
    whether to place it should see that the number is the extreme of its
    competition before they do, not after it settles."""
    if dist is None:
        return None
    predicted = dist.expected_home_goals + dist.expected_away_goals
    completed = (
        session.query(SoccerMatch)
        .filter(SoccerMatch.league == match.league,
                SoccerMatch.home_goals_ft.isnot(None),
                SoccerMatch.away_goals_ft.isnot(None))
        .all()
    )
    if len(completed) < _MIN_COMPLETED_FOR_PLAUSIBILITY:
        return None   # no honest base rate yet -- say nothing rather than guess
    totals = [c.home_goals_ft + c.away_goals_ft for c in completed]
    actual = sum(totals) / len(totals)
    if actual <= 0:
        return None
    ratio = predicted / actual
    if ratio >= _GOAL_OUTLIER_RATIO:
        verdict = (f"UNUSUALLY HIGH -- {ratio:.2f}x the realised rate. Treat a large edge here as "
                   "the model being evaluated at the extreme of its range, not as a free bet.")
    elif ratio <= 1 / _GOAL_OUTLIER_RATIO:
        verdict = f"unusually low -- {ratio:.2f}x the realised rate."
    else:
        verdict = "in line with this competition."
    return ReasoningFactorOut(
        label="Goal expectation vs this competition",
        detail=(f"Model expects {predicted:.2f} total goals; {match.league} has actually averaged "
                f"{actual:.2f} over {len(completed)} completed matches. {verdict}"),
    )


# --- NATIONAL TEAMS (2026-08-09) -------------------------------------------
# Fixtures are stored under "INTL", the same code the ratings use. Pricing goes
# through predict_national_match, which refuses any CROSS-CONFEDERATION pairing:
# confederation qualifying is closed, so the six sub-pools were never tied to a
# common scale and Brazil's attack rating came out below Vietnam's. Every ASEAN
# fixture is AFC-vs-AFC so the gate never bites today; it exists for the first
# time Kalshi lists a World Cup match.
NATIONAL_LEAGUES = {"INTL"}
NATIONAL_MARKET_TYPES = {
    "national_moneyline_3way", "national_total", "national_spread", "national_btts",
}


def _national_prediction(match: SoccerMatch | None):
    if match is None or match.league not in NATIONAL_LEAGUES:
        return None
    states = elo_service_soccer._cache.get("states_by_league") or {}
    return predict_national_match(
        canonical_team_key(match.home_team), canonical_team_key(match.away_team),
        states.get("INTL"),
    )


def _national_moneyline_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _national_prediction(match)
    if pred is None:
        return None
    return {"home": pred.prob_home_win, "draw": pred.prob_draw,
            "away": pred.prob_away_win}.get(market.side, lambda: None)()


def _national_total_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _national_prediction(match)
    if pred is None or market.line is None:
        return None
    return pred.prob_total_over(market.line)


def _national_spread_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """Same two expressions as _game_spread_model_prob -- the away side is the
    COMPLEMENT of the mirrored line, not the mirrored line itself."""
    pred = _national_prediction(match)
    if pred is None or market.line is None:
        return None
    if market.side == "home":
        return pred.distribution.prob_home_spread_cover(-market.line)
    if market.side == "away":
        return 1.0 - pred.distribution.prob_home_spread_cover(market.line)
    return None


def _national_btts_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _national_prediction(match)
    if pred is None:
        return None
    return pred.distribution.prob_btts()


def _leagues_cup_moneyline_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _leagues_cup_prediction(match)
    if pred is None:
        return None
    return {"home": pred.prob_home_win, "draw": pred.prob_draw,
            "away": pred.prob_away_win}.get(market.side, lambda: None)()


def _leagues_cup_total_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _leagues_cup_prediction(match)
    if pred is None or market.line is None:
        return None
    return pred.prob_total_over(market.line)


def _leagues_cup_spread_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """P(<team> wins by MORE than line).

    Uses the SAME two expressions as _game_spread_model_prob rather than
    re-deriving them: Kalshi's line is a positive "wins by more than X", while
    prob_home_spread_cover's own convention is a negative line for the favoured
    side, so the home case negates and the away case is the COMPLEMENT of the
    mirrored line. Writing the away case as prob_home_spread_cover(line) without
    the complement looks symmetric and is wrong -- it answers "did the home team
    fail to lose by more than X", which is a different question."""
    pred = _leagues_cup_prediction(match)
    if pred is None or market.line is None:
        return None
    if market.side == "home":
        return pred.distribution.prob_home_spread_cover(-market.line)
    if market.side == "away":
        return 1.0 - pred.distribution.prob_home_spread_cover(market.line)
    return None


def _leagues_cup_advance_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """Single-match advance: win, plus half the draw. Exact for this format --
    the Leagues Cup goes straight to penalties with no extra time."""
    pred = _leagues_cup_prediction(match)
    if pred is None:
        return None
    if market.side == "home":
        return pred.prob_home_advance()
    if market.side == "away":
        return pred.prob_away_advance()
    return None


def _leagues_cup_btts_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _leagues_cup_prediction(match)
    if pred is None:
        return None
    return pred.distribution.prob_btts()


def _cup_moneyline_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _cup_prediction(match)
    if pred is None:
        return None
    # Settles on REGULATION -- Kalshi's own "Reg Time" label. Not P(advance).
    return {"home": pred.prob_home_win, "draw": pred.prob_draw,
            "away": pred.prob_away_win}.get(market.side, lambda: None)()


def _cup_advance_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _cup_prediction(match)
    if pred is None:
        return None
    if market.side == "home":
        return pred.prob_home_advance
    if market.side == "away":
        return pred.prob_away_advance
    return None


def _cup_total_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    pred = _cup_prediction(match)
    if pred is None or market.line is None:
        return None
    return pred.prob_total_over(market.line)  # regulation grid, never extra time


def _cup_spread_model_prob(market: Market, match: SoccerMatch | None) -> float | None:
    """P(<team> wins by MORE than line) in REGULATION.

    Reads `pred.regulation`, not an extra-time grid: CupTiePrediction exposes
    both, and a cup spread settles on 90 minutes exactly like cup_total does.
    Same complement-not-mirror rule as the UEFA and Leagues Cup versions."""
    pred = _cup_prediction(match)
    if pred is None or market.line is None:
        return None
    if market.side == "home":
        return pred.regulation.prob_home_spread_cover(-market.line)
    if market.side == "away":
        return 1.0 - pred.regulation.prob_home_spread_cover(market.line)
    return None


def cup_model_note(match: SoccerMatch | None) -> str | None:
    """The cross-tier caution (see cup_match.CAUTION_NOTE). Returns None for a
    same-tier tie, which needs no conversion and no warning."""
    pred = _cup_prediction(match)
    return pred.caution_note if pred is not None and pred.needs_caution else None


_MODEL_PROB_DISPATCH = {
    "uefa_moneyline_3way": lambda m, match, news: _uefa_moneyline_model_prob(m, match),
    "uefa_total": lambda m, match, news: _uefa_total_model_prob(m, match),
    "uefa_spread": lambda m, match, news: _uefa_spread_model_prob(m, match),
    "conmebol_moneyline_3way": lambda m, match, news: _conmebol_moneyline_model_prob(m, match),
    "conmebol_total": lambda m, match, news: _conmebol_total_model_prob(m, match),
    "conmebol_spread": lambda m, match, news: _conmebol_spread_model_prob(m, match),
    "leagues_cup_moneyline_3way": lambda m, match, news: _leagues_cup_moneyline_model_prob(m, match),
    "leagues_cup_total": lambda m, match, news: _leagues_cup_total_model_prob(m, match),
    "leagues_cup_spread": lambda m, match, news: _leagues_cup_spread_model_prob(m, match),
    "leagues_cup_advance": lambda m, match, news: _leagues_cup_advance_model_prob(m, match),
    "leagues_cup_btts": lambda m, match, news: _leagues_cup_btts_model_prob(m, match),
    "national_moneyline_3way": lambda m, match, news: _national_moneyline_model_prob(m, match),
    "national_total": lambda m, match, news: _national_total_model_prob(m, match),
    "national_spread": lambda m, match, news: _national_spread_model_prob(m, match),
    "national_btts": lambda m, match, news: _national_btts_model_prob(m, match),
    "cup_moneyline_3way": lambda m, match, news: _cup_moneyline_model_prob(m, match),
    "cup_advance": lambda m, match, news: _cup_advance_model_prob(m, match),
    "cup_total": lambda m, match, news: _cup_total_model_prob(m, match),
    "cup_spread": lambda m, match, news: _cup_spread_model_prob(m, match),
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
        return start - _start_time_uncertainty(match) < now_utc

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

    # A HALT ON ONE PLATFORM IS INFORMATION THE OTHER HAS NOT PRICED YET.
    # See cs2_markets.py for the case that produced this: a walkover where
    # Kalshi halted both sides while Polymarket kept quoting a phantom
    # 0.495/0.505, manufacturing a +19.1pp edge on a match nobody would play.
    # The per-market `status == "active"` test below cannot catch it, because
    # the halted rows are not the rows being recommended -- the halt has to be
    # read at the FIXTURE level, across platforms.
    _halted_fixture_ids = {
        _m.soccer_match_id for _m in markets
        if _m.soccer_match_id and (_m.status or "") == "inactive"
    }

    markets = [
        m for m in markets
        if not _match_already_decided(m)
        and not _match_already_started(m)
        and not _match_ladder_resolved(m)
        and not _match_looks_live_by_trading(m)
        and not (m.soccer_match_id and m.soccer_match_id in _halted_fixture_ids)
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
        if match.league in UEFA_LEAGUES:
            # Identical trap to the cup one below: a UEFA tie's league is the
            # COMPETITION, which has no rating pool, so the per-league count
            # would read zero for both clubs and reject every row before
            # _model_prob runs. Resolve each club's real league instead.
            return any(elo_service_soccer.resolve_league(n) is None
                       for n in (match.home_team, match.away_team))
        if match.league in CONMEBOL_LEAGUES:
            # FOURTH instance of the same trap, and this one DID ship for one
            # pass before being caught: "LIBERTADORES"/"SUDAMERICANA" are
            # COMPETITIONS, not rating pools, so the per-league count at the
            # bottom read zero for both clubs and rejected all 208 rows with
            # "no tracked match history" -- while the model itself priced 9 of
            # the 16 fixtures perfectly when called directly. That gap between
            # "the gate says unrated" and "the model says fine" is the tell, and
            # it is the third time this file has produced it.
            #
            # Deferring to the model (rather than re-deriving a condition) is
            # what keeps the gate and the price in agreement -- the same choice
            # the NATIONAL branch below makes, and for the same reason. A gate
            # that disagrees with the model in either direction is a bug: looser
            # moves the refusal one step later, tighter hides prices we have.
            return _conmebol_prediction(match) is None
        if match.league in NATIONAL_LEAGUES:
            # INTL genuinely IS a rating pool, so the count check at the bottom
            # would work -- but it would pass a cross-confederation fixture that
            # predict_national_match then refuses, leaving the row unpriced with
            # a misleading "no tracked match history" reason. Defer to the model
            # so the gate and the price agree.
            return _national_prediction(match) is None
        if match.league in LEAGUES_CUP_LEAGUES:
            # THIRD instance of the same trap (UEFA above, cups below), caught
            # here before it shipped: "LEAGUES_CUP" is a competition, not a
            # rating pool, so the per-league count at the bottom reads zero for
            # both clubs and rejects all 420 rows before _model_prob runs. The
            # tell is that every row comes back with "no tracked match history"
            # while the clubs themselves resolve perfectly -- which is exactly
            # what this returned on the first end-to-end run.
            #
            # Requiring MLS/MEX1 specifically (not merely "some league") also
            # makes the gate agree with predict_leagues_cup_match, which refuses
            # anything outside that pair. A gate looser than the model would
            # just move the refusal one step later.
            return any(
                elo_service_soccer.resolve_league(name) not in ("MLS", "MEX1")
                for name in (match.home_team, match.away_team)
            )
        if match.league in CUP_TIERS:
            # A cup tie's league is the COMPETITION ("COPPA_ITALIA"), which has
            # no rating pool by design, so the per-league count below would read
            # zero for both clubs and short-circuit every cup row to unpriced
            # BEFORE _model_prob ever runs. That is exactly what happened on
            # 2026-08-08: 191 cup rows served unpriced while the same clubs
            # priced fine in a fresh process, because this gate -- not the
            # pricing -- was rejecting them. Each club's real division is
            # resolved instead, by the same function the cup pricing uses.
            return any(
                elo_service_soccer.resolve_league(name) not in CUP_TIERS[match.league]
                for name in (match.home_team, match.away_team)
            )
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
        stake_dollars = size_stake_dollars(staking_mode, kelly, weekly_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, max_spread=FUTURES_MAX_SPREAD, yes_bid=snap.yes_bid if snap else None, yes_ask=snap.yes_ask if snap else None,  sport="soccer")
        if m.market_type in TRACKING_ONLY_MARKET_TYPES:
            kelly = None
            stake_dollars = None
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
                cross_key=soccer_game_cross_key(
                    m.soccer_match_id, m.market_type, m.team, m.line, m.side
                ) or None,
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
                model_note=cup_model_note(match) if m.market_type in CUP_MARKET_TYPES else None,
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
# POLYMARKET'S SIDE OF league_winner resolves by EVENT SLUG, not by label --
# see _futures_division. Its group_label is Polymarket's own event title
# ("Eredivisie: 2026-27 Winner"), which shares no text with Kalshi's
# ("Eredivisie Champion") AND carries the season in it, so a label map would
# have to be re-edited every season. The slug is what we asked for by name, so
# it is exact and stable.
_POLYMARKET_SLUG_TO_DIVISION = {
    slug: division
    for division, slug in polymarket_soccer_client.LEAGUE_WINNER_EVENT_SLUGS.items()
}
# most_clean_sheets resolves the same way, by the slug we asked for by name.
# Without this every row would fall through to the label map, miss, and be
# served unpriced -- the failure mode upsert_polymarket_soccer_league_winner_row
# warns about.
_POLYMARKET_SLUG_TO_DIVISION.update({
    slug: division
    for division, slug in polymarket_soccer_client.MOST_CLEAN_SHEETS_EVENT_SLUGS.items()
})
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

# MLS Cup / conference bracket futures. Deliberately absent from
# _MARKET_TYPE_LABEL_TO_DIVISION above, which is what keeps _futures_division()
# returning None for them -- that is load-bearing, not an oversight. That dict
# feeds the per-league loop that runs simulate_season(), the ROUND-ROBIN model,
# and running it for MLS is precisely the thing season_sim_soccer's docstring
# says is wrong (unbalanced conference schedule). Mapping these to "MLS" to make
# them look tidy would silently route them into the wrong model. They are priced
# from playoff_sim_service_mls instead, below.
_MLS_PLAYOFF_MARKET_TYPES = ("mls_cup_winner", "mls_conference_winner")
# Liga MX torneo champion (KXLIGAMX). Priced by the LIGUILLA bracket, not by
# season_sim_soccer: a torneo is won in the knockout, not on the table, which is
# exactly why this league sat unpriced until playoff_sim_ligamx.py existed.
_LIGAMX_MARKET_TYPE = "ligamx_champion"

MID_SEASON_SIM_NOTE = (
    "Mid-season estimate: this league is already part-way through its season, so the "
    "simulation starts from the current table and plays out only the remaining fixtures. "
    "That path is new and has never been checked against a finished season. It also holds "
    "each team's strength fixed, which makes it more confident than a market that prices in "
    "the chance a side improves or collapses -- expect it to overstate the leader and "
    "understate everyone chasing."
)

MLS_BRACKET_APPROXIMATE_NOTE = (
    "Approximate: this price comes from a playoff bracket simulation that has never been "
    "checked against real results. MLS's current format has only one completed postseason in "
    "this app's data, which is not enough to calibrate against. The simulation is internally "
    "consistent (every team's chances add up correctly), but that only means the arithmetic is "
    "right, not that the numbers are true -- the racing finishing-order model added up "
    "correctly too and was still 30 points off. Treat the edge as a disagreement with the "
    "market, not a proven opportunity."
)

_SOCCER_LEAGUE_NAME = {
    "E0": "Premier League", "SP1": "La Liga", "I1": "Serie A", "D1": "Bundesliga",
    "F1": "Ligue 1", "MLS": "MLS", "P1": "Liga Portugal", "N1": "Eredivisie",
    "E1": "EFL Championship", "SP2": "La Liga 2", "I2": "Serie B", "D2": "2. Bundesliga",
    "F2": "Ligue 2",
    # 2026-08-09. Kept in step with the frontend's own SOCCER_LEAGUE_LABEL --
    # a division missing from either map renders as its raw code ("BRA1"), and
    # this map had fallen 14 divisions and 9 competitions behind.
    "B1": "Belgian Pro League", "T1": "Turkish Super Lig", "G1": "Greek Super League",
    "SC0": "Scottish Premiership", "E2": "EFL League One", "E3": "EFL League Two",
    "BRA1": "Brasileirao", "ARG1": "Liga Profesional", "MEX1": "Liga MX",
    "JPN1": "J1 League", "SWE1": "Allsvenskan", "NOR1": "Eliteserien",
    "DNK1": "Danish Superliga", "CHN1": "Chinese Super League",
    "COPPA_ITALIA": "Coppa Italia", "DFB_POKAL": "DFB Pokal", "EFL_CUP": "EFL Cup",
    "UCL": "Champions League", "UEL": "Europa League", "UECL": "Conference League",
    "UEFA_SUPER_CUP": "UEFA Super Cup", "FRA_SUPER_CUP": "Trophee des Champions",
    "GER_SUPER_CUP": "DFL-Supercup",
    "LEAGUES_CUP": "Leagues Cup",
    "LIBERTADORES": "Copa Libertadores", "SUDAMERICANA": "Copa Sudamericana",
    "INTL": "Internationals",
    # 2026-08-18. KSA1 was ALREADY MISSING before this batch -- it was wired
    # into the series maps on 2026-08-14 and never given a name here or in the
    # frontend, so Saudi rows have been rendering as the raw code "KSA1" since.
    # Exactly the drift the 2026-08-09 note above warns about, caught only by
    # re-running the diff.
    "KSA1": "Saudi Pro League",
    "CHI1": "Primera Division (Chile)", "PAR1": "Primera Division (Paraguay)",
    "BOL1": "Liga Profesional (Bolivia)", "PER1": "Liga 1 (Peru)",
    "COL1": "Primera A (Colombia)", "ECU1": "LigaPro (Ecuador)",
    "URU1": "Primera Division (Uruguay)", "VEN1": "Liga FUTVE (Venezuela)",
    "USL1": "USL Championship", "NWSL": "NWSL",
    "BRA2": "Serie B (Brazil)", "ARG2": "Primera Nacional (Argentina)",
    "E4": "National League", "N2": "Eerste Divisie", "MYS1": "Malaysia Super League",
}

_FUTURES_MARKET_TYPES = ["league_winner", "relegation", "top_half", "top4", "top2", "team_points",
                         # most_clean_sheets (2026-08-27). The sim has computed
                         # most_clean_sheets_prob all along and nothing read it;
                         # this is the wiring, not new modelling.
                         "most_clean_sheets",
                         *_MLS_PLAYOFF_MARKET_TYPES, _LIGAMX_MARKET_TYPE]

# Types whose simulation can legitimately DECLINE to produce a distribution, so
# an all-zero result must be read as "not modelled" rather than "0% for
# everyone". Only most_clean_sheets today: simulate_season skips it on a
# part-played season with unknown starting counts. The rank markets
# (relegation/top_half/top4/top2) always produce a real distribution, so they
# keep the existing 0.0 default and are deliberately not listed.
_SIM_MAY_DECLINE = {"most_clean_sheets"}
_SIM_EMITTED_NOTHING = (
    "No baseline -- the season simulation does not produce a clean-sheet leader "
    "for a part-played season unless the clean sheets played so far are supplied, "
    "and counting only simulated matches would understate every team."
)

_SIM_PROB_FIELD_BY_MARKET_TYPE = {
    "relegation": "relegation_prob",
    "most_clean_sheets": "most_clean_sheets_prob",
    "top_half": "top_half_prob",
    "top4": "top4_prob",
    "top2": "top2_prob",
}


def _futures_division(m) -> str | None:
    """CALLED WITH TWO DIFFERENT TYPES, which is why every access here is a
    getattr: the ORM `Market` while rows are being built, and the pydantic
    `FuturesMarketOut` afterwards (the mid-season-note pass at the end of
    list_soccer_futures). Reading `m.source_event_id` directly 500'd the whole
    route, because the output model has no such field.

    Polymarket league_winner rows carry their league in the event SLUG, which is
    exact and season-stable -- its group_label is Polymarket's own title and
    embeds the season ("Eredivisie: 2026-27 Winner"), so a label map would need
    re-editing every year. The output model already carries the resolved
    division as `league`, so that is preferred once it exists.
    """
    division = getattr(m, "league", None)
    if division:
        return division
    slug = getattr(m, "source_event_id", None)
    if slug:
        division = _POLYMARKET_SLUG_TO_DIVISION.get(slug)
        if division:
            return division
    return _MARKET_TYPE_LABEL_TO_DIVISION.get(
        (getattr(m, "market_type", None), getattr(m, "group_label", None) or ""))


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

    # market.id -> the pool spelling that owns that team's rating, filled in
    # per league below. Falls back to the plain canonical key, so a league
    # whose pool resolution finds nothing behaves exactly as it did before.
    pool_key_by_market_id: dict[int, str] = {}
    by_league: dict[str, list[Market]] = {}
    for m in markets:
        division = _futures_division(m)
        if division:
            by_league.setdefault(division, []).append(m)

    sim_by_league: dict[str, SeasonSimResult | None] = {}
    mid_season_divisions: set[str] = set()
    progress_by_division: dict[str, float] = {}
    # HOISTED OUT OF THE LOOP. This was called per division below, and it parses
    # the whole 122 MB match cache (1.26s) -- 24 divisions meant 30 seconds of
    # identical work per request, holding the GIL throughout and starving every
    # other endpoint. load_matches is memoized now too, so this is belt and
    # braces: the hoist makes the single-read intent explicit at the call site.
    all_matches = soccer_data.load_matches()
    # Bucket ONCE by league rather than letting current_season_table re-scan all
    # 270,349 matches per division -- 24 divisions made that a 6.5M-iteration
    # filter, and it was 6% of this endpoint's CPU. Exactly equivalent: the
    # function's first act is to skip any match whose league doesn't match.
    matches_by_league: dict[str, list[dict]] = {}
    for _m in all_matches:
        matches_by_league.setdefault(_m.get("league"), []).append(_m)
    for division, league_markets in by_league.items():
        state = elo_service_soccer.get_rating_state(division)
        if state is None:
            sim_by_league[division] = None
            continue
        second_tier_division = PROMOTION_SOURCE_DIVISION.get(division)
        second_tier_state = elo_service_soccer.get_rating_state(second_tier_division) if second_tier_division else None
        # RESOLVE MARKET SPELLINGS ONTO THE POOL, per league.
        #
        # Polymarket writes full club names and football-data writes short ones
        # ("1. FC Kaiserslautern" vs "kaiserslautern"), so before this only
        # 82/162 league-title legs found their rating and the rest priced as
        # nothing -- D2 matched 1 of 18. See soccer_pool_resolver for why this
        # is derived rather than added to TEAM_ALIASES, and for the uniqueness
        # rule that makes it safe.
        #
        # AN UNRESOLVED TEAM STILL ENTERS THE FIELD, under its own key. That is
        # deliberate and is the field-completeness lesson from the esports
        # tournament sim: dropping a team shrinks the field the sim normalises
        # over, so the survivors' probabilities inflate to fill the gap. The sim
        # already gives an unrated entrant a real placeholder rating (see
        # simulate_season's unrated_teams handling), so passing it through keeps
        # the league's size honest instead of manufacturing confidence.
        # THE MAPPING MUST BE INJECTIVE. Two market teams resolving onto one
        # pool spelling both read that team's probability, so the group stops
        # summing to 1: Argentina came out at 1.059 and Eredivisie at 0.979
        # before this guard. Worse than the arithmetic, it means we cannot tell
        # the two clubs apart -- Argentina fields several "Gimnasia"s and
        # "Estudiantes"s -- so the honest move is to refuse BOTH and let them
        # enter the field under their own keys, exactly as resolve_to_pool
        # already refuses an ambiguous single lookup.
        _pool = set(state.attack_log)
        _claims: dict[str, list[int]] = {}
        for _m in league_markets:
            if not _m.team:
                continue
            resolved = resolve_to_pool(_m.team, _pool)
            if resolved:
                _claims.setdefault(resolved, []).append(_m.id)
            pool_key_by_market_id[_m.id] = resolved or canonical_team_key(_m.team)
        for _resolved, _ids in _claims.items():
            if len(_ids) > 1:
                for _id in _ids:
                    _market = next(x for x in league_markets if x.id == _id)
                    pool_key_by_market_id[_id] = canonical_team_key(_market.team)
        canonical_teams = list({pool_key_by_market_id[m.id]
                                for m in league_markets if m.team})
        # SEED FROM THE REAL TABLE. Without this the sim runs a fresh season from
        # zero, which is only correct between seasons -- it silently discards
        # every point already banked. Harmless for the five European leagues
        # while they are on their summer break (current_season_table returns
        # empty for them and the call behaves exactly as before), and badly
        # wrong for a calendar-year league: Brazil was 20.5 of 38 rounds in when
        # this was added, with Palmeiras 8 points clear, and the old call
        # modelled that as level.
        starting_table, played_pairs = current_season_table(division, matches_by_league.get(division, []))
        if played_pairs:
            mid_season_divisions.add(division)
        # HOW FAR INTO ITS SEASON THIS LEAGUE IS. Feeds the season-progress gate
        # below -- see season_sim_soccer.MIN_SEASON_PROGRESS for the measurement
        # that makes it necessary. Recorded for EVERY division, including ones
        # with no table yet: those read 0.0, which is the correct answer and the
        # one that blocks.
        progress_by_division[division] = season_progress(len(canonical_teams), played_pairs)
        sim_by_league[division] = simulate_season(
            state, canonical_teams, division, n_simulations=3000, second_tier_state=second_tier_state,
            starting_table=starting_table, played_pairs=played_pairs,
        )

    # STASH THE PER-LEAGUE SIMS FOR THE INTEGRITY CHECK.
    #
    # Write-only, after the fact: this records what was just computed and changes
    # nothing about how anything is priced. It exists because soccer is the last
    # sim-backed futures sport with no coherence guard, and the reason is purely
    # that its sims are built PER REQUEST here -- CFB was coverable only because
    # poller_cfb._CONF_SIM already held its result for a cheap check to read
    # (see integrity_checks.incoherent_group_legs).
    #
    # Soccer's one-winner legs are per-league: league_winner sums to 1 in each
    # league, relegation to that league's automatic-drop count. With this in
    # place a group-aware check can assert both without re-running a 3,000-trial
    # Monte Carlo inside the health endpoint.
    #
    # Last-write-wins and deliberately unlocked: a stale or half-populated read
    # can only make the CHECK quieter, never mis-price a market, so it needs no
    # synchronisation.
    _LAST_SIM_BY_LEAGUE["data"] = dict(sim_by_league)

    # One cached simulation prices every MLS Cup and conference-bracket row (see
    # _MLS_PLAYOFF_MARKET_TYPES). Cached rather than run here because assembling
    # its inputs costs ~10 live ESPN calls -- unlike the European sim above,
    # whose inputs are already in memory.
    mls_playoff = playoff_sim_service_mls.get_result()
    # Liga MX entrants per torneo, read off the live market rows -- the market's
    # field is the authority on who is actually entered, so a promoted or
    # relegated club cannot be missed.
    _ligamx_fields: dict[str, list[str]] = {}
    for _m in markets:
        if _m.market_type == _LIGAMX_MARKET_TYPE and _m.team:
            _ligamx_fields.setdefault(_m.group_label or "", []).append(canonical_team_key(_m.team))

    def _pool_key(m: Market) -> str:
        """The pool spelling for this row, or the plain canonical key when the
        league had no rating state (pool_key_by_market_id is only filled for
        leagues whose sim actually ran)."""
        return pool_key_by_market_id.get(m.id) or canonical_team_key(m.team)

    out = []
    for m in markets:
        division = _futures_division(m)
        sim_result = sim_by_league.get(division) if division else None
        model_prob = None
        if m.market_type == _LIGAMX_MARKET_TYPE:
            # One sim PER TORNEO: Kalshi lists Apertura and Clausura at the same
            # time and they are separate championships, so group_label -- not the
            # series -- selects the result. Merging them would build a 36-team
            # field that never plays.
            _lg = m.group_label or ""
            _field = _ligamx_fields.get(_lg) or []
            _res = playoff_sim_service_ligamx.get_result(_lg, _field) if _field else None
            if _res is not None and m.team:
                raw = _res.champion_prob.get(_pool_key(m))
                # None rather than 0.0 for a team the sim does not carry: on an
                # 18-team field 0.0 reads as a confident "cannot win", which is a
                # fabricated price -- same reasoning as the MLS branch below.
                model_prob = round(raw, 4) if raw is not None else None
        elif m.market_type in _MLS_PLAYOFF_MARKET_TYPES:
            if mls_playoff is not None and m.team:
                probs = (mls_playoff.cup_champion_prob if m.market_type == "mls_cup_winner"
                         else mls_playoff.conference_champion_prob)
                raw = probs.get(_pool_key(m))
                # None (not 0.0) for a team the sim doesn't carry: on a 30-team
                # bracket 0.0 reads as a confident "cannot win", which is a
                # fabricated price, same reasoning as the team_points rungs.
                model_prob = round(raw, 4) if raw is not None else None
        elif sim_result is not None and m.team:
            if m.market_type == "team_points":
                # Not a rank question -- read the simulated season-points
                # distribution at this rung's threshold. Left unpriced (None) when
                # the sim doesn't cover the team, rather than defaulting to 0.0
                # the way the rank markets below do: on a points ladder 0.0 is a
                # confident "will not reach", which is a fabricated price.
                raw = prob_points_at_least(sim_result, _pool_key(m), m.line) if m.line is not None else None
                model_prob = round(raw, 4) if raw is not None else None
            else:
                prob_field = _SIM_PROB_FIELD_BY_MARKET_TYPE.get(m.market_type, "champion_prob")
                prob_dict = getattr(sim_result, prob_field)
                # AN ALL-ZERO DISTRIBUTION MEANS THE SIM DECLINED, NOT THAT
                # EVERY TEAM IS AT 0%.
                #
                # simulate_season fills clean_sheet_leader with 0.0 for every
                # team and then skips it entirely when a season is part-played
                # and the caller has not supplied starting clean-sheet counts --
                # its own comment says "Emit nothing in that case rather than a
                # wrong number", because the count would otherwise cover
                # simulated matches alone and understate everyone.
                #
                # `.get(key, 0.0)` then turned that refusal into a confident
                # "0% chance". Measured on the live board when most_clean_sheets
                # shipped: 6 of 12 leagues (ARG1 BRA1 CHN1 NOR1 SC0 SWE1) served
                # 0.000 for EVERY team, against real market prices of 0.3-0.5 --
                # a fabricated -30pp edge on every row. The other six summed to
                # 0.79-1.00 as a real distribution should.
                #
                # Same reasoning team_points states just above: a fabricated
                # price is worse than an absent one. Scoped to the types whose
                # sim can decline, so the rank markets keep their existing
                # behaviour.
                if (m.market_type in _SIM_MAY_DECLINE
                        and not any(v for v in prob_dict.values())):
                    model_prob = None
                    if not no_baseline_reason:
                        no_baseline_reason = _SIM_EMITTED_NOTHING
                else:
                    model_prob = round(prob_dict.get(_pool_key(m), 0.0), 4)
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "soccer", m.market_type)
        # SEASON-PROGRESS GATE. Priced but not stakeable while the league is too
        # early in its season for this question to be calibrated. kelly=None is
        # the established "did not qualify" signal size_stake_dollars reads, and
        # a null stake is what keeps a row off the recommended list.
        #
        # Labelled rather than hidden, matching the MLS-bracket and CFB posture:
        # the model number stays visible with a note saying why it is not backed
        # and when it will be.
        _div = _futures_division(m)
        _progress = progress_by_division.get(_div, 0.0)
        if not season_progress_ok(m.market_type, _progress):
            kelly = None
        # UNRATED CLUB. Match pricing already refuses these -- get_match_distribution
        # returns None when either side has no prior matches, enforced in the
        # service so no caller can skip it. The season sim had no such rule: it
        # hands an unrated club a placeholder rating and then NORMALISES over the
        # field, which produced LASK at 78.5% against a 13% market and IK Sirius
        # at 95.3%. 128 of 735 live rows (17%) are for such clubs.
        #
        # The season-progress gate hides this today, but it lifts on fixtures
        # PLAYED, not on the club becoming rated -- so without this, an unrated
        # club becomes stakeable the moment its league matures.
        #
        # Blocks only the unrated club's OWN row. The club stays IN the simulated
        # field on purpose: simulate_season normalises over the teams it is
        # given, so dropping one hands its share to everyone else and quietly
        # corrupts the rated clubs' prices too -- the field-completeness failure
        # the racing sim already had.
        if _div and m.team and elo_service_soccer.get_team_match_count(_div, m.team) == 0:
            kelly = None
        stake_dollars = size_stake_dollars(staking_mode, kelly, futures_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=FUTURES_UNIT_SCALE, min_market_price=FUTURES_MIN_MARKET_PRICE, max_spread=FUTURES_MAX_SPREAD, yes_bid=snap.yes_bid if snap else None, yes_ask=snap.yes_ask if snap else None, sport="soccer", team=m.team)
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        out.append(
            FuturesMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                # Same shape as placed_bets._cross_platform_key's futures branch
                # (sport|market_type|team) but with the club name canonicalised,
                # so Kalshi's "Eindhoven" and Polymarket's "PSV Eindhoven" are
                # one proposition rather than two.
                cross_key=f"soccer|{m.market_type or ''}|{canonical_team_key(m.team or '')}",
                group_label=m.group_label,
                # Division code; paper_logger maps it to a readable name. The MLS
                # bracket markets have no division (by design -- see
                # _MLS_PLAYOFF_MARKET_TYPES), so name the competition directly.
                league="MLS" if m.market_type in _MLS_PLAYOFF_MARKET_TYPES else division,
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
    # Which (family, club) slots are already held by REAL, still-pending money.
    # paper is excluded on purpose, same rule as models/exposure.py: the paper
    # logger records everything priced, so counting it would starve the board of
    # real recommendations because it had been busy simulating.
    taken_families: set[tuple] = set()
    for pb in (
        session.query(PlacedBet)
        .filter(PlacedBet.paper == False,  # noqa: E712 -- SQLAlchemy needs ==
                PlacedBet.status == "pending",
                PlacedBet.stake_pool == "futures",
                PlacedBet.sport == "soccer")
        .all()
    ):
        fam = _NESTED_POSITION_FAMILIES.get(pb.market_type or "")
        if fam and pb.team:
            taken_families.add((fam, pb.team))
    _collapse_nested_position_futures(out, taken_families)
    # WITHIN-type rungs (team_points: 50+/60+/70+ points) -- a different shape
    # from the cross-type nesting above, and soccer had no entry in
    # LADDER_FUTURES_TYPES at all until 2026-08-20.
    ladder_zeroed = apply_ladder_futures_cap(out, "soccer")
    if ladder_zeroed:
        log.info("soccer futures: collapsed %d team_points ladder rungs", ladder_zeroed)

    # ONE STAKE PER PROPOSITION ACROSS VENUES. Soccer was the ONLY router of
    # eleven that never called this -- it did not even import it -- while
    # market_catalog_soccer's own upsert_polymarket_soccer_league_winner_row
    # docstring says these rows "MUST go through apply_duplicate_listing_cap or
    # one title gets staked twice". The instruction was written and never wired,
    # which is the same helper-exists-but-is-not-called shape as the 2026-08
    # rollout that left $4,180 double-staked across four routers.
    #
    # MEASURED on the live board before fixing: 26 propositions are listed more
    # than once -- 8 across Kalshi AND Polymarket, 18 duplicated within Kalshi
    # alone -- and 0 are currently staked twice. The exposure is latent, held
    # back only by the volume gate and the 20pp bar, not by anything structural.
    # Adding a second venue for relegation would widen it.
    #
    # No fixture_attr: these are season-long rows with no match to key on, so
    # identity is (team, market_type, line, side) exactly as for NFL and MLB
    # futures.
    duped = apply_duplicate_listing_cap(out)
    if duped:
        log.info("soccer futures: collapsed %d cross-listed duplicate(s)", duped)

    # The MLS bracket model is UNCALIBRATED, and saying so is the point.
    #
    # When it shipped (2026-08-07) it was checked for COHERENCE -- cup
    # probabilities summing to exactly 1.0, conference to 2.0, playoff berths to
    # 18.0 -- and that was mistaken for validation. It isn't. The racing
    # finishing-order model summed correctly too and was still 30pp wrong:
    # coherence means the arithmetic is right, calibration means the numbers are
    # true. Nothing here has ever been compared against real outcomes.
    #
    # And it cannot be, yet. MLS's current playoff format has one completed
    # postseason in this app's data. A model cannot be calibrated against one
    # event, so the honest posture is the same "approximate" flag CFB's playoff
    # markets carry -- priced, staked like anything else, clearly labelled, and
    # judged on results rather than suppressed on theory (see
    # cfb_markets.APPROXIMATE_MARKET_TYPES for why suppressing is worse).
    #
    # Appended rather than assigned so it cannot clobber the nested-collapse
    # note above, which carries a staking consequence this one does not.
    for row in out:
        if row.market_type in _MLS_PLAYOFF_MARKET_TYPES:
            row.model_note = f"{row.model_note or ''} {MLS_BRACKET_APPROXIMATE_NOTE}".strip()
    # Same posture as the MLS bracket above and CFB's approximate markets:
    # labelled rather than suppressed, and judged on results. Applied per
    # DIVISION rather than per market_type because "league_winner" is shared by
    # eight leagues, only some of which are mid-season at any given moment --
    # and which ones changes with the calendar, so this cannot be a static list.
    for row in out:
        if _futures_division(row) in mid_season_divisions:
            row.model_note = f"{row.model_note or ''} {MID_SEASON_SIM_NOTE}".strip()
    # Season-progress note, on the rows the gate actually blocked. Keyed off a
    # null stake PLUS a real model number, so it cannot fire on a row left
    # unstaked for some other reason (no book, no price, below the edge bar).
    for row in out:
        _div = _futures_division(row)
        if _div is None or row.model_prob is None:
            continue
        _prog = progress_by_division.get(_div, 0.0)
        if row.suggested_stake_dollars is not None:
            continue
        if row.team and elo_service_soccer.get_team_match_count(_div, row.team) == 0:
            row.model_note = (
                f"{row.model_note or ''} Not staked: this club has no rated match "
                f"history in {_div}, so its probability comes from a placeholder "
                f"strength rather than a real estimate of this team. The same rule "
                f"already blocks match pricing for unrated clubs."
            ).strip()
        elif not season_progress_ok(row.market_type, _prog):
            row.model_note = f"{row.model_note or ''} {season_progress_note(row.market_type, _prog)}".strip()

    out.sort(key=lambda m: (m.group_label or "", -(m.implied_prob or 0)))
    return out


# Finishing-position families whose members are STRICTLY NESTED: winning the
# league implies top 2, which implies top 4, which implies top 6. Same at the
# bottom of the table for relegation/bottom-3.
_NESTED_POSITION_FAMILIES: dict[str, str] = {
    "league_winner": "finish_top",
    "top2": "finish_top",
    # top_half was MISSING until 2026-08-20 while top2/top4/top6 were all here,
    # so the widest rung of the EPL ladder was the one rung that could always be
    # staked alongside a narrower one. Verified nested on the live board: across
    # 20 clubs and 60 adjacent comparisons, top_half >= top4 >= top2 >=
    # league_winner with zero violations.
    "top_half": "finish_top",
    "top4": "finish_top",
    "top6": "finish_top",
    "relegation": "finish_bottom",
    "bottom3": "finish_bottom",
    # Winning the MLS Cup REQUIRES winning your conference bracket first -- the
    # sim literally produces the cup winner by playing the two conference
    # winners against each other. So backing a club in both is one opinion at
    # two prices, exactly the case this collapse exists for. Note these are the
    # same nested family despite being different market_type strings, which is
    # the same shape that let Manchester City reach 27% of the book.
    "mls_cup_winner": "mls_playoff",
    "ligamx_champion": "ligamx_liguilla",
    "mls_conference_winner": "mls_playoff",
}
NESTED_POSITION_NOTE = (
    "Not staked: a wider threshold on the same club is already staked, and these are nested "
    "(winning the league implies top 2 implies top 4). Backing several is one opinion at three "
    "prices, not three bets -- if the model is wrong about this club they all lose together."
)


def _collapse_nested_position_futures(rows: list[FuturesMarketOut], taken: set[tuple] | None = None) -> None:
    """Keep ONE staked rung per (source, club, nested family); zero the rest.

    Found on the live board 2026-08-07: Manchester City was 27.3% of the entire
    soccer futures book across league_winner + top2 + top4 -- one view of City
    staked three times at three thresholds. This is the same thing
    LADDER_MARKET_TYPES already collapses for win-total rungs, but it slipped
    through because these are three different market_type strings rather than
    three lines of one type.

    DONE HERE, IN THE BACKEND, on purpose. The obvious home looked like
    frontend markets.ts::ladderCollapseKey, where the other ladder collapses
    live -- but buildSoccerRecommendedBets keeps its OWN local ladder set and
    never calls that helper, and it takes only a weekly pool, so soccer futures
    never pass through it at all. They reach the user solely via the Futures
    page, which applies no collapse. A rule added there would have been dead
    code for the exact case it was written for. `suggested_stake_dollars` is the
    field every view already filters on, so zeroing it here is the one place
    that cannot be bypassed.

    Rows are kept and priced, only unstaked -- the same "zeroed AFTER sizing so
    the model number and edge still surface for tracking" posture used for map
    markets and the CFB weak-pool badge.

    Deliberately NOT applied across market types that merely correlate:
    division_winner vs conference_champion are genuinely different outcomes
    (winning one does not imply the other). Those are bounded by the per-team
    dollar ceiling in models/exposure.py instead.
    """
    best: dict[tuple, FuturesMarketOut] = {}
    taken = taken or set()
    for row in rows:
        family = _NESTED_POSITION_FAMILIES.get(row.market_type)
        if family is None or row.suggested_stake_dollars is None or not row.team:
            continue
        # A SIBLING ALREADY BACKED WITH REAL MONEY OCCUPIES THE FAMILY'S SLOT.
        #
        # This collapse used to be stateless: it picked a survivor per refresh
        # and had no idea one had already been placed. Survivorship is chosen by
        # EDGE, which moves with the price while model_prob barely moves -- so as
        # prices drift the survivor hops between rungs, and each hop offers a
        # fresh-looking bet on a club already backed.
        #
        # That is exactly how it went wrong (user-reported 2026-08-20):
        # Manchester City top2 placed 2026-08-17 22:54, then top4 placed
        # 2026-08-18 23:50 once the edges had moved and top4 became the day's
        # survivor. Two nested legs on one club -- the very outcome this function
        # was written on 2026-08-07 to prevent, defeated by time rather than by
        # any single response being wrong.
        #
        # Placed-bet awareness is what closes it, and it is one-directional: it
        # can only REMOVE a stake, never add one, so a feed hiccup that returns
        # no placed bets degrades to exactly the old behaviour.
        if (family, row.team) in taken:
            row.suggested_stake_dollars = None
            row.suggested_stake_units = None
            row.stake_pool = None
            row.model_note = NESTED_POSITION_NOTE
            continue
        key = (family, row.source, row.team)
        held = best.get(key)
        if held is None or (row.edge or 0) > (held.edge or 0):
            if held is not None:
                held.suggested_stake_dollars = None
                held.suggested_stake_units = None
                held.stake_pool = None
                held.model_note = NESTED_POSITION_NOTE
            best[key] = row
        else:
            row.suggested_stake_dollars = None
            row.suggested_stake_units = None
            row.stake_pool = None
            row.model_note = NESTED_POSITION_NOTE


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
        # CROSS-LEAGUE FIXTURES DO NOT HAVE A POOL OF THEIR OWN.
        #
        # REAL BUG (user-reported 2026-08-12: "Miami vs Leon both show 0 ranked
        # matches -- do we have enough data to trust this?"). The three lookups
        # below were keyed on `match.league`, which for a Leagues Cup fixture is
        # the literal string "LEAGUES_CUP" -- a competition, not a rating pool.
        # No such pool exists, so the drawer reported 0 rated matches for BOTH
        # clubs and no expected goals, on a row the app had priced and staked
        # $10 on. The truth was Inter Miami 219 rated matches and Club Leon 530,
        # in the MLS and MEX1 pools that predict_leagues_cup_match actually uses.
        #
        # The pricing path was right the whole time -- predict_leagues_cup_match
        # REFUSES a club with get_count <= 0 rather than inventing a default
        # rating, so a priced Leagues Cup row already proved both clubs were
        # rated. Only the explainer was wrong, which is the worst place for it:
        # the drawer exists to tell you whether to believe the number.
        # Same class as the esports rating-lookup/pricing-path split.
        lc_pred = _leagues_cup_prediction(match)
        if lc_pred is not None:
            dist = lc_pred.distribution
            home_n = elo_service_soccer.get_team_match_count(lc_pred.home_league, match.home_team)
            away_n = elo_service_soccer.get_team_match_count(lc_pred.away_league, match.away_team)
            pool_note = f" [{lc_pred.home_league} vs {lc_pred.away_league}]"
        else:
            dist = elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team)
            home_n = elo_service_soccer.get_team_match_count(match.league, match.home_team)
            away_n = elo_service_soccer.get_team_match_count(match.league, match.away_team)
            pool_note = ""
        factors.append(ReasoningFactorOut(label="League", detail=f"{match.league} ({match.season}){pool_note}"))
        factors.append(ReasoningFactorOut(
            label="Team match history",
            detail=f"{match.home_team}: {home_n} rated matches, {match.away_team}: {away_n} rated matches",
        ))
        if lc_pred is not None:
            factors.append(ReasoningFactorOut(
                label="Cross-league strength offset",
                detail=(f"{lc_pred.home_league} vs {lc_pred.away_league}, gap {lc_pred.strength_gap:+.3f} log-goals. "
                        "Fitted on 172 completed Leagues Cup matches held out by season; "
                        "venue term measured at ~0 because the competition is played at neutral sites."),
            ))
            plaus = _competition_goal_plausibility(session, match, dist)
            if plaus is not None:
                factors.append(plaus)
        if dist is not None:
            factors.append(ReasoningFactorOut(
                label="Model expected goals",
                detail=f"{match.home_team} {dist.expected_home_goals:.2f} - {dist.expected_away_goals:.2f} {match.away_team}",
            ))
            factors.append(ReasoningFactorOut(
                label="Model outcome split",
                detail=f"Home {dist.prob_home_win()*100:.1f}% / Draw {dist.prob_draw()*100:.1f}% / Away {dist.prob_away_win()*100:.1f}%",
            ))
        # The leagues_cup_* family had no Pick line either, so even once the
        # numbers appeared the drawer never said what the bet actually was.
        if m.market_type == "leagues_cup_spread" and m.team is not None and m.line is not None:
            factors.append(ReasoningFactorOut(label="Pick", detail=f"{m.team} wins by more than {m.line} goals"))
        if m.market_type == "leagues_cup_total" and m.line is not None:
            factors.append(ReasoningFactorOut(label="Pick", detail=f"Over {m.line} total goals"))
        if m.market_type == "leagues_cup_btts":
            factors.append(ReasoningFactorOut(label="Pick", detail="Both teams to score"))
        if m.market_type == "leagues_cup_moneyline_3way":
            pick = "Draw" if m.side == "draw" else (m.team or "—")
            factors.append(ReasoningFactorOut(label="Pick", detail=f"{pick} (90 minutes)"))
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

    # FUTURES. Everything above sits under `if match is not None`, and a futures
    # market has no soccer_match_id -- it is season-long, not tied to one game.
    # So this endpoint used to return ZERO factors for all 714 soccer futures
    # rows: a 200 with an empty body, which renders as a reasoning panel that
    # silently shows nothing rather than an error. Found 2026-08-08 while
    # auditing every sport's reasoning after the CFB routing bug.
    elif m.market_type in _FUTURES_MARKET_TYPES:
        division = _futures_division(m)
        if m.market_type in _MLS_PLAYOFF_MARKET_TYPES:
            division = "MLS"
        label = f"{m.team or '—'} — {m.group_label or m.market_type}"
        if division:
            state = elo_service_soccer.get_rating_state(division)
            n = state.get_count(canonical_team_key(m.team)) if (state and m.team) else 0
            factors.append(ReasoningFactorOut(
                label="Competition",
                detail=f"{_SOCCER_LEAGUE_NAME.get(division, division)} ({division})"))
            factors.append(ReasoningFactorOut(
                label="Team match history",
                detail=(f"{m.team}: {n} rated matches in this league's pool"
                        if n else f"{m.team}: no rated history in this pool -- priced off a promoted-team placeholder")))
        question = {
            "league_winner": "Wins the league",
            "relegation": "Finishes in the automatic relegation zone",
            "top2": "Finishes in the top 2", "top4": "Finishes in the top 4",
            "top_half": "Finishes in the top half",
            "team_points": f"Finishes the season on {m.line} or more points" if m.line is not None else "Season points total",
            "mls_cup_winner": "Wins the MLS Cup",
            "ligamx_champion": "Wins the Liga MX torneo (via the Liguilla)",
            "mls_conference_winner": "Wins their conference's playoff bracket",
        }.get(m.market_type, m.market_type)
        factors.append(ReasoningFactorOut(label="Question", detail=question))
        if m.market_type in _MLS_PLAYOFF_MARKET_TYPES:
            factors.append(ReasoningFactorOut(
                label="How this is priced",
                detail=("A playoff bracket simulation seeded from the live MLS conference standings and the "
                        "real remaining fixture list, run 10,000 times. It has never been checked against "
                        "real results -- MLS's current format has only one completed postseason -- so treat "
                        "the edge as a disagreement with the market, not a proven opportunity.")))
        else:
            factors.append(ReasoningFactorOut(
                label="How this is priced",
                detail=("A Monte Carlo of the full season: every pairing's goal distribution comes from the "
                        "same Poisson attack/defence ratings used for single matches, and 3,000 simulated "
                        "seasons are ranked by points, goal difference and goals scored.")))

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
    if _leagues_cup_prediction(match) is not None:
        methodology = (
            "A CROSS-LEAGUE model, not the domestic one (see leagues_cup_match.py). Each club keeps its own "
            "league's attack/defence ratings -- Inter Miami's from MLS, a Liga MX club's from MEX1 -- which are "
            "not comparable on their own, so a fitted strength offset bridges them. The offset was fitted on 172 "
            "completed Leagues Cup matches and held out BY SEASON, improving Poisson deviance in three of four "
            "held-out seasons. The venue term was MEASURED rather than assumed and came out at ~0, because the "
            "competition is played at neutral or near-neutral US sites -- reusing the domestic home advantage "
            "would have applied a ~30% scoring boost that does not exist. It refuses to price any pairing that "
            "is not MLS vs Liga MX, and refuses any club with no rated history rather than using a default "
            "rating. model_validated stays false: the offset predicts GOALS better than assuming the leagues "
            "are equal, which is a weaker claim than beating a market price."
        )
    insight = ""
    if match is not None:
        _lc = _leagues_cup_prediction(match)
        # Same cross-league fix as the factors above -- match.league is
        # "LEAGUES_CUP", which is a competition and not a rating pool, so the
        # domestic lookup returns None and the narrative silently goes blank.
        dist = (_lc.distribution if _lc is not None
                else elo_service_soccer.get_match_distribution(match.league, match.home_team, match.away_team))
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
