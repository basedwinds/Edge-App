"""Tennis markets API -- parallel to routers/mma_markets.py.

Moneyline (surface-blended walk-forward Elo, elo_tennis.py) plus set
winner, game spread/total, and exact match score (game_lines_tennis.py --
real, regressed-from-data constants, see that module's own docstring and
scripts/derive_tennis_game_line_constants.py). Backtested across all three
real tiers this app can train on (tour/challenger/itf) -- NO edge found at
any tier for moneyline (see elo_tennis.py's docstring for the real
numbers); the set/game markets have NOT been backtested against real
historical odds yet (tennis-data.co.uk/tennisexplorer don't carry these
market types' own historical prices) -- ships as an honest, real-data-
derived reference estimate, model_validated: false everywhere, same policy
as every other market in this app.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice
from app.api.routers.settings import get_staking_params, get_flat_params, get_tennis_pool_dollars, get_unit_dollars
from app.api.schemas import FuturesMarketOut, ReasoningFactorOut, ReasoningOut, TennisMarketOut
from app.clients import flashscore_tennis_client
from app.clients.tennisexplorer_client import TennisExplorerClient
from app.db.database import get_session
from app.db.models import Market, TennisMatch
from app.ingestion.market_catalog_tennis import tournament_name_to_slug
from app.ingestion.market_matcher_tennis import full_name_to_abbreviated_key
from app.models import game_lines_tennis
from app.models.baseline import elo_service_tennis
from app.models.bracket_sim_tennis import simulate_tournament
from app.models.ladder_sanity import (
    find_resolved_entities,
    looks_already_live_by_trading,
    pair_looks_live_by_surge,
    pair_looks_live_by_travel,
    pair_looks_resolved,
)
from app.models.staking import FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

router = APIRouter(prefix="/tennis", tags=["tennis"])

GAME_MARKET_TYPES = {
    "moneyline", "set_winner", "game_spread", "set_spread", "game_total",
    "exact_score", "set_total", "total_sets",
}

NO_BASELINE_REASON = (
    "No baseline yet -- this market's model is still being built and validated against this app's "
    "own historical data, not shipped as a guessed number."
)

# REAL FINDING (2026-07-19, user-reported): a player with ZERO real matches
# in either offline training source gets Elo's neutral BASE_RATING (1500)
# by construction -- not an actual estimate of their skill, just the prior.
# Paired against a real, rated opponent below 1500, this produces a
# mathematically "correct" but practically meaningless high win probability
# (confirmed live: exactly this scenario produced a 72.7% model estimate
# against a real 0.5% market price). Validated the 0-match cutoff against a
# real walk-forward check (see elo_service_tennis.py::get_player_match_count's
# own docstring for the numbers) before picking it, rather than guessing.
NO_HISTORY_REASON = (
    "One or both players have no tracked match history in this app's own data (tennis-data.co.uk / "
    "tennisexplorer) -- their rating would be a pure neutral placeholder, not a real estimate, so no "
    "model number is shown rather than risk a misleadingly confident one."
)


def _resolve_side(market_team: str | None, match: TennisMatch | None) -> tuple[str, str] | None:
    """Returns (player_key, opponent_key) for whichever of player_a/player_b
    `market_team` refers to, or None if it can't be resolved (shouldn't
    happen in practice -- market.team is always one of the match's two real
    names)."""
    if match is None or market_team is None:
        return None
    key_from_market = full_name_to_abbreviated_key(market_team)
    if key_from_market is None:
        return None
    if key_from_market == match.player_a_key:
        return match.player_a_key, match.player_b_key
    if key_from_market == match.player_b_key:
        return match.player_b_key, match.player_a_key
    return None


def _elo_diff(player_key: str, opponent_key: str, surface: str | None) -> float | None:
    p_r = elo_service_tennis.get_player_rating(player_key, surface)
    o_r = elo_service_tennis.get_player_rating(opponent_key, surface)
    if p_r is None or o_r is None:
        return None
    return p_r - o_r


def _match_best_of(match: TennisMatch) -> int:
    """Live-created matches (see market_catalog_tennis.py::find_or_create_upcoming_match)
    don't carry a real best_of yet -- no free live source flags a Grand Slam
    match specifically. Defaults to 3 (correct for the overwhelming
    majority: WTA is always Bo3, Challenger/ITF are always Bo3, and most
    ATP tour matches are Bo3 -- only ATP Grand Slams are Bo5). A real, known
    simplification, not a guess dressed up as a fact -- flagged here rather
    than silently assumed correct."""
    return match.best_of or 3


def _moneyline_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    sides = _resolve_side(market.team, match)
    if sides is None:
        return None
    player_key, opponent_key = sides
    p = elo_service_tennis.get_match_win_prob(player_key, opponent_key, match.surface)
    return round(p, 4) if p is not None else None


def _set_winner_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    sides = _resolve_side(market.team, match)
    if sides is None or match is None:
        return None
    player_key, opponent_key = sides
    elo_diff = _elo_diff(player_key, opponent_key, match.surface)
    if elo_diff is None:
        return None
    return round(game_lines_tennis.prob_win_set(elo_diff), 4)


def _game_spread_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    sides = _resolve_side(market.team, match)
    if sides is None or match is None or market.line is None:
        return None
    player_key, opponent_key = sides
    elo_diff = _elo_diff(player_key, opponent_key, match.surface)
    if elo_diff is None:
        return None
    return round(game_lines_tennis.prob_game_spread_cover(market.line, elo_diff), 4)


def _game_total_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    if match is None or market.line is None:
        return None
    elo_diff = _elo_diff(match.player_a_key, match.player_b_key, match.surface)
    if elo_diff is None:
        return None
    best_of = _match_best_of(match)
    p_over = game_lines_tennis.prob_over_total_games(market.line, elo_diff, best_of)
    return round(p_over, 4)


def _exact_score_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    sides = _resolve_side(market.team, match)
    if sides is None or match is None or not market.side or "-" not in market.side:
        return None
    player_key, opponent_key = sides
    elo_diff = _elo_diff(player_key, opponent_key, match.surface)
    if elo_diff is None:
        return None
    try:
        player_sets, opponent_sets = (int(x) for x in market.side.split("-", 1))
    except ValueError:
        return None
    p_a_moneyline = elo_service_tennis.get_match_win_prob(player_key, opponent_key, match.surface)
    if p_a_moneyline is None:
        return None
    best_of = _match_best_of(match)
    table = game_lines_tennis.prob_exact_score(elo_diff, best_of, p_a_moneyline)
    if table is None:
        return None
    prob = table.get((player_sets, opponent_sets))
    if prob is None:
        return None
    # THE TABLE IS CONDITIONAL ON THAT PLAYER WINNING, so it must be scaled by
    # the probability they win at all. Proven, not assumed: its entries sum to
    # 1.0 across winner-side scorelines only (2-0 and 2-1 for a Bo3, with no
    # 0-2/1-2 mass at all), and its straight-sets values of 0.670-0.774 line up
    # with the 0.705 straight-sets rate measured over 1,089 real completed Bo3
    # matches in this app's own cache -- i.e. it is P(scoreline | this player
    # wins), which is exactly how derive_tennis_game_line_constants.py bucketed
    # it ("favorite_sets-underdog_sets" by favourite win-prob decile).
    #
    # Returning it raw claimed a 67% chance of "Player A wins 2-0" in an evenly
    # matched game, where the real unconditional figure is about 34%. That
    # roughly doubled the model probability on every exact-score market and
    # manufactured edge against perfectly sane prices.
    p_player_wins = p_a_moneyline if player_sets > opponent_sets else 1.0 - p_a_moneyline
    return round(prob * p_player_wins, 4)


def _set_spread_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    """Polymarket's "Set Handicap +/-1.5" -- a SET handicap, measured not assumed.

    This market used to be stored as `game_spread` and priced with the GAMES
    model, on the strength of a client docstring asserting it was "confirmed
    live to be a GAMES differential". It is not. Tested against 120 resolved
    markets on real 3-set matches -- the cases where the two readings disagree,
    since nobody wins 2-0 in a three-setter -- the set reading was right 120/120
    and the games reading 73/120. Every -1.5 side resolved to 0 and every +1.5
    side to 1, which is the set handicap's defining behaviour and not the games
    handicap's (Jay Clarke covered +1 on games yet resolved 0).

    So -1.5 means "wins by 2+ SETS": 2-0 in a Bo3, 3-0 or 3-1 in a Bo5. That is
    read straight off the exact-score distribution rather than approximated,
    scaled by the win probability for the same conditional-table reason as
    _exact_score_model_prob above.
    """
    sides = _resolve_side(market.team, match)
    if sides is None or match is None or market.line is None:
        return None
    player_key, opponent_key = sides
    elo_diff = _elo_diff(player_key, opponent_key, match.surface)
    p_win = elo_service_tennis.get_match_win_prob(player_key, opponent_key, match.surface)
    if elo_diff is None or p_win is None:
        return None
    best_of = _match_best_of(match)
    table = game_lines_tennis.prob_exact_score(elo_diff, best_of, p_win)
    if table is None:
        return None
    margin = abs(market.line)
    # CAREFUL WITH THIS TABLE. It is conditional on the FAVOURITE winning --
    # expressed in the calling player's coordinates, but every entry is a
    # favourite-win scoreline. A first version of this function summed only the
    # entries where the calling player wins, which silently returned 0 for the
    # underdog's -1.5 side and therefore 1.0 for every +1.5 side. The mass for
    # "underdog wins 2-0" is not in this table at all.
    #
    # So take from it only what it actually knows: the WINNER'S margin
    # distribution, which is perspective-free. `blowout` is P(the winner, whoever
    # that turns out to be, wins by more than `margin` sets) -- about 0.67 at
    # even strength rising to 0.77 for a strong favourite, matching the 0.705
    # straight-sets rate measured over 1,089 real completed Bo3 matches.
    blowout = sum(
        prob for (mine, theirs), prob in table.items()
        if abs(mine - theirs) > margin
    )
    if market.line < 0:
        return round(p_win * blowout, 4)
    # The +margin side loses only when the OPPONENT wins by more than margin.
    # Written this way the two sides of one market sum to exactly 1 by
    # construction, which the previous version did not.
    return round(1.0 - (1.0 - p_win) * blowout, 4)


def _set_total_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    """PER-SET game total (Polymarket's "Set N O/U X.5" -- no Kalshi
    equivalent, market.side="set_N" carries which set this is, though the
    model itself doesn't currently vary by set number -- see
    game_lines_tennis.py::SET_GAMES_STD's own docstring on why only
    first-set data was fit)."""
    if match is None or market.line is None:
        return None
    elo_diff = _elo_diff(match.player_a_key, match.player_b_key, match.surface)
    if elo_diff is None:
        return None
    return round(game_lines_tennis.prob_over_set_games(market.line, elo_diff), 4)


def _total_sets_model_prob(market: Market, match: TennisMatch | None) -> float | None:
    """Whether the match goes the full distance in SET count (Polymarket's
    "Total Sets O/U X.5" -- no Kalshi equivalent). Derived for free from the
    same empirical exact-score table moneyline/exact_score already use."""
    if match is None or market.line is None:
        return None
    elo_diff = _elo_diff(match.player_a_key, match.player_b_key, match.surface)
    if elo_diff is None:
        return None
    p_a_moneyline = elo_service_tennis.get_match_win_prob(match.player_a_key, match.player_b_key, match.surface)
    if p_a_moneyline is None:
        return None
    best_of = _match_best_of(match)
    p_over = game_lines_tennis.prob_over_total_sets(elo_diff, best_of, p_a_moneyline, market.line)
    return round(p_over, 4) if p_over is not None else None


def _model_prob(m: Market, match: TennisMatch | None) -> float | None:
    if m.market_type == "moneyline":
        return _moneyline_model_prob(m, match)
    if m.market_type == "set_winner":
        return _set_winner_model_prob(m, match)
    if m.market_type == "game_spread":
        return _game_spread_model_prob(m, match)
    if m.market_type == "set_spread":
        return _set_spread_model_prob(m, match)
    if m.market_type == "game_total":
        return _game_total_model_prob(m, match)
    if m.market_type == "exact_score":
        return _exact_score_model_prob(m, match)
    if m.market_type == "set_total":
        return _set_total_model_prob(m, match)
    if m.market_type == "total_sets":
        return _total_sets_model_prob(m, match)
    return None


LIVE_TRADING_LOOKBACK = datetime.timedelta(hours=6)  # see ladder_sanity.py's own module comment for why 6, not 1


def _batch_recent_snapshots_for_live_check(session: Session, market_ids: list[int]) -> dict[int, list["MarketSnapshot"]]:
    """EVERY MarketSnapshot within the last LIVE_TRADING_LOOKBACK for each
    market_id, one query for the whole list, grouped in Python -- needed
    because `looks_already_live_by_trading` looks at the MAX/MIN price and
    volume across the whole window, not a single before/after pair (see its
    own docstring for why a single-snapshot comparison missed the real
    case this was built for). A flat `ts >= cutoff` filter, unlike
    markets.py's other batched-snapshot helpers, which all want exactly one
    row per market_id.

    Selects the THREE COLUMNS the callers read rather than whole entities. This
    window spans every tennis market over six hours -- 358,068 rows measured
    live -- and building an ORM instance for each cost 5.2s of a 12.6s response,
    enough to push the endpoint past the 18s the cross-sport page allows a sport
    before it drops it entirely (the reported "bets appear then disappear").
    Row objects still expose .market_id/.last_price/.volume, so callers are
    unchanged."""
    if not market_ids:
        return {}
    from app.db.models import MarketSnapshot

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


@router.get("/markets", response_model=list[TennisMarketOut])
def list_tennis_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "tennis", Market.market_type.in_(GAME_MARKET_TYPES)).all()
    match_ids = {m.tennis_match_id for m in markets if m.tennis_match_id}
    matches_by_id = {
        m.id: m for m in session.query(TennisMatch).filter(TennisMatch.id.in_(match_ids)).all()
    } if match_ids else {}

    def _match_already_decided(m: Market) -> bool:
        match = matches_by_id.get(m.tennis_match_id) if m.tennis_match_id else None
        return match is not None and match.winner_key is not None

    # REAL BUG this guards against (user-reported 2026-07-19: recommended
    # bets with market prices near 0%): TennisMatch.winner_key only gets set
    # by the slow offline tennisdata/tennisexplorer crawl, which can lag a
    # real-world match finishing by a day or more -- meaning a market whose
    # own platform has ALREADY finalized/closed it (Kalshi's real `status`
    # goes to "finalized"; confirmed live a Polymarket MARKET can be
    # individually closed=true even while its EVENT container stays
    # closed=false for months, see polymarket_tennis_client.py::
    # _market_status) would otherwise sail through with its last live price
    # frozen at $0.00/$1.00, producing a nonsensical giant "edge" against the
    # model's own live probability. Both upsert paths now store the platform's
    # own real per-market status (Kalshi always did; Polymarket's hardcoded
    # "active" was the actual bug), so this is a real, not guessed, signal.
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _match_already_started(m: Market) -> bool:
        """SAME fix as mlb_markets.py's `_game_already_started`, same
        reasoning: a market whose event has dropped off the platform's own
        "open" listing (match concluded, or Polymarket individually closed
        the market) simply stops getting fresh snapshots/status updates --
        its last stale price sits frozen in the DB indefinitely with no
        further signal to catch it. Once the match's own real, estimated
        start instant (see TennisMatch.estimated_start_time) is in the past,
        the pregame Elo model has no business being compared against
        whatever price is currently sitting there, decided or not --
        excluded regardless of winner_key. Rows without a real
        estimated_start_time yet (not resolved by either poller) fall
        through unaffected, same "don't guess" policy as everywhere else."""
        match = matches_by_id.get(m.tennis_match_id) if m.tennis_match_id else None
        if match is None or not match.estimated_start_time:
            return False
        try:
            start = datetime.datetime.fromisoformat(match.estimated_start_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return start < now_utc

    # NOTE: deliberately NOT using Market.updated_at as a "still open" signal
    # (tried and reverted 2026-07-19) -- SQLAlchemy's `onupdate` only fires
    # when the ORM detects an actual value change, and thin/illiquid tennis
    # markets routinely go many poll cycles with a literally unchanged price,
    # so `updated_at` stays frozen on plenty of genuinely-still-open rows too
    # (confirmed live: a 30-minute staleness cutoff wrongly hid ~370 real
    # open Kalshi moneyline rows in testing).
    #
    # FOURTH gap (2026-07-19, same real bug independently confirmed for MMA
    # -- see mma_markets.py's own version of this comment): status only
    # updates while the poller can still SEE the match -- our poller only
    # fetches each series' currently-OPEN listing, so a match that concludes
    # simply drops out of every future poll and its last-known status/price
    # sit frozen in the DB forever. Unlike `Market.updated_at`,
    # `MarketSnapshot.ts` IS a reliable "last confirmed still open" signal --
    # a fresh row gets INSERTED every poll cycle regardless of whether the
    # price moved (confirmed live: real snapshots ticking every ~5 minutes
    # for genuinely-open rows even with a completely unchanged price). A
    # match with no snapshot in the last 20 minutes (4 missed 5-minute
    # cycles' worth of slack) essentially certainly isn't live-listed by
    # either platform anymore.
    all_snapshots = _batch_latest_snapshots(session, [m.id for m in markets])

    # THIS IS MEASURED AGAINST THE FEED, NOT THE WALL CLOCK, and that is the
    # whole point. The wall-clock version was the cause of the user-reported
    # FLICKER: tennis matches vanishing from Recommended and reappearing minutes
    # later, repeatedly.
    #
    # The old comment above justified 20 minutes as "4 missed 5-minute cycles'
    # worth of slack". That premise is simply false in practice. Measured over
    # 6 hours of real snapshot history (41 write bursts): the tennis refresh
    # lands at a MEDIAN gap of 8 minutes, not 5, with a long tail -- real
    # observed gaps of 16, 17, 21 and 26 minutes. The refresh does six sequential
    # jobs over hundreds of events and routinely overruns its own 5-minute
    # interval, so the scheduler skips the overlapping run.
    #
    # Every gap past 20 minutes therefore tipped EVERY tennis market over the
    # staleness line at once, emptying the board until the next burst refilled
    # it. Nothing was wrong with the matches; the poll was just late.
    #
    # Comparing each market against the newest snapshot in the feed instead is
    # self-calibrating: when the whole poll runs late, everything shifts together
    # and nothing is dropped, while a single market that stops updating WHILE
    # its neighbours keep ticking -- the genuine "delisted, price frozen" case
    # this gate exists to catch -- still stands out immediately.
    STALE_BEHIND_FEED = datetime.timedelta(minutes=20)
    # If the feed itself dies, "behind the feed" would keep every frozen market
    # alive forever, so an absolute backstop still applies. Set well past the
    # worst observed gap so normal lateness never reaches it.
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

    # FIFTH gap (2026-07-19, user-reported): a match can be genuinely
    # IN PROGRESS -- not yet decided, not stale, status still "active" on
    # both platforms -- while `estimated_start_time` (the ONLY signal the
    # four checks above have) is simply WRONG for that specific match, same
    # root cause as the MMA occurrence_datetime bug but here the match
    # hasn't closed at all, just started early/differently than estimated.
    # Confirmed live: a real ITF match priced "Set 1 Over 8.5/9.5/10.5
    # games" ALL at 99.55% and "Set 2 Over 8.5/9.5/10.5" ALL at 0.45%
    # simultaneously -- a real, still-undecided pregame ladder never prices
    # different thresholds identically (Over 8.5 must be at least as likely
    # as Over 9.5), so two rungs converging on the same extreme value is a
    # structural tell the real outcome is already locked in, independent of
    # any timestamp this app stores. See ladder_sanity.py for the general
    # version of this check (shared with every other sport).
    ladder_groups: dict[tuple, list[tuple[float, float]]] = {}
    for m in markets:
        if m.line is None or m.tennis_match_id is None:
            continue
        snap = all_snapshots.get(m.id)
        implied = _implied_prob(snap)
        if implied is None:
            continue
        set_tag = m.side if m.market_type == "set_total" else None
        key = (m.tennis_match_id, m.market_type, m.team, set_tag)
        ladder_groups.setdefault(key, []).append((m.line, implied))
    match_ids_by_group = {key: key[0] for key in ladder_groups}
    resolved_group_keys = find_resolved_entities(ladder_groups)
    matches_with_resolved_ladder = {match_ids_by_group[key] for key in resolved_group_keys}

    def _match_ladder_resolved(m: Market) -> bool:
        return m.tennis_match_id in matches_with_resolved_ladder

    # SIXTH gap (2026-07-19, same day, user-reported): a real Kalshi ITF
    # moneyline (Thanaphat Boosarawongse vs Aniketh Venkataraman) was still
    # showing a live $6+ recommended bet at a 99%/1% price while the real
    # match was already final -- `estimated_start_time` said it hadn't even
    # started for another 3+ hours, so none of the five checks above fired
    # (not decided, not "started" by the wrong schedule, no ladder to
    # compare against for moneyline, status still "active", snapshots still
    # arriving every ~5 minutes so not stale). See
    # ladder_sanity.py::looks_already_live_by_trading for the real
    # validation behind this (Kalshi-only for now -- Polymarket's volume
    # scale hasn't been separately checked).
    recent_snapshots_for_live_check = _batch_recent_snapshots_for_live_check(session, [m.id for m in markets])

    def _market_looks_live_by_trading(m: Market) -> bool:
        if m.source != "kalshi":
            return False
        current = all_snapshots.get(m.id)
        current_price = current.last_price if current else None
        recent = recent_snapshots_for_live_check.get(m.id, [])
        return looks_already_live_by_trading(current_price, [(s.last_price, s.volume) for s in recent])

    matches_live_by_trading = {m.tennis_match_id for m in markets if m.tennis_match_id and _market_looks_live_by_trading(m)}

    # SEVENTH gap (2026-08-03, user-reported): Firman vs Vladson, a Kalshi ITF
    # women's moneyline. Kalshi had it status=finalized (result Vladson) with
    # prices 0.01/0.99 on 33k/41k volume, while this app still recommended it --
    # estimated_start_time claimed the match started ~4 hours AFTER it had
    # actually finished, no winner was scraped yet, and market_cleanup keys on
    # the ticker date being in the PAST so a same-day match cannot trip it until
    # tomorrow. The two checks above both miss it by construction: a moneyline
    # has no ladder rungs, and a price pinned near 0.01 for hours shows no recent
    # swing. Grouping the two SIDES of the same moneyline gives the missing
    # signal -- see ladder_sanity.pair_looks_resolved.
    moneyline_sides: dict[int, list[tuple[float | None, float | None]]] = {}
    for m in markets:
        if m.market_type != "moneyline" or m.tennis_match_id is None:
            continue
        snap = all_snapshots.get(m.id)
        if snap is None:
            continue
        moneyline_sides.setdefault(m.tennis_match_id, []).append(
            (_implied_prob(snap), snap.volume)
        )
    matches_pair_resolved = {
        mid for mid, sides in moneyline_sides.items() if pair_looks_resolved(sides)
    }

    # NINTH gap -- see ladder_sanity.pair_looks_live_by_travel for the reported
    # case (Hanttu vs Roots) and why price TRAVEL, not price level, is what
    # separates a live match from a genuine lopsided favourite. Reuses the
    # already-loaded live-check window rather than querying again.
    moneyline_travel: dict[int, list[tuple[float | None, float | None, float | None]]] = {}
    moneyline_surge: dict[int, list[tuple[float | None, float | None, float | None]]] = {}
    for m in markets:
        if m.market_type != "moneyline" or m.tennis_match_id is None:
            continue
        snap = all_snapshots.get(m.id)
        if snap is None:
            continue
        window = recent_snapshots_for_live_check.get(m.id, [])
        # 0.0 is "never traded", not a real quote -- counting it would invent a
        # full-scale swing for a market that simply had no early trades.
        seen = [s.last_price for s in window if s.last_price]
        swing = (max(seen) - min(seen)) if len(seen) > 1 else 0.0
        moneyline_travel.setdefault(m.tennis_match_id, []).append(
            (_implied_prob(snap), snap.volume, swing)
        )
        vols = [s.volume for s in window if s.volume is not None]
        moneyline_surge.setdefault(m.tennis_match_id, []).append(
            (swing, max(vols) if vols else None, min(vols) if vols else None)
        )
    matches_live_by_travel = {
        mid for mid, sides in moneyline_travel.items() if pair_looks_live_by_travel(sides)
    }
    # Second, price-BLIND arm -- see ladder_sanity.pair_looks_live_by_surge for
    # the reported Ovcharenko vs Broadus case. The travel rule above needs the
    # two sides at opposite extremes, so a live match that is still CLOSE slips
    # past it; a ten-fold volume jump on a real base does not.
    matches_live_by_surge = {
        mid for mid, sides in moneyline_surge.items() if pair_looks_live_by_surge(sides)
    }

    def _match_pair_resolved(m: Market) -> bool:
        return (m.tennis_match_id in matches_pair_resolved
                or m.tennis_match_id in matches_live_by_travel
                or m.tennis_match_id in matches_live_by_surge)

    # EIGHTH gap (2026-08-03, user-reported: Toby Martin, Kayla Day set 1, and
    # Miriam vs Calista Liu all recommended while already under way or finished).
    #
    # Every gate above ultimately trusts estimated_start_time, and for tennis that
    # field runs LATE -- Kalshi's occurrence_datetime is a scheduled estimate it
    # never revises. So mid-match the app believes the match has not started,
    # while Kalshi keeps quoting it (snapshots 3 minutes old, status active), so
    # it is neither stale nor decided nor ladder-resolved. Kayla Day vs Diane
    # Parry: est_start 2026-08-03T17:00Z, match_date 2026-08-02, no winner
    # scraped, snapshot 3 minutes old.
    #
    # match_date comes from a DIFFERENT source (tennisexplorer) and is the only
    # independent signal available. A match whose own date is already past is not
    # an upcoming bet, whatever the start estimate claims. Deliberately
    # conservative: match_date can itself be a day early, so this may drop a few
    # genuinely upcoming matches. That is the right direction to err -- a missed
    # bet costs nothing, a bet placed into a live or finished match is exactly
    # what must never happen.
    today = now_utc.date()

    # A match does not run for half a day; an expiry this far past the stated
    # start means the start was never revised (see _start_time_untrusted).
    _RESCHEDULE_GAP = datetime.timedelta(hours=10)

    def _start_time_untrusted(m: Market) -> bool:
        """True when this match's stored start cannot be believed.

        Kalshi NEVER revises occurrence_datetime (what we store as
        estimated_start_time) when a match is rescheduled, but it DOES revise
        expected_expiration_time. Verified on Fritz vs Jodar: occurrence stuck at
        2026-08-02T21:30Z while the expiration had moved to 2026-08-03T21:50Z.

        So a DATE disagreement between the two means the start is stale, and every
        "has it started?" check above is reasoning from a wrong number. Those get
        the conservative treatment. When the two agree -- the common case -- the
        start is trustworthy and the match is NOT dropped, which is the whole
        point: the earlier blunt version keyed on match_date alone and threw away
        genuinely upcoming matches.

        Falls back to the match_date check only when there is no expiration to
        compare against, so behaviour never gets *less* safe than before.
        """
        match = matches_by_id.get(m.tennis_match_id) if m.tennis_match_id else None
        if match is None:
            return False
        start, expiry = match.estimated_start_time, match.expected_expiration_time
        if start and expiry:
            try:
                s_dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
                e_dt = datetime.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            except ValueError:
                return False
            # Compare ELAPSED TIME, not calendar dates. Measured over 125 matches
            # carrying both fields: the median gap is 0.0h (Kalshi normally sets
            # expiration equal to occurrence) and only 6 exceed 6h -- those are
            # the real reschedules. A date comparison instead flagged 6 matches
            # that merely start late in the UTC day and expire after midnight,
            # and its "expiry date has passed" arm excluded every remaining
            # match outright, emptying the tennis list completely.
            if (e_dt - s_dt) > _RESCHEDULE_GAP:
                return True   # expiry far beyond the start: the start is stale
            # Start is trustworthy -- say nothing here and let
            # _match_already_started apply the normal instant comparison.
            return False
        # No expiration to cross-check -- fall back to the independent date.
        if not match.match_date:
            return False
        try:
            return datetime.date.fromisoformat(match.match_date[:10]) < today
        except ValueError:
            return False

    def _match_looks_live_by_trading(m: Market) -> bool:
        return m.tennis_match_id in matches_live_by_trading

    # EIGHTH gap, and the first one backed by a POSITIVE in-play signal rather
    # than an inference from a timestamp. Kalshi and Polymarket cannot report
    # that a match has started -- every candidate field was tested and failed
    # (see flashscore_tennis_client's docstring) -- so all seven checks above
    # ultimately reason from estimated_start_time, which for a rescheduled match
    # was measured wrong on 38/38 tour matches by a median of 26 hours.
    #
    # Flashscore publishes an explicit status, so this hides a match only when a
    # real source says it is in play. It is deliberately one-directional: a match
    # the feed does not know about, or any feed failure at all, leaves everything
    # exactly as it is. That is the guarantee that matters here -- the app can
    # miss hiding a started match, but it cannot hide a match that has not
    # started, which is the failure that costs a real bet.
    live_pairs = _flashscore_live_pairs()
    live_match_ids = {
        mid for mid, match in matches_by_id.items()
        if match.player_a_key and match.player_b_key
        and frozenset((match.player_a_key, match.player_b_key)) in live_pairs
    } if live_pairs else set()

    def _match_live_on_flashscore(m: Market) -> bool:
        return m.tennis_match_id in live_match_ids

    markets = [
        m for m in markets
        if not _match_already_decided(m)
        and not _match_already_started(m)
        and not _match_ladder_resolved(m)
        and not _match_looks_live_by_trading(m)
        and not _match_live_on_flashscore(m)
        and not _match_pair_resolved(m)
        and not _start_time_untrusted(m)
        and (m.status or "active") == "active"
        and not _market_stale(m)
    ]
    # Hoisted: as an inline set literal this was rebuilt once per
    # all_snapshots entry -- quadratic, and the dominant cost of the
    # tennis endpoint at 34k markets (183M attribute reads, ~40s).
    _kept_market_ids = {m.id for m in markets}
    snapshots_by_market = {mid: s for mid, s in all_snapshots.items() if mid in _kept_market_ids}
    weekly_pool, futures_pool = get_tennis_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    def _either_player_unrated(match: TennisMatch | None) -> bool:
        if match is None:
            return False
        return (
            elo_service_tennis.get_player_match_count(match.player_a_key) == 0
            or elo_service_tennis.get_player_match_count(match.player_b_key) == 0
        )

    out = []
    for m in markets:
        match = matches_by_id.get(m.tennis_match_id) if m.tennis_match_id else None
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        if _either_player_unrated(match):
            model_prob = None
            no_baseline_reason = NO_HISTORY_REASON
        else:
            model_prob = _model_prob(m, match)
            no_baseline_reason = None if model_prob is not None else NO_BASELINE_REASON
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "tennis", m.market_type)
        stake_dollars = size_stake_dollars(staking_mode, kelly, weekly_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full)
        out.append(
            TennisMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                line=m.line,
                side=m.side,
                match_label=f"{match.player_a_name} vs {match.player_b_name}" if match else None,
                tennis_match_id=m.tennis_match_id,
                tour=match.tour if match else None,
                tier=match.tier if match else None,
                match_date=match.match_date if match else None,
                surface=match.surface if match else None,
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
            )
        )
    out.sort(key=lambda m: (m.match_date or "9999", m.match_label or ""))
    return out


# Real, freely-scrapable draw data (tennisexplorer.com) is fetched fresh at
# request time -- this cache exists only to stop a page reload/refetch from
# re-scraping the same tournament's draw repeatedly within a short window
# (a real draw changes at most a few times a day, well within a TTL this
# short). Module-level, same "cheap in-process cache" pattern as MLB's
# BatterOpsCache.
# Flashscore's in-play flag, cached off the request path. Short TTL because this
# is a SAFETY decision: a match going live is exactly the event we need to react
# to quickly, and the feed is 5 cheap requests.
_LIVE_CACHE_TTL_SECONDS = 60
_live_cache: dict[str, object] = {"at": None, "data": frozenset()}


def _flashscore_live_pairs() -> frozenset:
    """Pairs Flashscore positively reports as IN PLAY.

    FAILS OPEN, and that is the whole design. On any error -- including the
    x-fsign token rotating and every request 4xx-ing -- this returns the last
    good set, or an EMPTY set before the first success. An empty set hides
    nothing, so a dead feed degrades to exactly today's behaviour rather than
    blanking the board or hiding genuinely upcoming matches.
    """
    import time

    now = time.monotonic()
    at = _live_cache["at"]
    if isinstance(at, float) and now - at < _LIVE_CACHE_TTL_SECONDS:
        return _live_cache["data"]  # type: ignore[return-value]
    try:
        fresh = frozenset(flashscore_tennis_client.get_live_pairs())
    except Exception:
        return _live_cache["data"]  # type: ignore[return-value]
    # Only advance the cache on a real answer. An empty result is indistinguishable
    # from "feed down", so it must not overwrite a good set and un-hide live matches.
    if fresh:
        _live_cache["at"], _live_cache["data"] = now, fresh
    return _live_cache["data"]  # type: ignore[return-value]


_DRAW_CACHE_TTL_SECONDS = 600
_draw_cache: dict[tuple[str, str], tuple[float, tuple[list[list[str]] | None, str | None]]] = {}


def _get_tournament_draw(slug: str, tour: str) -> tuple[list[list[str]] | None, str | None]:
    import time

    cache_key = (slug, tour)
    cached = _draw_cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _DRAW_CACHE_TTL_SECONDS:
        return cached[1]
    tour_suffix = "atp-men" if tour == "atp" else "wta-women"
    try:
        with TennisExplorerClient() as client:
            result = client.get_tournament_draw(slug, 2026, tour_suffix)
    except Exception:
        # REAL BUG this avoids (caught live 2026-07-19): a single transient
        # fetch failure (network hiccup, tennisexplorer momentarily slow)
        # used to get cached for the FULL 10-minute TTL, silently blanking
        # every tournament's model_prob for that whole window even though a
        # retry moments later would have succeeded (confirmed: the exact
        # same tournament that returned nothing here worked immediately in
        # a fresh, uncached call). Don't cache a failure -- only a real,
        # successfully-fetched draw is worth remembering.
        return None, None
    _draw_cache[cache_key] = (now, result)
    return result


def _match_player_to_sim(team_full_name: str, sim_result: dict[str, float]) -> float | None:
    """Sim results are keyed on the draw's bare surname (e.g. "Van De
    Zandschulp"); Market.team is the real full name Kalshi gives us (e.g.
    "Botic Van De Zandschulp") -- same suffix-match resolution this app
    already uses for MMA/Tennis partial-name markets."""
    for surname, prob in sim_result.items():
        if team_full_name.lower().endswith(surname.lower()):
            return prob
    return None


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_tennis_futures(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(
        Market.sport == "tennis", Market.market_type == "tournament_winner", Market.status == "active"
    ).all()
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    weekly_pool, futures_pool = get_tennis_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    # REAL BUG fixed here (2026-07-19): grouping by `group_label` alone
    # silently merged the Men's AND Women's US Open into one blob --
    # Kalshi's own `competition` text for both is the bare "US Open", no
    # gender marker at all (unlike every other tournament, which is prefixed
    # "ATP "/"WTA "), confirmed live once the real Women's US Open market
    # appeared (Men's + Women's had 56 combined rows under one group_label,
    # spanning both `side` values). `(group_label, tour)` is the real unique
    # tournament identity.
    by_tournament: dict[tuple[str, str], list[Market]] = {}
    for m in markets:
        by_tournament.setdefault((m.group_label or "", m.side or "atp"), []).append(m)

    sim_by_tournament: dict[tuple[str, str], dict[str, float] | None] = {}
    for (group_label, tour), group_markets in by_tournament.items():
        slug = tournament_name_to_slug(group_label)
        rounds, surface = _get_tournament_draw(slug, tour)
        sim_by_tournament[(group_label, tour)] = simulate_tournament(rounds, surface=surface) if rounds else None

    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        sim_result = sim_by_tournament.get((m.group_label or "", m.side or "atp"))
        model_prob = _match_player_to_sim(m.team, sim_result) if (sim_result and m.team) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "tennis", m.market_type)
        stake_dollars = size_stake_dollars(staking_mode, kelly, futures_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=FUTURES_UNIT_SCALE)
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        out.append(
            FuturesMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                group_label=m.group_label,
                line=m.line,
                side=None,  # `side` is repurposed internally to carry tour (atp/wta), not a real display side -- see market_catalog_tennis.py's upsert
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
def get_tennis_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport != "tennis":
        raise HTTPException(404, "market not found")
    match = session.get(TennisMatch, m.tennis_match_id) if m.tennis_match_id else None
    label = f"{match.player_a_name} vs {match.player_b_name}" if match else (m.group_label or m.market_type)
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    if m.market_type == "moneyline":
        caveats = [
            "model_validated: false -- backtested fresh in this app's own harness across tour/challenger/ITF "
            "tiers using tennis-data.co.uk + tennisexplorer.com's real historical odds; the market beat this "
            "Elo baseline at every tier tested (see Backtests)."
        ]
        methodology = (
            "Surface-blended walk-forward Elo (dynamic K, the standard published tennis-Elo design) -- "
            "overall rating blended with a surface-specific rating, weighted by how much surface experience "
            "the player has. No market/style/physical-attribute information."
        )
    else:
        caveats = [
            "model_validated: false -- this market type has not been backtested against real historical "
            "odds (neither tennis-data.co.uk nor tennisexplorer.com carries historical prices for set "
            "winner/game spread/total/exact score), unlike moneyline. The underlying per-set/per-game "
            "constants ARE real, regressed from 491,775 matches with actual set-by-set scores -- see "
            "scripts/derive_tennis_game_line_constants.py -- but the full pricing model itself is an "
            "honest reference estimate, not a validated edge."
        ]
        methodology = {
            "set_winner": "Per-set win probability, logistic regression fit against walk-forward Elo diff across every real set outcome in this app's merged match cache.",
            "set_spread": "Set handicap (+/-1.5 sets) read directly off the empirical exact-scoreline distribution -- P(win by 2+ sets), scaled by the match win probability. Verified against 120 resolved markets on real 3-set matches: the set reading was correct 120/120, the games reading 73/120.",
            "game_spread": "Game differential (games won by each player) regressed against Elo diff, Normal-approximation cover probability -- same shape as this app's NFL/NBA/MLB spread models.",
            "game_total": "Total match games regressed against |Elo diff|, split by best-of-3 vs best-of-5 (a Bo5 match averages ~36 games vs Bo3's ~23 -- pooling these would bias every Grand Slam prediction), Normal-approximation over probability.",
            "exact_score": "Empirical scoreline frequency, bucketed by the favorite's own win-probability decile and best_of -- a direct nonparametric fit from real historical scorelines, not derived from an independent-sets assumption (checked separately and found to make moneyline WORSE when forced that way, see Backtests).",
            "set_total": "Games within a single set regressed against |Elo diff| -- fit on real first-set data only (resid_std=2.03 games, much tighter than a full match's ~7-9 given far less accumulated variance in one set), Normal-approximation over probability. No Kalshi equivalent -- Polymarket-only market.",
            "total_sets": "Whether the match goes the full distance in set count -- derived for free from the same empirical scoreline table exact_score uses (sums every real scoreline whose total set count exceeds the line), not a separately-fit constant. No Kalshi equivalent -- Polymarket-only market.",
        }.get(m.market_type, "No detailed methodology available.")
    insight = ""

    if match is not None:
        a_rating = elo_service_tennis.get_player_rating(match.player_a_key, match.surface)
        b_rating = elo_service_tennis.get_player_rating(match.player_b_key, match.surface)
        if a_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.player_a_name} Elo rating", detail=f"{a_rating:.0f}"))
        if b_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.player_b_name} Elo rating", detail=f"{b_rating:.0f}"))
        if match.surface:
            factors.append(ReasoningFactorOut(label="Surface", detail=match.surface))
        factors.append(ReasoningFactorOut(label="Tier", detail=match.tier))
        if m.market_type == "set_winner" and m.line is not None:
            factors.append(ReasoningFactorOut(label="Set", detail=str(int(m.line))))
        elif m.market_type == "set_spread" and m.line is not None:
            factors.append(ReasoningFactorOut(label="Set handicap", detail=f"{m.line:+g} sets"))
        elif m.market_type == "game_spread" and m.line is not None:
            factors.append(ReasoningFactorOut(label="Line", detail=f"{m.team} {'+' if m.line >= 0 else ''}{m.line} games"))
        elif m.market_type == "game_total" and m.line is not None:
            factors.append(ReasoningFactorOut(label="Line", detail=f"O/U {m.line} games"))
        elif m.market_type == "exact_score" and m.side:
            factors.append(ReasoningFactorOut(label="Scoreline", detail=f"{m.team} wins {m.side}"))
        elif m.market_type == "set_total" and m.line is not None:
            set_label = m.side.replace("set_", "Set ") if m.side else "?"
            factors.append(ReasoningFactorOut(label="Line", detail=f"{set_label} O/U {m.line} games"))
        elif m.market_type == "total_sets" and m.line is not None:
            factors.append(ReasoningFactorOut(label="Line", detail=f"O/U {m.line} sets"))
        if a_rating is not None and b_rating is not None:
            gap = a_rating - b_rating
            tseed = f"{match.player_a_name}|{match.player_b_name}|{a_rating}|{b_rating}"
            surf = f" on {match.surface}" if getattr(match, "surface", None) else ""
            if abs(gap) < 30:
                insight = _seeded_choice(tseed, [
                    f"By win/loss history alone this is a genuine toss-up -- surface-blended Elo has {match.player_a_name} and {match.player_b_name} nearly even ({a_rating:.0f} to {b_rating:.0f}). ",
                    f"There's little between these two{surf}: the surface-blended ratings put {match.player_a_name} and {match.player_b_name} almost level ({a_rating:.0f} to {b_rating:.0f}). ",
                    f"On form this projects close, with {match.player_a_name} and {match.player_b_name} sitting near even on surface-blended Elo ({a_rating:.0f} to {b_rating:.0f}). ",
                ])
            else:
                stronger, s_r, weaker, w_r = (
                    (match.player_a_name, a_rating, match.player_b_name, b_rating) if gap > 0
                    else (match.player_b_name, b_rating, match.player_a_name, a_rating)
                )
                insight = _seeded_choice(tseed, [
                    f"{stronger} comes in as the stronger player on surface-blended Elo{surf}, clear of {weaker} ({s_r:.0f} to {w_r:.0f}). ",
                    f"The ratings prefer {stronger} here{surf}, sitting well above {weaker} ({s_r:.0f} to {w_r:.0f}). ",
                    f"Surface-blended Elo gives {stronger} the edge{surf}, ahead of {weaker} ({s_r:.0f} to {w_r:.0f}). ",
                ])

    if not insight and m.market_type == "tournament_winner":
        player = m.team or (m.group_label or "this player")
        event = m.group_label or "the draw"
        insight = _seeded_choice(f"{player}|tenntw", [
            f"This is the outright for {player}. It's priced by simulating the actual scraped draw thousands of times -- each match decided by the players' surface-blended Elo -- and counting how often {player} lifts the trophy. ",
            f"{player}'s title price comes from a bracket Monte Carlo off the real draw: play every round out thousands of times on surface-blended Elo and tally how often {player} is the last one standing. ",
            f"Priced from a simulated run of {event}: {player} is carried through the bracket thousands of times on Elo-based match odds, and the share of wins is this number. ",
        ])

    insight += _edge_sentence(model_prob, market_prob)

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
