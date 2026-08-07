"""Kelly Criterion bet-sizing, applied conservatively on purpose: every
model in this app is explicitly labeled model_validated: false (see
elo_service.py and every other model's own docstring) -- full Kelly assumes
your probability estimate is correct, and betting full Kelly on a WRONG
edge is how real money gets lost fast. User confirmed 2026-07-16: quarter
Kelly (25% of the full Kelly-suggested fraction), hard-capped at 5% of
bankroll on any single position regardless of how large the calculated edge
looks -- a big Kelly number is exactly as likely to mean "the model is
wrong" as "there's a real edge," given this app's own repeated finding
across every sport this user has built that these edges mostly don't
survive backtesting.

Kelly formula for a binary Yes/No contract (Kalshi/Polymarket's actual
structure): buying YES at price `market_price` costs `market_price` per
contract and pays $1 if correct (profit 1-market_price) or $0 if wrong
(loss market_price) -- net odds b = (1-market_price)/market_price per unit
staked. Standard Kelly: f* = p - (1-p)/b, simplified below to avoid a
division-by-zero at market_price=1.
"""
FRACTIONAL_KELLY = 0.25
MAX_STAKE_FRACTION = 0.05
# Raised from 0.01 -> 0.03 2026-07-16: at 1pp, 722/2192 futures rows cleared
# the gate (see Recommended Bets user feedback -- "over 700 recommended bets
# ... some identical?"). Checked the real numbers before picking a new value:
# re-running the same gate at 3/5/8/10pp on live data only trimmed the count
# to 622/549/490/446 -- the 700+ count was NEVER mostly noise-level edges,
# it's structural (ladder rungs of the same underlying bet, the same
# real-world outcome priced on both platforms) -- see
# frontend/src/api/markets.ts::buildRecommendedBets for the actual fix. 3pp
# is a modest bump to also trim genuinely-marginal disagreements, not a
# fix for the volume problem by itself.
MIN_EDGE_TO_BET = 0.03

# Added 2026-07-16: user is running NFL as ONE of ~8-9 sports on a single
# 200-unit cross-sport bankroll (tennis/motorsport/MMA/esports/NBA/MLB/
# NCAAF/NCAAB planned) -- computing kelly_fraction against the FULL bankroll
# (as this app did through Round 18) was wrong the moment a second sport
# exists, since a single NFL bet could otherwise eat capital meant for other
# sports entirely. NFL now gets its own sub-allocation of the total
# bankroll (NFL_ALLOCATION_PCT, user-set in Settings, default 15%), split
# further into two pools:
#   - WEEKLY pool: per-game markets (moneyline/spread/total/team_total/half
#     variants) -- capital frees up every week when the market settles.
#   - FUTURES pool: everything else (season win totals, MVP/DPOY/OPOY,
#     division/conference/SB futures, season-stat ladders, ...) -- capital
#     is locked up for months until the season resolves, so it gets a
#     SMALLER slice (FUTURES_SUBPOOL_PCT, default 30%) of NFL's allocation.
# kelly_fraction() itself is unchanged (still bankroll-agnostic, returns a
# fraction 0..MAX_STAKE_FRACTION) -- what changed is WHICH dollar amount
# gets passed into suggested_stake_dollars() at the call site (see
# markets.py), based on this classification.
WEEKLY_MARKET_TYPES = {
    "moneyline", "spread", "total", "team_total",
    "spread_1h", "spread_2h", "total_1h", "total_2h",
    # NFL half WINNER (KXNFL1H/KXNFL2H) -- per-game like every other half
    # market here, so capital frees up the same night, not at season end.
    "winner_1h", "winner_2h",
    "f5", "rfi",
    # MMA per-fight market types (added 2026-07-17) -- "weekly" in the sense
    # that capital frees up once each fight settles, same as every other
    # entry here, even though UFC's real cadence isn't literally weekly.
    "distance", "method_of_victory", "method_of_finish", "rounds", "round_of_victory",
    # Soccer (added 2026-07-19): a distinct name from plain "moneyline" since
    # the market SHAPE is genuinely different (3-way Home/Draw/Away, not
    # binary) -- per-match, capital frees up once each match settles.
    "moneyline_3way",
    # Esports (added 2026-07-19): "map_winner"/"series_winner"/
    # "series_handicap"/"series_total" all settle per-match (or per-map,
    # capital frees up on the same cadence), same "weekly" bucket role as
    # every other sport's per-game market types here. These strings are
    # reused verbatim across CS2/LoL/Valorant (same market shape, different
    # title) -- but as of 2026-07-20 each title draws from its OWN
    # independent pool (see settings.py::VALORANT_ALLOCATION_PCT_KEY's own
    # docstring), not a shared one; the string reuse here is just about
    # avoiding 3 near-identical entries, not about pooling. "tournament_winner"
    # is deliberately NOT listed -- it's a season-long futures market_type,
    # same as every other sport's own tournament/league-winner futures, and
    # correctly falls through to the FUTURES pool via is_weekly_market_type's
    # own unknown-defaults-to-futures behavior.
    "map_winner", "series_winner", "series_handicap", "series_total",
}

