"""LoL markets API -- parallel to routers/valorant_markets.py.

Real live inventory here (confirmed 2026-07-19, see kalshi_lol_client.py) is
map winner + total maps played -- no series (whole-match) winner Kalshi
ticker exists for LoL (unlike CS2's KXCS2GAME) and no Polymarket match-level
market type exists at all.

Ratings are trained on a real historical Leaguepedia crawl (5,604 matches,
Leaguepedia's own "Primary" tournament tier -- LCK/LPL/LEC/LCS-LTA/Worlds/
MSI, 2023-mid 2026 -- see scripts/build_lol_match_cache.py) plus this app's
own live-polled match history on top (see elo_service_lol.py). K=36 is
grid-searched against that real data (scripts/derive_lol_elo_constants.py --
67.13% walk-forward accuracy post-warmup, the strongest of all 3 esports
titles in this app). model_validated is still False for every market_type
here -- a real market-odds backtest against Kalshi's own historical trade
data now exists too (scripts/backtest_lol_market_odds.py, Map 1 only,
12-match sample) and found the market beats the model, same conclusion
every sport in this app has found.
"""
import datetime
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice
from app.api.routers.settings import get_lol_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import FuturesMarketOut, LolMarketOut, ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.clients import flashscore_esports_client
from app.db.chunked import fetch_in_chunks
from app.models.duplicate_fixtures import canonical_fixture_ids
from app.db.models import LolMatch, Market, MarketSnapshot
from app.ingestion import market_catalog_lol
from app.ingestion.market_matcher_lol import team_names_match
from app.models.baseline import elo_service_lol
from app.models.esports_tournament_pricing import is_competition_outcome, price_tournament_winners
from app.models.ladder_sanity import futures_group_decided, ESPORTS_LIVE_TRADING_MIN_PRICE_SWING, LOL_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA, looks_already_live_by_trading
from app.models.esports_start_time import borrowed_start_times, corrected_start_time
from app.models.staking import FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

_NO_BASELINE_METHODOLOGY = "No detailed methodology available for this market type yet -- see the module docstring above."

# Map markets are priced but NEVER STAKED.
#
# The model has no map-specific view at all: prob_map_n_win_a takes a map number
# and uses it only to bounds-check, returning the SAME per-map probability for
# every map in the series. Verified live -- across 84 LoL matches pricing a team
# on multiple maps, every one had exactly ONE distinct model probability.
#
# The market plainly models something we do not. BoostGate vs SU Esports, a Bo3
# that had not been played: our model said 35.55% for map 1 AND map 2, while
# Kalshi said 39.0%/16.5% and Polymarket 24.5%/15.0% -- both venues independently
# pricing map 2 far below map 1 for the same team. Whatever that structure is
# (side selection, draft, map order), we do not represent it, so an "edge" here
# measures our blind spot rather than an advantage.
#
# The settled record cannot settle the question either way: filtered to bets that
# were actually tradeable and cleared the 10pp gate, LoL is +5.5% on n=26, CS2
# +8.4% on n=7, Valorant -100% on n=6. Those samples are noise, and the headline
# paper numbers (+12% to +22%) come almost entirely from untradeable rows.
#
# So they stay PRICED and VISIBLE -- that is what keeps calibration data
# accruing so the question becomes answerable -- and carry no stake, the same
# posture as the esports tournament futures and the player-stat projections.
MAP_MARKET_NOTE = "no map-specific model (same probability every map) - tracking only, not staked"


router = APIRouter(prefix="/lol", tags=["lol"])

GAME_MARKET_TYPES = {"map_winner", "series_total", "series_winner"}

NO_BASELINE_REASON = (
    "No baseline yet -- this market's model is still being built and validated against this app's "
    "own historical data, not shipped as a guessed number."
)

NON_COMPETITION_REASON = (
    "Not priced: this is not a team-competition result. Polymarket lists it under the same "
    "market type as a tournament winner, but the question is a franchise/partnership slot, a "
    "roster or transfer announcement, an individual player feat, a soloqueue ladder or a "
    "novelty stat -- none of which a match-history model can speak to. Left unpriced on "
    "purpose rather than scored by a bracket simulator that would answer a different question."
)

