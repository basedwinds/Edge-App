"""Valorant markets API -- parallel to routers/mma_markets.py.

Map winner/series winner/series handicap/series total all route through the
same SeriesDistribution (elo_valorant.py) -- one team Elo rating per side,
extended to a full best-of-N series-score distribution via the standard
"race to k" identity, same "one grid, many markets" pattern as
elo_soccer.py's MatchGoalDistribution. Tournament winner futures have NO
baseline yet (no real bracket-simulation model built for esports, same
honest "no model, not a guessed number" pattern as this app's other
not-yet-built futures).

Ratings are trained on a real historical vlr.gg crawl (19,644 matches, main
VCT International/regional circuit + Game Changers + Challengers League,
455 curated events total -- see scripts/build_valorant_match_cache.py) plus
this app's own live-polled match history on top (see
elo_service_valorant.py). K=40 is grid-searched against that real combined
data (scripts/derive_valorant_elo_constants.py -- 61.99% walk-forward
accuracy post-warmup, beats the naive 0.5 baseline). model_validated is
still False for every market_type here -- a real market-odds backtest
against Kalshi's own historical trade data now exists too
(scripts/backtest_valorant_market_odds.py, Map 1 only, 18-match sample) and
found the market beats the model, same conclusion every sport in this app
has found.

Reuses `_batch_latest_snapshots`/`_implied_prob` from routers/markets.py
directly, same as every other sport's router in this app.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice
from app.api.routers.settings import get_staking_params, get_flat_params, get_unit_dollars, get_valorant_pool_dollars
from app.api.schemas import FuturesMarketOut, ReasoningFactorOut, ReasoningOut, ValorantMarketOut
from app.db.database import get_session
from app.clients import flashscore_esports_client
from app.db.chunked import fetch_in_chunks
from app.models.duplicate_fixtures import canonical_fixture_ids
from app.db.models import Market, MarketSnapshot, ValorantMatch
from app.ingestion import market_catalog_valorant
from app.ingestion.market_matcher_valorant import team_names_match
from app.models.baseline import elo_service_valorant
import logging

from app.clients import vlr_client
from app.models.esports_tournament_pricing import (
    find_event_path, is_competition_outcome, price_tournament_winners, skip_reason,
)
from app.models.tournament_sim_esports import TOURNAMENT_SIM_NOTE
from app.models.ladder_sanity import (
    futures_group_decided,
    ESPORTS_LIVE_TRADING_MIN_PRICE_SWING,
    VALORANT_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA,
    VALORANT_POLYMARKET_LIVE_TRADING_MIN_VOLUME_DELTA,
    looks_already_live_by_trading,
)
from app.models.esports_start_time import borrowed_start_times, corrected_start_time
from app.models.staking import apply_duplicate_listing_cap, FUTURES_MAX_SPREAD, FUTURES_MIN_MARKET_PRICE, FUTURES_UNIT_SCALE, has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.staking import MIN_LIVE_FUTURES_BID
from app.models.clv_selection import bucket_clv_stats, gate_kelly

_NO_BASELINE_METHODOLOGY = "No detailed methodology available for this market type yet -- see the module docstring above."

log = logging.getLogger("valorant_markets")

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


router = APIRouter(prefix="/valorant", tags=["valorant"])

GAME_MARKET_TYPES = {"map_winner", "series_winner", "series_handicap", "series_total"}

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

UNRATED_TEAM_REASON = (
    "Not priced: this team has no rating in this app. Its ratings come from a crawl of the "
    "top competitive tiers, so teams from lower divisions, academy rosters and newly-formed "
    "orgs are genuinely absent -- and this app returns nothing rather than inventing a "
    "default rating for them."
)

COLD_START_CAVEAT = (
    "Ratings are trained on a real historical vlr.gg crawl (19,644 matches, main VCT circuit + Game "
    "Changers + Challengers League, 455 curated events) plus this app's own live-polled matches on top "
    "-- 61.99% walk-forward accuracy post-warmup, beats the naive 0.5 baseline. A real market-odds "
    "backtest against Kalshi's own historical trade data (Map 1 only, 18-match sample) found the market "
    "beats the model, so model_validated stays false regardless."
)


def _team_side(match: ValorantMatch | None, team_name: str | None) -> str | None:
    """Returns "team_a" | "team_b" | None -- which side of the match this
    market's `team` field refers to. Exact-normalized match only (see
    market_matcher_valorant.py's own docstring on why token-subset matching
    is unsafe for Valorant team names)."""
    if match is None or not team_name:
        return None
    if team_names_match(team_name, match.team_a):
        return "team_a"
    if team_names_match(team_name, match.team_b):
        return "team_b"
    return None


def _game_model_prob(m: Market, match: ValorantMatch | None) -> float | None:
    if match is None or not match.best_of:
        return None
    dist = elo_service_valorant.get_series_distribution(
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
def list_valorant_futures(session: Session = Depends(get_session)):
    """See cs2_markets.py::list_cs2_futures's own docstring -- Valorant's own
    version, priced by the same Elo-seeded single-elim Monte Carlo
    (esports_tournament_pricing.py): model_prob/edge shown for tracking,
    deliberately NOT staked (bracket is an approximation of real double-elim/
    Swiss events); season-long aggregate markets left unpriced."""
    markets = session.query(Market).filter(Market.sport == "valorant", Market.market_type == "tournament_winner", Market.status == "active").all()
    # Field size per group, for skip_reason's single-event size backstop.
    _group_sizes: dict[str, int] = {}
    for _m in markets:
        _k = _m.group_label or ''
        _group_sizes[_k] = _group_sizes.get(_k, 0) + 1
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

    # vlr.gg knows which teams have already been knocked out of each event's
    # group stage; without it the sim quotes real title odds on teams that
    # cannot win (FNATIC 9.3%, KIWOOM DRX 6.3% -- both eliminated). Guarded
    # because a scrape failure must degrade to the old pricing, not to nothing.
    def _event_state_for(label: str):
        try:
            path = find_event_path(label, vlr_client.list_events())
            return vlr_client.event_state(path) if path else None
        except Exception:
            log.warning("vlr event state unavailable for %r -- rating-seeded fallback", label)
            return None

    _implied_by_market = {_m.id: _implied_prob(snapshots_by_market.get(_m.id)) for _m in markets}
    _field_refusals: dict[str, str] = {}
    _unfielded: dict[int, str] = {}
    _progress_aware: set[str] = set()
    priced = price_tournament_winners(markets, elo_service_valorant,
                                      event_state_for=_event_state_for,
                                      implied_by_market=_implied_by_market,
                                      refusals=_field_refusals,
                                      unfielded=_unfielded,
                                      progress_aware=_progress_aware)
    # STAKED, not tracking-only, as of 2026-08-02. These were hardcoded to
    # kelly_fraction=None on the reasoning that the bracket is an approximation.
    # That reasoning was inverted: the paper logger only records rows the app
    # actually staked, so suppressing them meant they never became paper bets,
    # never accrued forward CLV, and could never be evaluated -- guaranteeing the
    # approximation could never be proven right OR wrong. Since forward CLV is
    # the only thing this app trusts, an approximate model is the one that most
    # needs measuring. They are badged approximate in the UI instead, and the
    # CLV-selection gate can retire them once the data speaks.
    _weekly, _futures_pool = get_valorant_pool_dollars(session)
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
            _clv, "valorant", m.market_type,
        )
        _stake = size_stake_dollars(_mode, _kelly, _futures_pool, model_prob, implied, _unit, _fm, _ff, unit_scale=FUTURES_UNIT_SCALE, min_market_price=FUTURES_MIN_MARKET_PRICE, max_spread=FUTURES_MAX_SPREAD, yes_bid=snap.yes_bid if snap else None, yes_ask=snap.yes_ask if snap else None, sport="valorant", team=m.team)
        # A decided group is shown for the record, never sized. The prior
        # behaviour dropped these rows entirely, which is why a settled
        # future appeared to vanish rather than read as finished.
        _settled = (m.group_label or "") in _decided
        if _settled:
            _kelly = None
            _stake = None
        # ---- BLIND TOURNAMENTS: STAKE ONLY DEMONSTRABLY LIVE TEAMS (#207) ----
        #
        # A tournament group is priced one of two ways. WITH real group standings
        # and the real playoff slot count, the model knows who is through and who
        # is out (only Valorant has a source -- vlr.gg). WITHOUT them it falls
        # back to a flat rating-seeded bracket that re-simulates the whole event
        # from scratch, so a team knocked out yesterday keeps its full
        # pre-tournament win probability.
        #
        # THE FIRST VERSION OF THIS GATE BLOCKED THE WHOLE TITLE, and that was
        # too blunt. Measured on the four legs it removed: three were tight,
        # liquid books on teams the market plainly has alive -- CS2 Vitality
        # (bid 0.22 / ask 0.23), LCK Gen.G (0.29/0.31), LCS Team Liquid
        # (0.32/0.35). Those are ordinary model-vs-market disagreements about
        # LIVE teams, which is the thing this app exists to find. Only the fourth
        # was a real defect, and it was a missing FIELD rather than a missing
        # bracket -- now refused upstream by MIN_BRACKET_FIELD.
        #
        # So the test is per-leg and asks the narrower question the failure
        # actually poses: is this team still alive? A two-sided book with a real
        # BID is the answer. Nobody bids to BUY an eliminated team, so a standing
        # bid is the market asserting the team can still win -- the same book
        # data has_real_trading and the ASK guard already rely on. An eliminated
        # leg collapses to bid 0 / ask ~0.01 and is refused here.
        _bid = snap.yes_bid if snap else None
        _live_book = _bid is not None and _bid > MIN_LIVE_FUTURES_BID
        # OR, NOT AND -- and the difference cost a real stake (2026-08-21).
        #
        # These are two INDEPENDENT reasons to refuse, and joining them with
        # `and` meant a row had to fail BOTH before it was refused. A group that
        # fell back to the flat rating-seeded bracket is not progress-aware:
        # esports_tournament_pricing says of exactly that case, "a team
        # eliminated yesterday keeps its full pre-tournament win probability.
        # Only the first kind is safe to STAKE." A LIVE BOOK DOES NOT REPAIR
        # THAT -- if anything a liquid market makes the stale model MORE
        # dangerous, because the price it disagrees with is a real opinion.
        #
        # Found on VCT EMEA Stage 2: Team Vitality had LOST its upper-bracket
        # match and sat in Lower Bracket Round 1, five series from the title.
        # The bracket-blind sim still priced it at .371 -- FIRST of five -- while
        # the market had it .075, fourth. Fair value from that position is ~4.5%,
        # so the model was ~8x high, and it carried a real $2.50 stake purely
        # because the book was live. The market's own ordering was correct and
        # the model's was inverted; Karmine sat in the Upper Bracket FINAL and
        # the model ranked it last.
        if not _live_book or (m.group_label or "") not in _progress_aware:
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
                model_note=(
                    TOURNAMENT_SIM_NOTE if model_prob is not None
                    # Explain the blank rather than leaving it mysterious: a user
                    # who sees an empty model column has no way to tell "not
                    # modelled yet" from "deliberately out of scope".
                    # Field-completeness refusal first: it is the only cause that
                    # depends on WHO ELSE is in the field rather than on this row.
                    else _field_refusals.get(m.group_label or '')
                    or _unfielded.get(m.id)
                    or skip_reason(m.group_label, _group_sizes.get(m.group_label or '', 0))
                    # Third cause, and the only one the label cannot reveal: the
                    # team itself is outside the rated pool.
                    or (UNRATED_TEAM_REASON if (m.team and elo_service_valorant.get_team_rating(m.team) is None) else None)
                ),
            )
        )
    out.sort(key=lambda m: (m.group_label or "", -(m.model_prob or 0), m.team or ""))
    return out


@router.get("/markets", response_model=list[ValorantMarketOut])
def list_valorant_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "valorant", Market.market_type.in_(GAME_MARKET_TYPES | {"tournament_winner"})).all()
    match_ids = {m.valorant_match_id for m in markets if m.valorant_match_id}
    matches_by_id = {mt.id: mt for mt in session.query(ValorantMatch).filter(ValorantMatch.id.in_(match_ids)).all()} if match_ids else {}

    # Same "don't keep predicting an already-decided/-started/stale market"
    # discipline as every other sport's router in this app (see
    # mma_markets.py's own extended docstring on why all three gates matter).
    def _match_already_decided(m: Market) -> bool:
        match = matches_by_id.get(m.valorant_match_id) if m.valorant_match_id else None
        return match is not None and match.winner is not None

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _match_already_started(m: Market) -> bool:
        match = matches_by_id.get(m.valorant_match_id) if m.valorant_match_id else None
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
        if feed_latest is None or now_for_staleness - feed_latest > FEED_DEAD_AFTER:
            return now_for_staleness - ts > STALE_BEHIND_FEED
        return feed_latest - ts > STALE_BEHIND_FEED

    # REAL BUG this guards against (user-reported 2026-07-20: recommended
    # bets pricing off already-decided matches, e.g. "0.1%" prices) -- see
    # ladder_sanity.py's own module comment for the full esports-specific
    # calibration story, including the real Gentle Mates GC vs G2 Gozen
    # Polymarket case this was validated against live. `_match_already_started`
    # above only fires once vlr.gg's own live listing has actually populated
    # a real estimated_start_time, which lags behind both platforms' own
    # live trading -- this catches the case where that hasn't happened yet
    # but the market's own price/volume history already makes clear the
    # series is live or over. Valorant is the only esports title with real
    # Polymarket inventory, so it's the only one needing its OWN separate
    # Polymarket threshold (Kalshi and Polymarket volume are never
    # comparable scales, same rule as every other sport in this app).
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
        if m.source == "kalshi":
            min_volume_delta = VALORANT_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA
        elif m.source == "polymarket":
            min_volume_delta = VALORANT_POLYMARKET_LIVE_TRADING_MIN_VOLUME_DELTA
        else:
            return False
        current = all_snapshots.get(m.id)
        current_price = current.last_price if current else None
        recent = recent_snapshots_by_market.get(m.id, [])
        return looks_already_live_by_trading(
            current_price, [(s.last_price, s.volume) for s in recent],
            min_volume_delta=min_volume_delta,
            min_price_swing=ESPORTS_LIVE_TRADING_MIN_PRICE_SWING,
        )

    matches_live_by_trading = {m.valorant_match_id for m in markets if m.valorant_match_id and _market_looks_live_by_trading(m)}

    def _match_looks_live_by_trading(m: Market) -> bool:
        return m.valorant_match_id in matches_live_by_trading

    # POSITIVE in-play/finished signal, the only gate here not inferred from a
    # timestamp or a price. Kalshi's start times for esports are demonstrably
    # wrong (the reported DRX case really began 4h before its recorded start),
    # and a result may never arrive for a team whose sponsor name our results
    # source does not know. See flashscore_esports_client for the measured
    # coverage (modest) and for why it still ships: it is ONE-DIRECTIONAL and
    # fails open, so it can only ever hide a match a real source reports as
    # live or over, never one that is genuinely upcoming.
    _fs_states = flashscore_esports_client.cached_match_states("valorant")
    _fs_hidden = {
        mid for mid, match in matches_by_id.items()
        if flashscore_esports_client.hides_match(
            _fs_states, match.team_a, match.team_b, match.estimated_start_time)
    } if _fs_states else set()

    # SECOND positive source, and for Valorant the only one that actually
    # reports (see vlr_client.live_pairs and flashscore_esports_client's
    # TITLE_KEYWORDS note: the flashscore feed carries LoL only, so _fs_states
    # above is always empty here). Same one-directional, fail-open contract.
    #
    # ANCHORED IN TIME on purpose. Two Valorant teams meet repeatedly, and a
    # pair alone cannot tell today's live game from next week's rematch on
    # another row -- hiding the rematch would silently drop a legitimate
    # market. So a live report only applies to a fixture whose own start is
    # near now: already begun (or within 6h of beginning) and not more than
    # 12h past, which comfortably covers a long series without reaching a
    # future meeting.
    _vlr_live = vlr_client.live_pairs()
    _vlr_hidden: set[int] = set()
    if _vlr_live:
        # Same key space vlr_client.live_pairs() builds with, so the two cannot
        # drift apart on diacritics or punctuation.
        from app.ingestion.lol_team_aliases import base_key

        _now = datetime.datetime.now(datetime.timezone.utc)
        for mid, match in matches_by_id.items():
            a, b = base_key(match.team_a), base_key(match.team_b)
            if not a or not b or frozenset((a, b)) not in _vlr_live:
                continue
            raw = getattr(match, "estimated_start_time", None)
            if not raw:
                continue
            try:
                started = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=datetime.timezone.utc)
            ahead = (started - _now).total_seconds() / 3600.0
            if -12.0 <= ahead <= 6.0:
                _vlr_hidden.add(mid)

    def _match_live_on_flashscore(m: Market) -> bool:
        mid = getattr(m, "valorant_match_id", None)
        return mid in _fs_hidden or mid in _vlr_hidden

    # One id per real FIXTURE: duplicate Kalshi/Polymarket rows of the same
    # match share it, so the frontend's dedupe and per-match stake cap stop
    # being bypassed by the two rows having different ids.
    # Built from EVERY match row, not just this request's, because the
    # fixture whose clock was copied may not itself have a market here.
    _borrowed = borrowed_start_times(session.query(ValorantMatch).all())
    _fixture_keys = canonical_fixture_ids(session, ValorantMatch)

    # A HALT ON ONE PLATFORM IS INFORMATION THE OTHER HAS NOT PRICED YET.
    # See cs2_markets.py for the case that produced this: a walkover where
    # Kalshi halted both sides while Polymarket kept quoting a phantom
    # 0.495/0.505, manufacturing a +19.1pp edge on a match nobody would play.
    # The per-market `status == "active"` test below cannot catch it, because
    # the halted rows are not the rows being recommended -- the halt has to be
    # read at the FIXTURE level, across platforms.
    _halted_fixture_ids = {
        _m.valorant_match_id for _m in markets
        if _m.valorant_match_id and (_m.status or "") == "inactive"
    }

    markets = [
        m for m in markets
        if not _match_live_on_flashscore(m)
        if not _match_already_decided(m)
        and not _match_already_started(m)
        and not (m.valorant_match_id and m.valorant_match_id in _halted_fixture_ids)
        and (m.status or "active") == "active"
        and not _market_stale(m)
        and not _match_looks_live_by_trading(m)
    ]
    # Hoisted: as an inline set literal this was rebuilt once per
    # all_snapshots entry -- quadratic, and the dominant cost of the
    # tennis endpoint at 34k markets (183M attribute reads, ~40s).
    _kept_market_ids = {m.id for m in markets}
    snapshots_by_market = {mid: s for mid, s in all_snapshots.items() if mid in _kept_market_ids}
    weekly_pool, futures_pool = get_valorant_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    # Roster-change "Wait" caveat removed 2026-07-23 -- see cs2_markets.py's
    # own note: the calibration found no post-roster-change accuracy penalty
    # for esports, so the flag had nothing to wait for. Shared wait badge stays
    # for sports where a wait is real; esports no longer feed it.

    out = []
    for m in markets:
        match = matches_by_id.get(m.valorant_match_id) if m.valorant_match_id else None
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
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "valorant", m.market_type)
        pool = futures_pool if m.market_type == "tournament_winner" else weekly_pool
        _uscale = FUTURES_UNIT_SCALE if pool is futures_pool else 1.0
        stake_dollars = size_stake_dollars(staking_mode, kelly, pool, model_prob, implied, unit_dollars, flat_marginal, flat_full, max_spread=FUTURES_MAX_SPREAD, yes_bid=snap.yes_bid if snap else None, yes_ask=snap.yes_ask if snap else None,  unit_scale=_uscale, sport="valorant", team=m.team)
        # Zeroed AFTER sizing so the model number and edge still surface for
        # tracking (see MAP_MARKET_NOTE).
        _map_only = m.market_type == "map_winner"
        if _map_only:
            kelly = None
            stake_dollars = None
        out.append(
            ValorantMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                side=m.side,
                line=m.line,
                match_label=f"{match.team_a} vs {match.team_b}" if match else None,
                valorant_match_id=m.valorant_match_id,
                fixture_key=_fixture_keys.get(m.valorant_match_id, m.valorant_match_id),
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
    # Same cross-platform duplicate cap as cs2_markets -- see the note there.
    # 32 duplicated groups / 64 staked legs / $1,020 in the settled book.
    duped = apply_duplicate_listing_cap(out, fixture_attr="fixture_key")
    if duped:
        log.info("valorant: unstaked %d cross-platform duplicate listings", duped)
    out.sort(key=lambda m: (m.match_date or "9999", m.match_label or m.group_label or "", m.market_type))
    return out


def _game_insight_valorant(match: ValorantMatch, market_type: str, model_prob: float | None, market_prob: float | None) -> str:
    a_rating = elo_service_valorant.get_team_rating(match.team_a)
    b_rating = elo_service_valorant.get_team_rating(match.team_b)
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
    # Say WHY the priced number left the pure team-Elo view, when it did. Without
    # this the drawer could name the stronger team on Elo and then quote a model
    # number favouring the other one, with nothing in between -- which is exactly
    # what a user challenged on Team Secret vs DetonatioN FocusMe.
    stages = elo_service_valorant.explain_series(match.team_a, match.team_b, match.best_of or 3)
    if stages is not None and abs(stages["p_final"] - stages["p_elo_only"]) >= 0.05:
        a, b = match.team_a, match.team_b
        moved_to = a if stages["p_final"] > stages["p_elo_only"] else b
        # Attribute the move to the stages that ACTUALLY caused it, by sign of
        # each stage's own delta. Listing a stage just because it exists gets
        # this wrong: on Joblife GC vs Karmine Corp GC the text credited a 0-1
        # head-to-head record for pulling the number TOWARD Joblife, when 0-1
        # pushes the other way and the real mover was the rest/fatigue blend --
        # which was not mentioned at all.
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
            # Name the team rather than say "their": the sentence's subject is
            # team_a (whose probability the figures describe) while this record
            # is oriented to whichever side the move favoured, so a pronoun here
            # points at the wrong team half the time.
            # "their" only when the sentence subject (team_a) IS the side the
            # move favours -- otherwise name the team, or the pronoun points at
            # the wrong one.
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
        pa, pb = stages["player_strength_a"], stages["player_strength_b"]
        if pa is not None and pb is not None and _helped(d_player):
            # The player blend can pull toward a side WITHOUT that side's players
            # rating higher -- it moves the number whenever the player model is
            # less lopsided than the team ratings, which on a big Elo gap is most
            # of the time. Calling that "a rating edge to LOUD (1591 to 1615)"
            # printed the favoured team's LOWER number first and read as a
            # contradiction. Only claim an edge when there actually is one.
            moved_strength, other_strength = (pa, pb) if moved_to == a else (pb, pa)
            if moved_strength > other_strength:
                drivers.append(
                    f"a player-level rating edge to {moved_to} "
                    f"({moved_strength:.0f} to {other_strength:.0f}) that team Elo hasn't caught up with"
                )
            else:
                drivers.append(
                    f"a player-level read that is far closer to even than the team ratings "
                    f"({match.team_a} {pa:.0f}, {match.team_b} {pb:.0f})"
                )
        if drivers:
            # Wording corrected 2026-08-07. This used to close on "both smaller,
            # LESS-VALIDATED inputs than the team rating", which was unfair to
            # the model: both blends are validated on walk-forward Brier and both
            # sit exactly on their grid optimum (h2h prior_weight 10:
            # 0.22506 -> 0.22430; player w=0.4: 0.23064 -> 0.22742, i.e. the
            # player blend helps ~4x MORE than h2h, not less). Binning 19,144
            # predictions by how far the blend moved the price also showed they
            # help MORE on big moves, not less -- the opposite of what the old
            # sentence implied. What is genuinely unestablished is whether they
            # beat the MARKET, which is a narrower claim and the one now made.
            # The confidence claim is SIZE-AWARE, because the evidence is. Binned
            # by how far the blend moved the price, the 2-20pp bands each beat
            # pure Elo with a bootstrap CI entirely below zero; the 20pp+ band is
            # directionally the strongest of all but has only 56 observations and
            # a CI that crosses zero. Saying "measured" for a 30pp move would be
            # claiming something this app has not shown.
            move = abs(stages["p_final"] - stages["p_elo_only"])
            many = len(drivers) > 1
            verb = "they" if many else "it"
            if move < 0.20:
                strength = (
                    f"and on moves this size {verb} measurably beat{'' if many else 's'} team Elo alone in "
                    "backtest (19,144 walk-forward matches)"
                )
            else:
                strength = (
                    "and blends of this size point the right way in backtest, though on a thin sample "
                    "(56 matches) -- treat a swing this large as the least-established part of the number"
                )
            # Phrased as the DIRECTION of the shift, not as a pick. The earlier
            # wording said "the model still lands on {moved_to}", which was
            # simply false whenever the blends moved the number without flipping
            # it: on G2 Esports vs LOUD it read "the model still lands on LOUD"
            # while the figures quoted (84% -> 69%) are G2's probability and the
            # model still favoured G2. p_elo_only/p_final are always team_a's
            # probability, so team_a has to be the stated subject.
            subject = "them" if moved_to == a else match.team_a
            sentences.append(
                f"The blends pull toward {moved_to}, taking {subject} from "
                f"{stages['p_elo_only'] * 100:.0f}% to {stages['p_final'] * 100:.0f}% on "
                f"{' and '.join(drivers)}. {'Both carry' if many else 'That carries'} less weight than the team "
                f"rating, {strength}. What hasn't been shown is whether {'they beat' if many else 'it beats'} the MARKET "
                f"-- so read this as the disagreement resting on {'them' if many else 'it'} rather than on the Elo, "
                f"not as a reason to discount it."
            )
    sentences.append(_edge_sentence(model_prob, market_prob))
    return " ".join(sentences)


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_valorant_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    m = session.get(Market, market_id)
    if m is None or m.sport != "valorant":
        raise HTTPException(404, "market not found")
    match = session.get(ValorantMatch, m.valorant_match_id) if m.valorant_match_id else None
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
            "Team-level Elo (K=40, grid-searched against a real 19,644-match historical vlr.gg crawl -- "
            "see elo_valorant.py) gives a per-map win probability, extended to a full best-of-N series-"
            "score distribution via the standard 'race to k' binomial identity (same technique family as "
            "a tennis match-win-from-set-win-probability calculation)."
        )
        if match.best_of:
            factors.append(ReasoningFactorOut(label="Best of", detail=str(match.best_of)))
        a_rating = elo_service_valorant.get_team_rating(match.team_a)
        b_rating = elo_service_valorant.get_team_rating(match.team_b)
        if a_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.team_a} Elo rating", detail=f"{a_rating:.0f} ({elo_service_valorant.get_team_games(match.team_a)} maps tracked)"))
        if b_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{match.team_b} Elo rating", detail=f"{b_rating:.0f} ({elo_service_valorant.get_team_games(match.team_b)} maps tracked)"))
        # The two inputs that can override the team rating; listed always, so a
        # bet that leans on them is auditable rather than looking like it
        # contradicts the Elo line directly above.
        stages = elo_service_valorant.explain_series(match.team_a, match.team_b, match.best_of or 3)
        if stages is not None:
            if stages["h2h_total"]:
                factors.append(ReasoningFactorOut(
                    label="Head-to-head",
                    detail=f"{match.team_a} {stages['h2h_wins_a']}-{stages['h2h_total'] - stages['h2h_wins_a']} {match.team_b}",
                ))
            if stages["player_strength_a"] is not None and stages["player_strength_b"] is not None:
                factors.append(ReasoningFactorOut(
                    label="Player-model rating",
                    detail=f"{match.team_a} {stages['player_strength_a']:.0f}, {match.team_b} {stages['player_strength_b']:.0f}",
                ))
            factors.append(ReasoningFactorOut(
                label="Series prob: team Elo -> final",
                detail=(f"{match.team_a} {stages['p_elo_only'] * 100:.0f}% on team Elo alone, "
                        f"{stages['p_final'] * 100:.0f}% after head-to-head and the player blend"),
            ))
        insight = _game_insight_valorant(match, m.market_type, model_prob, market_prob)

    elif m.market_type == "tournament_winner":
        methodology = (
            "Elo-seeded single-elimination Monte Carlo of the event bracket: each team's Valorant team Elo "
            "sets its per-match win probabilities, the bracket is simulated many thousands of times, and the "
            "share of runs a team wins the whole event becomes its price. An APPROXIMATION -- real events "
            "are often double-elimination or Swiss, so this is a reference estimate (approx badge), not a "
            "validated edge."
        )
        team = m.team or (m.group_label or "this team")
        rating = elo_service_valorant.get_team_rating(m.team) if m.team else None
        if rating is not None:
            factors.append(ReasoningFactorOut(label=f"{m.team} Elo rating", detail=f"{rating:.0f}"))
        seed = f"{team}|{rating}|valtw"
        rt = f" (team Elo {rating:.0f})" if rating is not None else ""
        insight = _seeded_choice(seed, [
            f"This is the tournament outright for {team}{rt}. It comes from an Elo-seeded Monte Carlo of the event bracket -- {team}'s rating drives each round's win odds, and the price is how often they take the whole thing across thousands of simulated runs.",
            f"{team}'s{rt} title price is read off a bracket simulation: seed every team by Valorant Elo, play the event out many thousands of times, and count how often {team} is left standing.",
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