# Tennis (added 2026-07-18): moneyline only in this build (Phase 2 scope) --
# per-match market, capital frees up once each match settles.


def is_weekly_market_type(market_type: str) -> bool:
    """True for per-game markets (WEEKLY pool); False for everything else
    (FUTURES pool). Unknown/future market types default to the FUTURES pool
    -- the more conservative assumption (smaller pool) when a new market
    type is added and someone forgets to classify it."""
    return market_type in WEEKLY_MARKET_TYPES


# A market this far toward either extreme is priced as a near-certainty.
EXTREME_MARKET_PRICE = 0.10
# Disagreement with an extreme market beyond this is not an edge -- see the note
# in kelly_fraction.
IMPLAUSIBLE_EDGE = 0.25


# How many times more likely our model may think a side is than the market before
# the disagreement stops being an edge and starts being a bug.
IMPLAUSIBLE_ODDS_RATIO = 10.0


def implausible_disagreement(model_prob: float, market_price: float) -> bool:
    """Is this disagreement too large to believe?

    REPLACES a percentage-point gap gated on an absolute price cliff (a >=25pp gap
    at a price <=0.10 or >=0.90). That shape had two problems.

    First, percentage points are the wrong unit near the tails. A model of 0.27
    against a market of 0.005 is FIFTY-FOUR times the market's estimate and is
    plainly broken; a model of 0.479 against 0.0975 is under five times and is
    just an aggressive longshot call. The old rule scored those 27pp and 38pp and
    blocked the sane one harder than the absurd one.

    Second, the cliff was arbitrary and visible in the product. The user reported
    a Hanwha Life KeSPA Cup future vanishing from recommendations while Gen.G
    stayed: HLE priced 0.0975, a quarter-point under the 0.10 threshold, and
    Gen.G at 0.1465 above it. Same market, same model, opposite treatment,
    entirely because of where the cliff sat.

    An odds ratio has no cliff and treats both tails alike (above 0.5 it compares
    the complements, so "market says 94%, model says 3%" is measured as the 16x
    disagreement it is).

    CHOSEN AGAINST REAL OUTCOMES, not taste. Over 3,912 settled bets carrying a
    usable placement price, the old rule blocks 35 and a 10x ratio blocks 35 --
    the same exposure, so this is a reshaping rather than a loosening. It keeps
    blocking the cases that should be blocked (a 0.9995 market against a 0.654
    model, i.e. a decided match) and releases the genuine longshot.

    HONEST LIMIT: n=35 is far too small to prove one threshold beats another on
    results. The case for 10x rests on the unit being right for tail
    probabilities and on the cliff being indefensible -- not on a measured edge.
    """
    if not (0.0 < market_price < 1.0):
        return False
    if market_price <= 0.5:
        ratio = model_prob / market_price if market_price > 0 else float("inf")
    else:
        # Compare the complements above 0.5 so both tails are measured alike.
        ratio = (1.0 - model_prob) / (1.0 - market_price) if market_price < 1 else float("inf")
    return ratio >= IMPLAUSIBLE_ODDS_RATIO