COLD_START_CAVEAT = (
    "Ratings are trained on a real historical Leaguepedia crawl (5,604 matches, Primary tier only -- "
    "LCK/LPL/LEC/LCS-LTA/Worlds/MSI) plus this app's own live-polled matches on top -- 67.13% "
    "walk-forward accuracy post-warmup, beats the naive 0.5 baseline. A real market-odds backtest "
    "against Kalshi's own historical trade data (Map 1 only, 12-match sample) found the market beats "
    "the model, so model_validated stays false regardless."
)


def _team_side(match: LolMatch | None, team_name: str | None) -> str | None:
    if match is None or not team_name:
        return None
    if team_names_match(team_name, match.team_a):
        return "team_a"
    if team_names_match(team_name, match.team_b):
        return "team_b"
    return None


def _game_model_prob(m: Market, match: LolMatch | None) -> float | None:
    if match is None or not match.best_of:
        return None
    dist = elo_service_lol.get_series_distribution(
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
    # REAL COVERAGE GAP this closes (found live 2026-07-20, via
    # catalog_scan.py's newly-added esports coverage): KXLOLGAME is a real
    # whole-match/series winner Kalshi ticker this app never queried at all
    # (see kalshi_lol_client.py's own real-bug note) -- same model dispatch
    # as CS2/Valorant's own series_winner handling.
    if m.market_type == "series_winner":
        p = dist.prob_series_win_a() if side == "team_a" else dist.prob_series_win_b()
        return round(p, 4)
    return None


# Kalshi files a lot of things under LoL "tournament_winner" that are not team
# brackets at all. Measured 2026-08-02 across 605 active rows:
#
#     207  "Player to Penta"        -- a PLAYER prop, not a team outcome
#      70  "Solo Q Challenge"       -- a solo-queue ladder, not team play
#      56  "Team to Qualify for Worlds" -- season-long qualification, not a bracket
#      40  "TFT Set 17 ..."         -- Teamfight Tactics, a DIFFERENT GAME
#      25  "Global Power Rankings"  -- a published ranking, not a result
#
# Showing all 605 on the Futures page buried the ~86 rows that are genuinely
# split/season winners, and made LoL look like it had far more futures coverage
# than it does. This filters the VIEW only -- market_type is left alone on
# purpose, since rewriting it would move rows between CLV buckets and change what
# settlement expects, for no gain here.
# "Team to Make Grand Finals" is a DIFFERENT QUESTION from "wins the bracket":
# two teams reach a final, so its probabilities must sum to ~2.0 across the
# field, not 1.0. price_tournament_winners answers P(wins), so pointing it at
# these would understate every team by roughly half and manufacture a large
# negative edge on exactly the rows a user would notice. Excluded from pricing
# until a reach-the-final variant exists; they still LIST, just unpriced.
_REACH_FINAL_FUTURES = re.compile(r"make\s+grand\s+final|reach\s+.*final", re.IGNORECASE)


def _is_win_bracket_future(m: Market) -> bool:
    """Only the rows the win-the-bracket sim actually answers."""
    blob = f"{m.group_label or ''} {m.team or ''}"
    return _is_bracket_future(m) and not _REACH_FINAL_FUTURES.search(blob)


_NON_BRACKET_FUTURES = re.compile(
    r"penta|solo\s*q|soloq|tft|tacticians|power\s*rank|qualify|qualifi|shortest",
    re.IGNORECASE,
)


def _is_bracket_future(m: Market) -> bool:
    blob = f"{m.group_label or ''} {m.team or ''}"
    return not _NON_BRACKET_FUTURES.search(blob)


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_lol_futures(session: Session = Depends(get_session)):
    """See cs2_markets.py::list_cs2_futures's own docstring -- same real
    inventory-with-no-model shape, LoL's own version."""
    markets = session.query(Market).filter(Market.sport == "lol", Market.market_type == "tournament_winner", Market.status == "active").all()
    markets = [m for m in markets if _is_bracket_future(m)]
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    # Identify groups whose tournament is already won. Kalshi still reports every
    # leg `active` long after the event decides, so status alone cannot catch it --
    # the BLAST Bounty 2026 Season 2 Finals had MOUZ at 0.995 with 31 dead legs
    # still listed and $5 stakes recommended on 0.5% longshots.
    _by_group = {}
    for _m in markets:
        _p = _implied_prob(snapshots_by_market.get(_m.id))
        _by_group.setdefault(_m.group_label or "", []).append(_p)
    _decided = {g for g, ps in _by_group.items() if futures_group_decided("tournament_winner", ps)}
    # KEPT, not dropped. Dropping made a settled future silently disappear from
    # the page -- the user's own report was a champion market vanishing rather
    # than showing as finished. They are flagged instead, never staked below,
    # and the UI files them under a separate Settled section.
    _winner_by_group = {}
    for _m in markets:
        g = _m.group_label or ""
        if g not in _decided:
            continue
        _p = _implied_prob(snapshots_by_market.get(_m.id))
        if _p is not None and _p >= 0.5 and _m.team:
            _winner_by_group[g] = _m.team

    # Priced by the SAME Elo-seeded bracket Monte Carlo that already prices CS2
    # and Valorant tournament winners -- LoL was left unpriced not because the
    # model didn't fit but because 446 non-bracket rows (player props, TFT,
    # solo-queue) made the inventory look unmodellable. With those filtered out,
    # every team in every field is rated (103/103 checked across the ten largest
    # fields), so the existing sim applies unchanged.
    priced = price_tournament_winners([m for m in markets if _is_win_bracket_future(m)], elo_service_lol)
    _weekly, _futures_pool = get_lol_pool_dollars(session)
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
            _clv, "lol", m.market_type,
        )
        _stake = size_stake_dollars(_mode, _kelly, _futures_pool, model_prob, implied, _unit, _fm, _ff,
                                    unit_scale=FUTURES_UNIT_SCALE, min_market_price=FUTURES_MIN_MARKET_PRICE, sport="lol", team=m.team)
        # A decided group is shown for the record, never sized. The prior
        # behaviour dropped these rows entirely, which is why a settled
        # future appeared to vanish rather than read as finished.
        _settled = (m.group_label or "") in _decided
        if _settled:
            _kelly = None
            _stake = None
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
                stake_pool=None if _settled else "futures",
                line_move_pp=None,
                group_settled=_settled,
                group_winner=_winner_by_group.get(m.group_label or "") if _settled else None,
                # Explain the blank rather than leaving it mysterious: a user who
                # sees an empty model column has no way to tell "not modelled
                # yet" from "deliberately out of scope". LoL carries the most of
                # these -- pentakills, soloqueue ladders, roster-change news and
                # shortest-game props all arrive as tournament_winner rows.
                model_note=(
                    None if model_prob is not None
                    else NON_COMPETITION_REASON if not is_competition_outcome(m.group_label)
                    else None
                ),
            )
        )
    out.sort(key=lambda m: (m.group_label or "", m.team or ""))
    return out