def kelly_fraction(
    model_prob: float | None,
    market_price: float | None,
    fractional_kelly: float = FRACTIONAL_KELLY,
    max_stake_fraction: float = MAX_STAKE_FRACTION,
    min_edge_to_bet: float = MIN_EDGE_TO_BET,
    has_traded: bool = True,
    execution_price: float | None = None,
) -> float | None:
    """Returns the suggested stake as a fraction of bankroll (already
    scaled by fractional_kelly and capped at max_stake_fraction), or None if
    there's no usable edge (missing data, edge below min_edge_to_bet, or a
    negative/zero full-Kelly result -- i.e. the market price already looks
    at least as good as our model's own estimate, so there's no case for
    betting the YES side at all). The three tuning knobs default to this
    module's constants but are user-editable in Settings (2026-07-16) --
    callers fetch the live values via settings.py::get_staking_params and
    pass them in, rather than this function reading global state itself.

    `has_traded` -- REAL bug caught live via the Recommended Bets page
    (2026-07-17): a Kalshi market with zero real trades yet (volume=0,
    last_price=0, confirmed live for MLB team-total "Over 1.5" rungs on
    next-day games) has `implied_prob` computed from a fresh yes_bid/yes_ask
    midpoint -- a market-maker's opening quote, not real informed pricing.
    Compared against this app's own real, structural model_prob (e.g. a
    genuinely-likely-true ~85% for "scores 2+ runs"), an untraded ~50% seed
    quote produces a huge, entirely artifactual "edge" that dominated the
    Recommended Bets list once caught (all 12 MLB weekly picks were untraded
    rows at the time this was found). Caller passes False when a source's
    snapshot has volume AND last_price both exactly 0."""
    # IMPLAUSIBLE-EDGE GUARD (2026-08-03, after a real loss).
    #
    # A bet was recommended on Toby Martin at a market price of 0.05 while the
    # model said 0.67 -- a "62pp edge". The market was at 0.05 because the match
    # was already in play and he was losing; it settled LOST two minutes after
    # being placed. Every start-time gate missed it, because Kalshi's
    # occurrence_datetime claimed the match had not begun.
    #
    # This does not rely on any timestamp, which is the point -- those are the
    # thing that keeps failing. It relies on the shape of the disagreement, and
    # the outcome record backs it up. Placed bets bucketed by |edge|:
    #
    #     0-10pp   n=1923  win 38.3%  avg market price 0.408
    #     10-20pp  n= 713  win 45.7%  avg market price 0.390
    #     20-30pp  n= 328  win 44.8%  avg market price 0.320
    #     30pp+    n= 604  win 55.0%  avg market price 0.087
    #
    # The first band behaves like a real market (bet at 40.8%, win 38.3% -- fair
    # pricing minus spread). The 30pp+ band is betting at an average price of
    # 8.7%: those are not underdogs the model found value on, they are markets
    # priced low because the result was already being decided.
    #
    # So: refuse when the market has priced something as a heavy longshot AND the
    # model wildly disagrees. A genuine pre-match edge of that size against a
    # market that extreme does not exist -- it is a live price, a stale price, or
    # a broken model, and none of those is a bet worth making.
    if model_prob is not None and market_price is not None:
        if implausible_disagreement(model_prob, market_price):
            return None

    if model_prob is None or market_price is None:
        return None
    if market_price <= 0.0 or market_price >= 1.0:
        return None
    if not has_traded:
        return None

    # THE EDGE HAS TO SURVIVE THE PRICE YOU ACTUALLY PAY.
    #
    # market_price here is the MID of the book. You do not get the mid -- betting
    # the YES side of a market costs the ASK. On a tight book that distinction is
    # noise; on a wide one it is the whole bet. Measured live 2026-08-04 over 226
    # staked rows carrying a two-sided book, 16 had an edge that did not merely
    # shrink at the ask but INVERTED: a real $20 LoL recommendation on Team WE
    # read +8.6pp against the mid of 0.500 and -33.4pp against its ask of 0.92.
    # $230 was staked across those rows.
    #
    # The bar applied here is min_edge_to_bet, the same one the mid has to clear,
    # not merely "positive". min_edge_to_bet exists to demand a margin of safety;
    # demanding it only of a price you cannot transact at defeats the point.
    # Measured cost of using the stricter bar rather than "> 0": 41 rows / $510
    # instead of 16 rows / $230.
    #
    # Only applies when an ask is actually known. Rows with no two-sided quote
    # behave exactly as before, so this cannot silently empty a sport whose
    # platform does not publish a book.
    if execution_price is not None:
        if execution_price <= 0.0 or execution_price >= 1.0:
            return None
        if (model_prob - execution_price) < min_edge_to_bet:
            return None

    edge = model_prob - market_price
    if edge < min_edge_to_bet:
        return None

    full_kelly = model_prob - (1.0 - model_prob) * market_price / (1.0 - market_price)
    if full_kelly <= 0:
        return None

    return round(min(full_kelly * fractional_kelly, max_stake_fraction), 4)


def has_real_trading(source: str, volume: float | None, last_price: float | None) -> bool:
    """True unless a market has genuinely never traded (volume exactly 0 or
    missing).

    Used to be Kalshi-only, gated on volume==0 AND last_price==0 together:
    this app's Polymarket ingestion hardcoded volume=None on every single
    row (all 5 sports), on the assumption that "Polymarket's API never
    exposes real volume". That assumption was WRONG -- caught live
    2026-07-19 during a Tennis model-vs-market discrepancy audit (83
    matches with |model-market| >= 30pp, 137/185 rows Polymarket-sourced):
    the Gamma API's per-MARKET object carries a real `volumeNum` field
    (confirmed live, e.g. a real $231k-volume ATP moneyline market) that
    `extract_market_prices` simply never read. Now that every sport's
    Polymarket client/catalog layer threads real volume through (see
    polymarket_client.py::_extract_volume), both sources get the same gate.

    Dropped the `last_price==0` half of the old joint condition here, not
    just the source carve-out: spot-checked live 2026-07-19 (the same
    audit) a real, brand-new Polymarket moneyline market (Faurel vs Rincon)
    with volume=None/0 but last_price=0.97 -- Polymarket clearly
    seeds/reports a last_price even before a single contract has actually
    traded, unlike Kalshi where the original bug's untraded rows had BOTH
    fields at exactly 0. Requiring last_price==0 too would let exactly this
    kind of untraded-but-quoted Polymarket row keep sailing through the
    gate it was built to catch. `source` is kept as a parameter only so a
    future platform with its own quirk has a documented place to carve out,
    not because either current source needs one.

    (bestBid/bestAsk themselves were deliberately NOT threaded through
    alongside volume, despite being available from the same Gamma API
    object -- caught live 2026-07-19 in this same investigation: a 2-outcome
    market object carries ONE bestBid/bestAsk pair describing outcome index
    0 only, but this app builds one ROW per outcome from that same object;
    naively copying it onto both rows fed `_implied_prob()`'s yes_bid/
    yes_ask-midpoint preference the WRONG side's quote on one of the two
    rows for every 2-outcome Polymarket market. volume has no such
    per-outcome ambiguity -- it's a single real market-wide count -- which
    is why only it gets threaded through.)"""
    return not ((volume or 0) == 0)


def suggested_stake_dollars(kelly_frac: float | None, bankroll: float | None) -> float | None:
    if kelly_frac is None or bankroll is None or bankroll <= 0:
        return None
    return round(kelly_frac * bankroll, 2)


# Flat/scaled unit staking (user choice 2026-07-23). Rationale: Kelly sizes a
# bet proportional to its edge, which is optimal ONLY when the edge estimate is
# accurate -- and this app's own repeated finding is that NO model beats the
# market, so `model_prob` is not a trustworthy edge. Kelly therefore stakes the
# MOST on the biggest model-vs-market disagreements, which are the most likely
# to be model ERROR (the +30pp MMA-longshot rows), not real edge. Flat units
# sidestep that: every qualifying bet is a clean, equal-weighted stake, which is
# the honest way to accumulate CLV and FIND where edge actually is before
# trusting the model to size. Switch buckets back to Kelly once CLV proves them.
FLAT_MARGINAL_UNIT_EDGE = 0.03  # >= this edge (and Kelly-qualified) -> 0.5 unit
FLAT_FULL_UNIT_EDGE = 0.05      # >= this edge -> 1.0 unit

# Futures stake a QUARTER unit, not a full one. Measured 2026-08-02: a $20 unit
# against each sport's own futures sub-pool, capped at PORTFOLIO_CEILING_PCT
# (60%), made a futures bet arithmetically IMPOSSIBLE in eight of ten sports --
#
#     sport            futures pool   60% ceiling   max 1u bets
#     nba / mlb          $48.00        $28.80            1
#     soccer             $24.00        $14.40            0
#     cfb / mma / tennis $18.00        $10.80            0
#     valorant/cs2/lol   $12.00         $7.20            0
#
# Those buckets were not being throttled, they were blocked -- and a blocked
# stake never becomes a paper bet, so they accrued ZERO forward CLV. Same
# failure shape as the tracking-only suppression reversed earlier today: a rule
# that quietly prevents the measurement it depends on.
#
# 0.25u is chosen because it is the smallest change that unblocks EVERY sport
# (cfb/tennis/mma/soccer 2 bets, esports 1, nba/mlb 5). It is also independently
# right: a futures bet locks capital for months at far lower turnover than a game
# bet resolving in hours, so equal sizing was never really equal risk.
FUTURES_UNIT_SCALE = 0.25
# Deliberately does NOT scale UP past 1u for bigger edges: a large disagreement
# is a red flag (probable model error), not a reason to bet more -- see above.