@router.get("/markets", response_model=list[LolMarketOut])
def list_lol_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "lol", Market.market_type.in_(GAME_MARKET_TYPES | {"tournament_winner"})).all()
    match_ids = {m.lol_match_id for m in markets if m.lol_match_id}
    matches_by_id = {mt.id: mt for mt in session.query(LolMatch).filter(LolMatch.id.in_(match_ids)).all()} if match_ids else {}

    def _match_already_decided(m: Market) -> bool:
        match = matches_by_id.get(m.lol_match_id) if m.lol_match_id else None
        return match is not None and match.winner is not None

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _match_already_started(m: Market) -> bool:
        match = matches_by_id.get(m.lol_match_id) if m.lol_match_id else None
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
    # sport reached 35 minutes against a 20-minute threshold, so every
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
    # Leaguepedia's Cargo API has actually populated a real
    # estimated_start_time, which lags behind Kalshi's own live trading --
    # this catches the case where that hasn't happened yet but the market's
    # own price/volume history already makes clear the series is live or
    # over.
    LIVE_TRADING_LOOKBACK = datetime.timedelta(hours=6)  # see ladder_sanity.py's own module comment for why 6, not 1
    cutoff = datetime.datetime.utcnow() - LIVE_TRADING_LOOKBACK
    recent_rows = fetch_in_chunks(
        [m.id for m in markets],
        lambda chunk: (
            session.query(
                MarketSnapshot.market_id, MarketSnapshot.last_price, MarketSnapshot.volume
            )
            .filter(MarketSnapshot.market_id.in_(chunk), MarketSnapshot.ts >= cutoff)
            .all()
        ),
    )
    recent_snapshots_by_market: dict[int, list[MarketSnapshot]] = {}
    for snap in recent_rows:
        recent_snapshots_by_market.setdefault(snap.market_id, []).append(snap)

    def _market_looks_live_by_trading(m: Market) -> bool:
        if m.source != "kalshi":
            return False  # no real LoL Polymarket inventory to calibrate against -- see market_catalog_lol.py
        current = all_snapshots.get(m.id)
        current_price = current.last_price if current else None
        recent = recent_snapshots_by_market.get(m.id, [])
        return looks_already_live_by_trading(
            current_price, [(s.last_price, s.volume) for s in recent],
            min_volume_delta=LOL_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA,
            min_price_swing=ESPORTS_LIVE_TRADING_MIN_PRICE_SWING,
        )

    matches_live_by_trading = {m.lol_match_id for m in markets if m.lol_match_id and _market_looks_live_by_trading(m)}

    def _match_looks_live_by_trading(m: Market) -> bool:
        return m.lol_match_id in matches_live_by_trading

    # POSITIVE in-play/finished signal, the only gate here not inferred from a
    # timestamp or a price. Kalshi's start times for esports are demonstrably
    # wrong (the reported DRX case really began 4h before its recorded start),
    # and a result may never arrive for a team whose sponsor name our results
    # source does not know. See flashscore_esports_client for the measured
    # coverage (modest) and for why it still ships: it is ONE-DIRECTIONAL and
    # fails open, so it can only ever hide a match a real source reports as
    # live or over, never one that is genuinely upcoming.
    _fs_states = flashscore_esports_client.cached_match_states("lol")
    _fs_hidden = {
        mid for mid, match in matches_by_id.items()
        if flashscore_esports_client.hides_match(
            _fs_states, match.team_a, match.team_b, match.estimated_start_time)
    } if _fs_states else set()

    def _match_live_on_flashscore(m: Market) -> bool:
        return getattr(m, "lol_match_id", None) in _fs_hidden

    # One id per real FIXTURE: duplicate Kalshi/Polymarket rows of the same
    # match share it, so the frontend's dedupe and per-match stake cap stop
    # being bypassed by the two rows having different ids.
    # Built from EVERY match row, not just this request's, because the
    # fixture whose clock was copied may not itself have a market here.
    _borrowed = borrowed_start_times(session.query(LolMatch).all())
    _fixture_keys = canonical_fixture_ids(session, LolMatch)

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
    weekly_pool, futures_pool = get_lol_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    # Roster-change "Wait" caveat removed 2026-07-23 -- see cs2_markets.py's
    # own note (no post-roster-change accuracy penalty for esports, so nothing
    # to wait for). Shared wait badge stays for sports where it's real.

    out = []
    for m in markets:
        match = matches_by_id.get(m.lol_match_id) if m.lol_match_id else None
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        model_prob = _game_model_prob(m, match) if m.market_type in GAME_MARKET_TYPES else None
        no_baseline_reason = (
            None if model_prob is not None
            else NON_COMPETITION_REASON if not is_competition_outcome(getattr(m, 'group_label', None))
            else NO_BASELINE_REASON
        )
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "lol", m.market_type)
        pool = futures_pool if m.market_type == "tournament_winner" else weekly_pool
        _uscale = FUTURES_UNIT_SCALE if pool is futures_pool else 1.0
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, unit_scale=_uscale, sport="lol", team=m.team)
        # Zeroed AFTER sizing so the model number and edge still surface for
        # tracking (see MAP_MARKET_NOTE).
        _map_only = m.market_type == "map_winner"
        if _map_only:
            kelly = None
            stake_dollars = None
        out.append(
            LolMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                side=m.side,
                line=m.line,
                match_label=f"{match.team_a} vs {match.team_b}" if match else None,
                lol_match_id=m.lol_match_id,
                fixture_key=_fixture_keys.get(m.lol_match_id, m.lol_match_id),
                event_name=match.event_name if match else None,
                match_date=match.match_date if match else None,
                # A start time can be BORROWED from a rematch between the same
                # teams -- corrected only for rows where that collision is
                # provable (see esports_start_time).
                estimated_start_time=corrected_start_time(match, _borrowed),
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


def _game_insight_lol(match: LolMatch, model_prob: float | None, market_prob: float | None) -> str:
    # The ratings the PRICE came from, not the clean pool's -- see
    # elo_service_lol.get_matchup_ratings for the reported 1500/1500 bug.
    _mr = elo_service_lol.get_matchup_ratings(match.team_a, match.team_b)
    a_rating = _mr["a_rating"] if _mr else None
    b_rating = _mr["b_rating"] if _mr else None
    sentences = []
    if a_rating is not None and b_rating is not None:
        gap = a_rating - b_rating
        seed = f"{match.team_a}|{match.team_b}|{a_rating}|{b_rating}"
        if abs(gap) < 30:
            sentences.append(_seeded_choice(seed, [
                f"This one projects tight -- team Elo has {match.team_a} and {match.team_b} rated almost even ({a_rating:.0f} to {b_rating:.0f}), so there's little to separate them going in.",
                f"There's barely anything between these two on the ratings ({a_rating:.0f} to {b_rating:.0f}), which makes it close to a coin flip on paper.",
                f"About as even as it gets: team Elo puts {match.team_a} and {match.team_b} nearly level ({a_rating:.0f} to {b_rating:.0f}).",
            ]))
        else:
            stronger, s_r, weaker, w_r = (match.team_a, a_rating, match.team_b, b_rating) if gap > 0 else (match.team_b, b_rating, match.team_a, a_rating)
            sentences.append(_seeded_choice(seed, [
                f"{stronger} comes in as the stronger side by team Elo, clear of {weaker} ({s_r:.0f} to {w_r:.0f}).",
                f"The ratings favor {stronger} here, sitting above {weaker} ({s_r:.0f} to {w_r:.0f}).",
                f"Team Elo gives {stronger} the edge, ahead of {weaker} ({s_r:.0f} to {w_r:.0f}).",
            ]))
    # Say WHY the priced number left the pure team-Elo view. Measured 2026-08-07:
    # 20 of 63 live LoL matches have the blends moving it 5pp+ (up to 17.3pp),
    # and none of it was mentioned -- the drawer recited two ratings and then
    # quoted a number those ratings do not produce. Same defect Valorant had;
    # this mirrors the corrected wording there, including the three errors found
    # while fixing it (direction not "pick", attribute by each stage's own delta,
    # and don't call it a player edge unless there is one).
    stages = elo_service_lol.explain_series(match.team_a, match.team_b, match.best_of or 3)
    if stages is not None and abs(stages["p_final"] - stages["p_elo_only"]) >= 0.05:
        a, b = match.team_a, match.team_b
        moved_to = a if stages["p_final"] > stages["p_elo_only"] else b
        toward_a = stages["p_final"] > stages["p_elo_only"]
        d_h2h = stages["p_after_h2h"] - stages["p_elo_only"]
        d_rest = stages["p_after_rest"] - stages["p_after_h2h"]
        d_player = stages["p_final"] - stages["p_after_rest"]

        def _helped(delta: float) -> bool:
            return abs(delta) > 0.005 and (delta > 0) == toward_a

        drivers = []
        if stages["h2h_total"] and _helped(d_h2h):
            w, t = stages["h2h_wins_a"], stages["h2h_total"]
            wins, losses = (w, t - w) if moved_to == a else (t - w, w)
            whose = "their" if moved_to == a else f"{moved_to}'s"
            # State the rate against what Elo implied. A LOSING record can still
            # push a side up -- Team Heretics are 4-10 vs Fnatic, but 4/14 = 29%
            # is better than the 19% team Elo gave them, so h2h correctly moved
            # them up. Without the comparison that reads as a losing record
            # being offered as evidence in their favour.
            rate = wins / t if t else 0.0
            elo_implied = stages["p_elo_only"] if moved_to == a else 1.0 - stages["p_elo_only"]
            drivers.append(
                f"{whose} {wins}-{losses} head-to-head record in {t} prior meeting{'s' if t != 1 else ''} "
                f"({rate * 100:.0f}% where team Elo implies {elo_implied * 100:.0f}%)"
            )
        if _helped(d_rest):
            drivers.append(f"a rest/schedule advantage to {moved_to}")
        if _helped(d_player):
            drivers.append(f"a player-level read that differs from the team ratings, favouring {moved_to}")
        if drivers:
            many = len(drivers) > 1
            subject = "them" if moved_to == a else match.team_a
            # Size-aware, and LoL's evidence is NOT Valorant's -- measured
            # 2026-08-07 over 5,104 walk-forward predictions
            # (scripts/check_lol_blend_by_move_size.py):
            #   5-10pp   n=418  Brier 0.22742 -> 0.22046  CI entirely below 0
            #   10-20pp  n=141  Brier 0.15632 -> 0.16238  CI spans 0, and the
            #                   point estimate is the WRONG WAY
            # Valorant's blends improved monotonically with move size; LoL's do
            # not, and its largest measured band is directionally negative on
            # matches the model already predicts well (note the much lower Brier
            # there). So a big LoL move gets a warning, not the reassurance the
            # Valorant text gives.
            move = abs(stages["p_final"] - stages["p_elo_only"])
            if move < 0.10:
                strength = (
                    "and on moves this size they measurably beat team Elo alone in backtest "
                    "(5,104 walk-forward predictions)"
                )
            else:
                strength = (
                    "and a swing this large is NOT supported by backtest here -- in the 10-20pp band the blends "
                    "came out slightly WORSE than team Elo alone (n=141, not statistically distinguishable from "
                    "no effect), so treat this one with more caution than the size of the gap suggests"
                )
            sentences.append(
                f"The blends pull toward {moved_to}, taking {subject} from "
                f"{stages['p_elo_only'] * 100:.0f}% to {stages['p_final'] * 100:.0f}% on "
                f"{' and '.join(drivers)}. {'These carry' if many else 'That carries'} less weight than the team "
                f"rating, {strength}. Whether they beat the MARKET has not been shown either way."
            )
    sentences.append(_edge_sentence(model_prob, market_prob))
    return " ".join(sentences)


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_lol_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport != "lol":
        raise HTTPException(404, "market not found")
    match = session.get(LolMatch, m.lol_match_id) if m.lol_match_id else None
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
            "Team-level Elo (K=36, grid-searched against a real 5,604-match historical Leaguepedia crawl -- "
            "see elo_lol.py) gives a per-map win probability, extended to a full best-of-N series-score "
            "distribution via the standard 'race to k' binomial identity."
        )
        if match.best_of:
            factors.append(ReasoningFactorOut(label="Best of", detail=str(match.best_of)))
        mr = elo_service_lol.get_matchup_ratings(match.team_a, match.team_b)
        if mr:
            # Show the games behind each rating: a number with no evidence
            # count is what made a well-founded price look like a guess.
            factors.append(ReasoningFactorOut(
                label=f"{match.team_a} Elo rating",
                detail=f"{mr['a_rating']:.0f} ({mr['a_games']} maps)"))
            factors.append(ReasoningFactorOut(
                label=f"{match.team_b} Elo rating",
                detail=f"{mr['b_rating']:.0f} ({mr['b_games']} maps)"))
            if mr["pool"] == "expanded":
                factors.append(ReasoningFactorOut(
                    label="Rating pool",
                    detail="Lower-tier pool (gol.gg). One or both teams never appear in "
                           "the Primary-tier crawl, so the model prices them from the "
                           "expanded pool -- real observed maps, a smaller sample."))
        insight = _game_insight_lol(match, model_prob, market_prob)

    elif m.market_type == "tournament_winner":
        methodology = (
            "Elo-seeded single-elimination Monte Carlo of the event bracket: each team's LoL team Elo sets "
            "its per-match win probabilities, the bracket is simulated many thousands of times, and the "
            "share of runs a team wins the whole event becomes its price. An APPROXIMATION -- real events "
            "are often double-elimination or group/Swiss, so this is a reference estimate (approx badge), "
            "not a validated edge."
        )
        team = m.team or (m.group_label or "this team")
        rating = elo_service_lol.get_team_rating(m.team) if m.team else None
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} Elo rating", detail=f"{rating:.0f}"))
        seed = f"{team}|{rating}|loltw"
        rt = f" (team Elo {rating:.0f})" if rating is not None else ""
        insight = _seeded_choice(seed, [
            f"This is the tournament outright for {team}{rt}. It comes from an Elo-seeded Monte Carlo of the event bracket -- {team}'s rating drives each round's win odds, and the price is how often they take the whole thing across thousands of simulated runs.",
            f"{team}'s{rt} title price is read off a bracket simulation: seed every team by LoL Elo, play the event out many thousands of times, and count how often {team} is left standing.",
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