# Minimum MARKET price for a futures bet. Below this the model does not size it,
# so it never reaches the cross-sport "what should I place" list.
#
# CHOSEN FROM THE USER'S OWN REAL BOOK (paper=0, settled, n=135), by entry price:
#
#     entry price     n   won    win%      ROI
#     0-5%            4     0     0.0%   -100.0%
#     5-15%           8     0     0.0%   -100.0%
#     15-30%         39    11    28.2%    +39.6%
#     30-50%         64    33    51.6%    +38.7%
#     50%+           20    11    55.0%     -8.5%
#
# Twelve bets under 15%, ZERO winners; the first winner anywhere in the book is
# at 16.0%. Applying a floor moves ROI +19.2% -> +28.1% and, more to the point,
# takes the 95% CI from [-6.4%, +46.7%] (spans zero) to [+1.0%, +56.8%].
#
# SET AT 10%, NOT 15%, AND THE SECOND NUMBER IS WHY. The real book has only
# THREE settled bets in the 10-15% band. The paper harness has 211, and says
# that band is the best in the whole table:
#
#     paper harness (n=6,731 settled with a usable price)
#     0-5%      46   ROI +11.1%
#     5-10%     80   ROI -12.0%
#     10-15%   211   ROI +18.5%    <- best band
#     15-20%   326   ROI  -1.8%
#     20-30%   937   ROI +13.3%
#
# So the replicated negative is 5-10% (real book 0-for-5 there, paper -12.0%
# on n=80), and 0-5% is already mostly handled by implausible_disagreement.
# A 15% floor would have cut the 10-15% band on the strength of 3 observations
# while the 211-observation sample called it the best -- and would have removed
# EVERY LoL, tennis and Valorant row, since a large-field tournament winner is
# structurally priced under 15%. Paper fills are free, so that sample cannot
# settle whether those prices are actually reachable, but it is the only sample
# with the weight to rule on this band, and it says do not cut it.
#
# THIS IS NOT THE 5% FLOOR THAT WAS TESTED AND REJECTED EARLIER. That one was
# worth +1.4pp over 5 bets because implausible_disagreement already blocked 32 of
# the 37 sub-5% losers. It cannot see the 5-10% band at all -- an 8c market
# against a 25% model is a 3x disagreement, nowhere near the 10x trigger -- and
# that band is the one both samples agree loses.
#
# READ THIS BEFORE TRUSTING THE ROI TABLE ABOVE. Extending this floor to GAME
# bets was built, measured, and REVERTED, and the same measurement weakened the
# case for the numbers above:
#
#  - **0-for-12 is not a significant result.** At a true 12% win rate in that
#    band, P(0 wins in 12) = 21.6%; for the 9 game bets alone, P(0 in 9) = 31.6%.
#    A run like that turns up about one time in four. It is not evidence of a
#    broken band, and quoting it as though it were was overstating the case.
#  - **The band effect is SPORT-DEPENDENT, not general.** Splitting the paper
#    harness under 10c: tennis n=91 ROI **+7.5%** (positive, and the largest
#    sub-10c sample there is); all other sports n=35 ROI -32.1%; racing n=0.
#    The pooled "5-10% is -12.0%" was largely tennis, whose 0-5% is +30.7%.
#  - **A blanket game floor would have gutted racing on zero evidence**: 70.6% of
#    F1, 62.9% of IndyCar and 52.3% of NASCAR recommendations price under 10c,
#    and racing has 193 paper bets ALL STILL PENDING -- not one settled outcome
#    at any price. Same shape as the 15%-futures-floor mistake, which would have
#    deleted every LoL/tennis/Valorant row.
#
# SO WHY KEEP IT FOR FUTURES? Not because the price band is proven -- it isn't.
# It is a PRECAUTIONARY rule, and the asymmetry is real:
#   - Futures have essentially NO forward validation: 7 settled real bets and 1
#     of 180 paper. Betting the extreme tail of a model nothing has yet checked
#     is a different proposition from betting its middle.
#   - The one futures market type that COULD be tested is overstated in exactly
#     that tail (soccer relegation predicted 40-60% happened 35.8%, and no
#     historically top-half club has ever been rated above 30% relegation in
#     1,072 backtested team-seasons).
#   - Being wrong locks capital for months, and it costs 4 rows.
# Game bets have none of those: they settle in hours, they carry the bulk of the
# validation, and the biggest sub-10c sample in them is positive.
FUTURES_MIN_MARKET_PRICE = 0.10


def flat_stake_units(model_prob: float | None, market_price: float | None,
                     marginal_edge: float = FLAT_MARGINAL_UNIT_EDGE,
                     full_edge: float = FLAT_FULL_UNIT_EDGE) -> float | None:
    """Unit count (1.0 / 0.5 / None) for a bet, by edge tier. None below the
    marginal edge. Qualification (has-traded, positive full-Kelly, CLV bucket)
    is handled upstream by kelly_fraction/gate_kelly -- this only sizes."""
    if model_prob is None or market_price is None:
        return None
    edge = model_prob - market_price
    if edge >= full_edge:
        return 1.0
    if edge >= marginal_edge:
        return 0.5
    return None


def size_stake_dollars(
    mode: str,
    kelly_frac: float | None,
    pool: float | None,
    model_prob: float | None,
    market_price: float | None,
    unit_dollars: float | None,
    flat_marginal_edge: float = FLAT_MARGINAL_UNIT_EDGE,
    flat_full_edge: float = FLAT_FULL_UNIT_EDGE,
    unit_scale: float = 1.0,
    min_market_price: float = 0.0,
    remaining_capacity: float | None = None,
    sport: str | None = None,
) -> float | None:
    """The single sizing dispatch every router calls. `kelly_frac is None`
    means the bet didn't qualify (min-edge/has-traded/CLV gate) -> no bet in
    EITHER mode. In "flat" mode a qualified bet is sized by unit tier
    (independent of the per-sport pool, so bets are meaningful on a small
    bankroll); in "kelly" mode it's the classic kelly_frac * pool.

    `min_market_price` -- refuse to size a bet the market prices below this.
    Futures pass FUTURES_MIN_MARKET_PRICE; see its comment for the record this
    is fitted to. Enforced HERE rather than in the frontend on purpose: the
    cross-sport futures list filters on `suggested_stake_dollars != null`, so
    putting the rule in the one function every gate already feeds into means the
    view cannot drift out of step with it, and the per-sport Futures pages still
    show the row (unsized) for calibration tracking.

    `remaining_capacity` -- dollars still available on this bet's SIDE (game or
    futures) under the bankroll exposure caps, from models/exposure.py. None
    means uncapped, which is the safe default for any caller that hasn't been
    taught about caps yet. 0 or less means the side is full and the bet is shown
    unsized rather than stacked on top; a stake larger than what's left is
    trimmed to fit rather than refused outright, so the last slot in a side is
    usable instead of being wasted.

    `sport` -- applies the PER-SPORT futures ceiling as well as the global one
    (exposure.DEFAULT_FUTURES_PER_SPORT_CAP_FRACTION). Ignored on the game side.
    Optional on purpose: a futures caller that omits it still gets the global
    cap, so forgetting one router degrades to the old safe behaviour rather than
    to no cap at all. See exposure.py for why a per-sport ceiling is used
    instead of ranking all futures globally by edge.
    """
    if kelly_frac is None:
        return None
    if min_market_price > 0.0 and (market_price is None or market_price < min_market_price):
        return None
    if remaining_capacity is None:
        # Not passed -> read the snapshot the settings choke point refreshed.
        # This is what makes the cap un-forgettable: no router has to opt in.
        from app.models.exposure import remaining_for_unit_scale

        remaining_capacity = remaining_for_unit_scale(unit_scale, sport)
    if remaining_capacity is not None and remaining_capacity <= 0:
        return None
    if mode == "flat":
        if unit_dollars is None or unit_dollars <= 0:
            return None
        units = flat_stake_units(model_prob, market_price, flat_marginal_edge, flat_full_edge)
        # unit_scale is FUTURES_UNIT_SCALE for season-long markets -- see its
        # docstring for why an unscaled unit blocked them entirely.
        stake = round(units * unit_dollars * unit_scale, 2) if units else None
    else:
        stake = suggested_stake_dollars(kelly_frac, pool)
    if stake is not None and remaining_capacity is not None:
        stake = round(min(stake, remaining_capacity), 2)
        if stake <= 0:
            return None
    return stake
