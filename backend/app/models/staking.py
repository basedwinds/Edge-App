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
import unicodedata
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
MIN_EDGE_TO_BET = 0.20
# RAISED 0.03 -> 0.20 on 2026-08-17, and the reason is that 0.03 was inert.
#
# 520 settled tracked bets say the model's edge SCALE is inflated about 7x: it
# claims +24.1pp on average and delivers +3.5pp. A 3pp gate against a 7x-inflated
# number is really a ~0.4pp gate, which is no gate at all -- 92% of the live board
# cleared it, and raising it to 10pp changed literally nothing (431 rows both ways).
#
# DELIVERED EDGE RISES MONOTONICALLY WITH CLAIMED EDGE, which is the opposite of
# what I expected (I assumed the biggest claimed edges were the artifacts):
#
#     model claims        n    delivers    ROI
#      +4..+13pp         52     -2.4pp    -11.0%
#     +13..+15pp         52     -0.5pp    -12.0%
#     +15..+21pp        104     ~0.0pp     +7%
#     +24..+33pp        104     +3.9pp    +11%
#     +33..+39pp         52     +9.5pp    +29.3%
#     +39..+72pp         52    +18.4pp    +54.0%
#
# CHOSEN ON TRAIN, SPENT ONCE ON TEST. Threshold picked using only bets settled
# before 2026-08-11, then evaluated on the later window it had never seen:
#
#     gate        TRAIN ROI    TEST ROI
#     none          +22.4%       -6.1%
#     >=20pp        +32.0%       +8.6%
#     >=25pp        +39.9%       +9.7%
#     >=35pp        +51.7%      +20.3%
#
# Monotone in BOTH windows. The monotonicity across an independent sample is the
# robust part; the exact number is not, which is why this sits at the conservative
# end (20pp) rather than at the train optimum (35pp, but only n=29 in test).
#
# NOT a temperature calibration. Globally shrinking model_prob toward the market
# was already REJECTED on ECE in #192 -- this changes WHICH bets are taken, not
# what the model believes.
#
# COST: the live board goes from 466 staked rows to about 60. That is the point.
# The 208 bets in the bottom four claimed-edge deciles delivered nothing while
# paying spread on every one.

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

# A standing BID above this proves the market still books a futures leg as able
# to win. Used by the esports tournament gate (#207) to tell a live team from an
# eliminated one when the model itself cannot see the bracket: nobody bids to BUY
# a team that is already out, so a real bid is the market asserting the team is
# alive. An eliminated leg collapses to bid 0 with an ask around a cent.
# One cent, not a fitted number -- it is the exchange's minimum tick, so this
# says "someone is bidding at all" rather than imposing a price view.
MIN_LIVE_FUTURES_BID = 0.01


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


def implausible_certainty(model_prob: float, market_price: float) -> bool:
    """The OTHER half of the same pathology: the model claims near-certainty
    where the market still sees real doubt.

    `implausible_disagreement` covers the two LONGSHOT quadrants -- the model
    liking a side the market prices as remote (its `market <= 0.5` branch) and
    the model hating a side the market prices as near-locked (its complement
    branch, the "market says 94%, model says 3%" case in its docstring). Both
    are bets ON a longshot.

    Neither branch fires when the model is the more extreme one toward the
    FAVOURITE. Found 2026-08-15 while scoping the NO side (#186), on the live
    board:

        cs2  Spirit vs BIG    market 0.175  model 0.0172   BIG to win a Bo3
        lol  T1 vs DN Freecs  market 0.500  model 0.0212

    A 1.7% for a real CS2 team in a best-of-three is not a probability the model
    has any basis to assert, and 2.1% against a market of exactly even money is
    worse. Under `implausible_disagreement` both score ~0.1x and sail through,
    because it only measures the ratio in the direction that makes a longshot
    look good.

    THIS IS NOT A NO-SIDE GUARD -- it is a gap on BOTH sides, and closing it
    changes YES behaviour too. Measured on the live board: 121 YES bets before,
    120 after (one row where the model asserted a near-lock the market priced
    with real doubt), and 463 NO candidates before, 461 after. Disclosed rather
    than silently absorbed, because a guard that moves live YES exposure is not
    a no-op no matter how small the count.

    Same unit and same threshold as its sibling, for the same reason: an odds
    ratio has no cliff and treats both tails alike."""
    if not (0.0 < market_price < 1.0):
        return False
    if not (0.0 < model_prob < 1.0):
        return False        # the exact-0/1 guard owns that case
    if market_price <= 0.5:
        # Market sees doubt; model says the outcome is far MORE remote than that.
        ratio = market_price / model_prob
    else:
        # Mirror above 0.5: model says the complement is far more remote.
        ratio = (1.0 - market_price) / (1.0 - model_prob)
    return ratio >= IMPLAUSIBLE_ODDS_RATIO


# HOW CLOSE TO A COIN FLIP THE MODEL MAY BE AND STILL SIZE A BET.
#
# A 20pp edge is not one thing. The same 20pp means something completely
# different depending on where the model itself is standing, and the outcome
# record separates them cleanly. Every bet below had ALREADY cleared
# min_edge_to_bet, bucketed by |model_prob - 0.5|, staked rows only, settled:
#
#                        ESPORTS                     NON-ESPORTS
#     within 6pp    n= 177  ROI  -0.55%          n= 901  ROI  -0.37%
#     6-15pp        n= 208  ROI  +3.71%          n=1040  ROI  +1.86%
#     >15pp         n= 366  ROI  +7.12%          n=1487  ROI  +3.79%
#
# Monotonic in both families, and NOT a price artifact -- average market price
# is 0.36-0.47 across every band, so these are not longshot books versus even
# ones. The coin-flip band is 1,078 settled bets returning roughly nothing.
#
# WHY IT HAPPENS, caught 2026-08-20 on a live $10 LoL bet the user flagged
# ("series winner NO for DRX at 72% but they are a massive favourite"):
# elo_lol trains on a Leaguepedia crawl of PRIMARY TIER ONLY (LCK/LPL/LEC/
# LCS-LTA/Worlds/MSI). A Challengers-league side therefore has no historical
# data at all and carries a near-default rating built from a handful of
# live-polled maps -- DRX Challengers 1534 (5 maps) against OKSavingsBank
# BRION Challengers 1532 (9 maps). Two ratings 2 points apart produce a 50.6%
# series probability, the market said 72%, and the app booked the 21.4pp gap as
# an edge and staked the NO side. The model was not disagreeing with the
# market. It had no information and said so in a voice indistinguishable from
# a real read.
#
# THIS IS THE CHEAP DIRECTION TO BE WRONG IN. If the effect is noise, the cost
# is forgoing a band that measurably returned ~0; if it is real, it stops the
# app systematically betting against the market precisely where the market
# knows more. Capital freed goes to the >15pp band, which returns +4-7%.
#
# NOT a gate on thin ratings, deliberately. Thin CS2 ratings were measured and
# turned out to be the BEST-calibrated bucket, and a racing thin-rating gate
# was REJECTED on the same evidence. Nor is it a gate on the model being
# UNCERTAIN in general -- two well-rated, genuinely even teams belong at 50%.
# It gates the size of the CLAIM being sized, which is the quantity the outcome
# record actually separates.
UNINFORMATIVE_MODEL_BAND = 0.06


def uninformative_model(model_prob: float | None) -> bool:
    """True when the model's own estimate is close enough to a coin flip that
    its edge has no measured value, whatever the market price is."""
    if model_prob is None:
        return False
    return abs(model_prob - 0.5) <= UNINFORMATIVE_MODEL_BAND


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
    # A MODEL PROBABILITY OF EXACTLY 0 OR 1 IS NOT A PROBABILITY (2026-08-14).
    #
    # The season sims are Monte Carlo. When an outcome comes up in none of N
    # runs the sim reports 0.0000 -- which means "below the resolution of this
    # simulation", NOT "impossible". Nothing downstream knew the difference, so
    # a sim that simply never rolled a given outcome produced a certainty the
    # model cannot actually justify, and the edge computed off it is the market
    # price itself.
    #
    # Found by artifact-scanning the top of the board 2026-08-14: 277 priced
    # rows carried model_prob of exactly 0 or 1 --
    #
    #     cfb     141   conference_champion 28, quarterfinal 26, finalist 25
    #     racing  122   drivers_champion 104, constructors_champion 18
    #     lol      10   series_total          wnba 4  win_total
    #
    # The CFB rows settle it: this was scanned in mid-AUGUST, before a snap had
    # been played, so no team's conference-title probability is genuinely zero.
    #
    # PREVENTIVE, NOT A LIVE BLEED: 0 of the 277 were staked when this was
    # written, because a 0.0 model against any positive market price gives a
    # NEGATIVE edge and kelly already refuses those. The trap is unsprung, not
    # absent -- it springs the moment either (a) the 5 rows at model_prob 1.0
    # meet a cheap enough market, or (b) the NO side is ever surfaced, where
    # these rank at the TOP of the board by construction (8 of the top 25).
    # Laying 97c to win 3c on a probability the sim cannot resolve is the worst
    # risk/reward on the board, and it would look like its best edge.
    #
    # DELIBERATELY ONLY THE EXACT 0/1 CASE. A merely small probability (374 rows
    # under 1%) can be perfectly real, and a blanket longshot floor was already
    # measured and REJECTED once; FUTURES_MIN_MARKET_PRICE covers the price side.
    # This refuses the values that are not probabilities at all, nothing more.
    if model_prob is not None and (model_prob <= 0.0 or model_prob >= 1.0):
        return None

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
    # A 20pp edge measured from a coin flip is worth nothing -- see
    # UNINFORMATIVE_MODEL_BAND. Checked before the price guards because it is a
    # property of the model's claim alone and does not need a market at all.
    if uninformative_model(model_prob):
        return None

    if model_prob is not None and market_price is not None:
        if implausible_disagreement(model_prob, market_price):
            return None
        if implausible_certainty(model_prob, market_price):
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


# ---------------------------------------------------------------- NO side ---
#
# WHICH CELLS MAY BET THE NO SIDE, AND WHY ONLY THESE (#186, 2026-08-15).
#
# `kelly_fraction` refuses negative edge, so the app has only ever surfaced YES.
# That leaves the larger half of the board unharvested: 1,858 rows at >= +10pp
# against 3,642 at <= -10pp. A backtest over 11,166 settled `model_observations`
# said the unharvested half pays -- NO +16.0% [+10.5,+21.5] vs YES +15.2%
# [+9.4,+21.0], with a control arm (|edge| < 2pp) near zero on both sides, which
# is what makes the grading and the price field believable.
#
# THE AGGREGATE IS NOT THE BUILD. Decomposed by cell, it is concentrated, and
# two of the pieces reverse the naive reading:
#
#     tennis  set_spread     n= 282  +24.7%        cs2  series_winner n=149 +14.2%
#     tennis  set_winner     n= 231  +31.5%        lol  series_winner n= 70 +13.5%
#     tennis  game_total     n= 101  +52.8%
#     tennis  total_sets     n=  78  +11.6%
#     tennis  MONEYLINE      n= 363   -0.9%   <-- the liquid one does NOT pay
#     soccer  (all)          n= 105   -0.7%   <-- but would be 310 of 461 live
#     valorant series_winner n=  46   -1.9%
#     mlb / wnba             n= 273   ~+3%    noise
#
# Tennis moneyline losing is exactly what #192 predicts: that model's logistic is
# too steep (claimed 0.844 delivered 0.685 at the top, mirrored at the bottom),
# so its extreme calls are wrong in the direction that sinks a NO bet. The +20.3%
# "tennis" headline came entirely from the DERIVED markets, a different model
# path that #192 never touched. Aggregating the two hid both facts.
#
# SO THE ALLOWLIST IS THE EVIDENCE, NOT A SPORT LIST. A cell is here only if it
# has settled rows of its own showing profit. Soccer is excluded despite being
# the largest live source of NO candidates precisely because it is the largest:
# shipping 310 unmeasured bets on the strength of an aggregate driven by other
# sports is the mistake this comment exists to prevent.
#
# NOT YET MEASURABLE, deliberately absent: cfb, nfl, racing, mma have ZERO
# settled NO observations. Silence is not evidence either way -- revisit when
# `calibration_report.py` has rows for them.
NO_SIDE_CELLS = {
    ("tennis", "set_spread"),
    ("tennis", "set_winner"),
    ("tennis", "game_total"),
    ("tennis", "total_sets"),
    ("cs2", "series_winner"),
    ("lol", "series_winner"),
}


def no_side_allowed(sport: str | None, market_type: str | None) -> bool:
    """True if this cell has its own settled evidence for betting NO."""
    return (sport, market_type) in NO_SIDE_CELLS


def no_side_inputs(
    sport: str | None,
    market_type: str | None,
    model_prob: float | None,
    market_price: float | None,
    yes_bid: float | None,
    yes_ask: float | None,
) -> "tuple[float, float, float | None, float | None] | None":
    """Complement inputs for pricing the NO side, or None if this cell may not.

    Returns `(model_prob, market_price, bid, ask)` already in the NO frame, so a
    router feeds them to the SAME `kelly_fraction` / `size_stake_dollars` calls
    it already makes. Every gate then applies untouched -- which is the point.
    The alternative, a parallel NO pricing path per router, is how the spread
    guard ended up on 3 of 13 routers and the duplicate cap on 4 of 13.

    NOTE THE BID/ASK SWAP, it is not a typo:

        NO bid = 1 - yes_ask        NO ask = 1 - yes_bid

    You BUY the NO at `1 - yes_bid` (you cross to whoever is bidding for YES),
    so that is the ask in this frame. The swap also keeps the spread invariant:
    (1 - yes_bid) - (1 - yes_ask) == yes_ask - yes_bid, so `size_stake_dollars`'
    `max_spread` check measures the same book width either way and needs no
    NO-specific constant."""
    if not no_side_allowed(sport, market_type):
        return None
    if model_prob is None or market_price is None:
        return None
    return (
        1.0 - model_prob,
        1.0 - market_price,
        (1.0 - yes_ask) if yes_ask is not None else None,
        (1.0 - yes_bid) if yes_bid is not None else None,
    )


def kelly_fraction_no(
    model_prob: float | None,
    market_price: float | None,
    yes_bid: float | None = None,
    **kwargs,
) -> float | None:
    """Stake fraction for betting the NO side, or None.

    Arguments are in the YES frame -- the same `model_prob` and midpoint
    `market_price` every router already computes -- because asking callers to
    invert them is asking thirteen routers to each get a subtraction right.

    EVERY GUARD IS INHERITED, NOT REIMPLEMENTED. A NO bet is arithmetically a
    YES bet on the complement:

        NO model prob = 1 - model_prob
        NO midpoint   = 1 - market_price
        NO ask (paid) = 1 - yes_bid      <- mirror of paying the YES ask

    so this delegates to `kelly_fraction` under that substitution and the
    exact-0/1 guard, both implausibility guards, `has_traded`, the ask guard and
    the Kelly maths all apply unchanged. Reimplementing them here would be the
    partial-wiring defect this codebase keeps producing.

    THE ASK MATTERS MORE HERE, NOT LESS. Betting NO costs `1 - yes_bid` against
    a `1 - mid` midpoint, so the haircut is half the spread either way -- the
    economics are symmetric. What is NOT symmetric is the inventory: the tennis
    cells that pay are 79-97% untraded and the tennis cell that is liquid does
    not pay. `has_traded` and the spread cap are what keep that from being
    surfaced as edge, so callers must keep passing them.

    Caller is responsible for `no_side_allowed` -- this function will happily
    size a soccer NO bet, and soccer NO measured -0.7%."""
    if model_prob is None or market_price is None:
        return None
    return kelly_fraction(
        model_prob=1.0 - model_prob,
        market_price=1.0 - market_price,
        execution_price=(1.0 - yes_bid) if yes_bid is not None else None,
        **kwargs,
    )


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

# WIDEST BID/ASK A FUTURES BET MAY BE STAKED INTO (2026-08-13).
#
# THE FAILURE THIS CATCHES. A futures rung with no real book still reports a
# "price" -- a stale midpoint or last trade -- and the model's edge is measured
# against it, so an untraded market reads as a huge edge. Found live: an NFL
# wins_any leg staked at bid 0.02 / ask 0.46, model 0.815 vs a quoted 0.24, edge
# +0.587. There is no market there to beat.
#
# WHY 0.30 AND NOT A ROUND NUMBER. It is tied to the app's own recommend
# threshold rather than picked. A spread of S puts +/- S/2 of uncertainty on the
# midpoint the edge is measured from, so at S = 0.30 that uncertainty is 0.15 --
# larger than the 10pp edge the bet is being recommended on. Past that point the
# claimed edge is smaller than the error bar on its own reference price.
#
# MEASURED IMPACT, stated honestly: futures books are thin everywhere (p50
# spread 0.120, p75 0.310 across 2,435 quoted legs), but the STAKED set is
# already much tighter, because has_real_trading and the price floor filter most
# of the junk first. This blocks ONE currently-staked leg -- the wins_any one
# above. It is cheap insurance for the futures test tranche, not a large repair.
#
# A MISSING BOOK IS NOT A WIDE BOOK. Kalshi stores bid/ask on every futures row
# (2,435/2,435); several Polymarket ingesters store none (177/177 missing),
# because bestBid/bestAsk are per-outcome-index on a 2-outcome Gamma object and
# were deliberately not threaded through (see has_real_trading). Blocking on
# absence would silently kill those sports' futures entirely, so absence falls
# through to the volume gate instead.
FUTURES_MAX_SPREAD = 0.30


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
    max_spread: float | None = None,
    yes_bid: float | None = None,
    yes_ask: float | None = None,
    remaining_capacity: float | None = None,
    sport: str | None = None,
    team: str | None = None,
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

    `max_spread` with `yes_bid`/`yes_ask` -- refuse to size a bet whose book is
    wider than the edge being claimed against its midpoint. Futures pass
    FUTURES_MAX_SPREAD; see its comment. Enforced here for the same reason
    `min_market_price` is: the cross-sport list filters on
    `suggested_stake_dollars != null`, so the one function every gate feeds into
    is the only place the view cannot drift out of step with. Both quotes must
    be present -- a missing book falls through to the volume gate rather than
    being treated as a wide one.

    `remaining_capacity` -- dollars still available on this bet's SIDE (game or
    futures) under the bankroll exposure caps, from models/exposure.py. None
    means uncapped, which is the safe default for any caller that hasn't been
    taught about caps yet. 0 or less means the side is full and the bet is shown
    unsized rather than stacked on top; a stake larger than what's left is
    trimmed to fit rather than refused outright, so the last slot in a side is
    usable instead of being wasted.

    `team` -- applies the PER-TEAM futures ceiling too (a backstop against one
    club/franchise dominating the futures book across several market types).
    Same optional-by-design contract as `sport`.

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
    # Refuse a book too wide for its own quoted price to mean anything. Only
    # when BOTH sides are quoted: a missing book is not a wide one (see
    # FUTURES_MAX_SPREAD) and is left to the volume gate.
    if (max_spread is not None and yes_bid is not None and yes_ask is not None
            and (yes_ask - yes_bid) > max_spread):
        return None
    if remaining_capacity is None:
        # Not passed -> read the snapshot the settings choke point refreshed.
        # This is what makes the cap un-forgettable: no router has to opt in.
        from app.models.exposure import remaining_for_unit_scale

        remaining_capacity = remaining_for_unit_scale(unit_scale, sport, team)
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


# ---------------------------------------------------------------------------
# Nested futures: one opinion, listed as several markets.
# ---------------------------------------------------------------------------
#
# A bracket lists the SAME opinion at several depths. For College Football:
# making the 12-team field, earning a top-4 seed, reaching the quarterfinal,
# the semifinal, the final, and winning it. For one team those are nested --
# each implies the one before it -- so staking them separately is not four
# bets, it is one position sized four times.
#
# Found live on 2026-08-10: Indiana was staked on four legs at once (+21.8pp
# playoff, +41.6pp quarterfinal, +44.8pp finalist, +32.6pp champion), roughly
# 4x the intended single-team exposure.
#
# Combined.tsx already collapses the WITHIN-type version of this (a win-total
# ladder: 45+ implies 40+ implies 35+). It cannot collapse this one, because
# its dedupe key includes market_type and these are six different types. Same
# bug, one level up.
#
# The nesting is VERIFIED against the model's own output rather than assumed:
# across 50 teams carrying two or more legs, 156 adjacent comparisons, zero
# violations of P(playoff) >= P(QF) >= P(semi) >= P(final) >= P(champion), and
# top4_seed <= quarterfinal for all 45 comparable teams (a bye means you are
# already in the quarterfinal).
#
# Widest-first, and each tuple must stay ordered that way.
NESTED_FUTURES_FAMILIES: dict[str, tuple[tuple[str, ...], ...]] = {
    "cfb": (
        (
            "cfb_playoff",
            "cfb_top4_seed",
            "cfb_quarterfinal",
            "cfb_semifinal",
            "cfb_finalist",
            "cfb_national_champion",
        ),
    ),
}

NESTED_LEG_NOTE = (
    "Not staked separately: this is a narrower leg of a bet already taken on the same team "
    "(the bracket legs are nested, so each implies the one before it). Staking them all would "
    "size one position several times over. Shown for tracking."
)


def apply_nested_futures_cap(rows: list, sport: str) -> int:
    """Keep ONE staked leg per (team, family); zero the stake on the rest.

    Returns how many legs were zeroed. Mutates rows in place.

    WHICH LEG SURVIVES: the WIDEST one that the staking gates already approved
    -- deliberately not the highest-edge one.
    -
    Taking the biggest edge looks obviously right and is backwards here. Each
    extra round compounds elo_cfb's deliberately wide rating spread, so the
    deeper the leg, the more inflated its model probability, and therefore the
    larger its apparent edge (playoff_sim_cfb's own docstring says exactly
    this, and that the qualification markets "which need only one round" are
    much less affected). Indiana was the live case: +21.8pp on the widest leg
    rising to +44.8pp on a narrow one. Selecting on edge would systematically
    pick the most compounded, least trustworthy leg every time -- adverse
    selection against ourselves.
    -
    This only ever REMOVES stakes, never adds one, so it cannot promote a leg
    that some other gate rejected: the survivor is chosen from rows that were
    already staked.
    """
    families = NESTED_FUTURES_FAMILIES.get(sport)
    if not families:
        return 0
    zeroed = 0
    for family in families:
        depth = {mt: i for i, mt in enumerate(family)}
        best: dict[str, int] = {}          # team -> depth of the widest staked leg
        for r in rows:
            d = depth.get(r.market_type)
            if d is None or not r.team or not r.suggested_stake_dollars:
                continue
            if r.team not in best or d < best[r.team]:
                best[r.team] = d
        for r in rows:
            d = depth.get(r.market_type)
            if d is None or not r.team or not r.suggested_stake_dollars:
                continue
            if d == best.get(r.team):
                continue
            _unstake(r, NESTED_LEG_NOTE)
            zeroed += 1
    return zeroed


# Market types whose rungs are a LADDER: several thresholds of one opinion,
# where clearing a higher rung implies clearing every lower one. Keyed by sport
# so a type name meaning different things in two sports cannot collide.
#
# Verified on live MLB data rather than assumed: across 30 (team, source)
# groups holding two or more win_total rungs, 171 adjacent comparisons, ZERO
# cases where a higher line carried a higher model probability. That is what
# makes "the widest rung is simply the highest model_prob" safe to rely on
# without hardcoding whether a ladder reads over or under.
LADDER_FUTURES_TYPES: dict[str, frozenset[str]] = {
    "mlb": frozenset({"win_total"}),
    # NFL, added 2026-08-11 after AFC East total wins was staked at BOTH
    # "over 30" and "over 32" at once -- one directional opinion sized twice,
    # since clearing 32 clears 30.
    #
    # exact_win_total is deliberately NOT here. It is not a ladder: "exactly
    # 10 wins" is not implied by "exactly 11", so the rungs are genuinely
    # different propositions and collapsing them would drop a real bet. Only
    # cumulative over/under types belong in this map.
    "nfl": frozenset({"win_total", "division_wins"}),
}

LADDER_RUNG_NOTE = (
    "Not staked separately: a higher rung of the same ladder for this team is already staked, "
    "and clearing the higher rung implies clearing this one. Staking both would size one "
    "opinion twice. Shown for tracking."
)

DUPLICATE_LISTING_NOTE = (
    "Not staked separately: the identical market is already staked on the other platform at a "
    "better price. Shown for tracking."
)

COMPLEMENTARY_LEG_NOTE = (
    "Not staked separately: this is the same bet as the other side of the market, which is "
    "already staked. Shown for tracking."
)

# How far two model probabilities may miss summing to 1.0 and still be treated as
# the two halves of one binary market. Tight on purpose -- the observed pairs sum
# to 1.0000 exactly, because they come from ONE model call whose complement is
# computed, not from two independent estimates.
COMPLEMENTARY_PROB_TOLERANCE = 0.005


def apply_complementary_leg_cap(rows: list, fixture_attr: str | None = None,
                                entity_attr: str = "team") -> int:
    """One stake per proposition when a BINARY market is listed as both of its
    outcomes and the app buys YES on one and NO on the other.

    THIS IS NOT apply_duplicate_listing_cap. That one collapses the SAME listing
    carried by two platforms, keyed on (team, market_type, line, side) -- so by
    construction it cannot see this, where the two rows have different teams and,
    for a handicap, different lines. Same defect (double exposure on one view),
    different mechanism, and the first cap being wired everywhere would not have
    prevented any of it.

    WHY IT APPEARED NOW. The NO side shipped 2026-08-15 (#186) as an allowlist.
    For any two-outcome market, "YES on A" and "NO on B" are the SAME bet, and
    they carry the SAME edge by construction -- so whenever one clears the gate
    the other clears it too, every time. It is systematic, not occasional.

    Found 2026-08-20 on the live board, three shapes, all one defect:
        tennis set_spread   Montagud -1.5 YES  ==  Contri +1.5 NO     $20
        cs2 series_winner   Bushido YES        ==  Raccoons NO        $20
        cs2 series_winner   HyperSpirit YES    ==  Just Players NO    $20
    Four of six staked CS2 rows and two of three staked tennis rows were two
    halves of one bet. No REAL money had been double-staked yet -- the historical
    placed-bet book has zero such pairs -- so this is preventive.

    IDENTITY IS MEASURED, NOT ENUMERATED. Two legs are the same proposition when
    their MODEL probabilities sum to 1.0 and their positions are opposite. That
    test is self-verifying and needs no list of market types to maintain: the
    observed pairs sum to 1.0000 exactly, while a genuine three-way market cannot
    trigger it (a soccer home/draw pair sums to ~0.73). MARKET prices are NOT
    used for this -- the spread means the two sides sum to ~0.93, not 1.0.

    Keeping the better buying-side edge is right for the same reason it is right
    in apply_duplicate_listing_cap: identical proposition, identical model, so a
    bigger edge is purely a better price. `edge` is stored in the YES frame, so a
    NO row's buying-side edge is its negation.

    WIRE THIS WHEREVER `no_side_allowed` CAN RETURN TRUE. Today that is
    NO_SIDE_CELLS = tennis (set_spread/set_winner/game_total/total_sets), cs2 and
    lol (series_winner). A YES-only sport cannot produce the pair at all. If
    NO_SIDE_CELLS grows, this must be wired for the new sport in the same commit
    -- the last cap sat in only 4 of 13 routers for weeks because nobody checked.
    """
    def _entity(r):
        return getattr(r, entity_attr, None)

    def _buy_edge(r):
        e = r.edge
        if e is None:
            return None
        return -e if getattr(r, "position", None) == "no" else e

    groups: dict[tuple, list] = {}
    for r in rows:
        if not r.suggested_stake_dollars or r.model_prob is None:
            continue
        fixture = getattr(r, fixture_attr, None) if fixture_attr else None
        if fixture is None:
            continue          # without a fixture scope two unrelated events could pair up
        groups.setdefault((fixture, r.market_type), []).append(r)

    zeroed = 0
    for legs in groups.values():
        if len(legs) < 2:
            continue
        # Deterministic order so which leg survives cannot flap between refreshes
        # when two identical edges tie.
        legs.sort(key=lambda r: (-(_buy_edge(r) or -9), getattr(r, "id", 0)))
        kept: list = []
        for r in legs:
            twin = next(
                (k for k in kept
                 if getattr(k, "position", None) != getattr(r, "position", None)
                 and _entity(k) != _entity(r)
                 and abs((k.model_prob or 0) + (r.model_prob or 0) - 1.0) <= COMPLEMENTARY_PROB_TOLERANCE),
                None,
            )
            if twin is None:
                kept.append(r)
                continue
            _unstake(r, COMPLEMENTARY_LEG_NOTE)
            zeroed += 1
    return zeroed


def apply_ladder_futures_cap(rows: list, sport: str) -> int:
    """One stake per (team, ladder type); keep the WIDEST rung. Mutates in place.

    Widest = highest model_prob, which the monotonicity check above establishes
    is the lowest threshold of an over-ladder (and the highest of an under-one)
    without this needing to know which it is.

    Same reasoning as the nested-bracket cap: the rungs are not independent
    bets, so sizing each one separately multiplies a single position. Note this
    is scoped per SOURCE-independent team+type, so it also collapses the case
    where the ladder is listed on both platforms.
    """
    types = LADDER_FUTURES_TYPES.get(sport)
    if not types:
        return 0
    best: dict[tuple, object] = {}
    for r in rows:
        if r.market_type not in types or not r.team or not r.suggested_stake_dollars:
            continue
        key = (r.team, r.market_type)
        cur = best.get(key)
        if cur is None or (r.model_prob or 0) > (cur.model_prob or 0):
            best[key] = r
    zeroed = 0
    for r in rows:
        if r.market_type not in types or not r.team or not r.suggested_stake_dollars:
            continue
        if best.get((r.team, r.market_type)) is r:
            continue
        _unstake(r, LADDER_RUNG_NOTE)
        zeroed += 1
    return zeroed


def apply_duplicate_listing_cap(rows: list, fixture_attr: str | None = None,
                                entity_attr: str = "team") -> int:
    """One stake per identical proposition listed on BOTH platforms.

    Identity is (team, market_type, line, side) -- deliberately excluding
    `source`, because that is the whole point: Kalshi's "National League
    Champion" and Polymarket's "MLB: 2026 National League" are the same
    outcome, and Milwaukee was staked on both at once for 2x the intended
    exposure. 120 MLB rows are listed on both platforms, so this was latent
    across far more than the one pair that happened to clear the gate.

    HERE, KEEPING THE BEST EDGE IS CORRECT -- and that is not a contradiction
    of the nested-bracket cap, which deliberately refuses to. These rows are
    the SAME proposition carrying the SAME model_prob, so a bigger edge means
    only a better price; it is best execution, not adverse selection. Nested
    legs are different propositions whose deeper rungs are less trustworthy,
    which is why edge is the wrong selector there and the right one here.
    """
    def _entity(r):
        # `entity_attr` exists because not every row model calls it `team`.
        # Racing rows carry `driver` and have no `side` at all, so reading
        # r.team/r.side directly would raise on them -- or, worse, silently
        # collapse every racing row onto one key and unstake almost all of them.
        # Found 2026-08-11 while extending this cap to racing, BEFORE wiring it.
        return getattr(r, entity_attr, None)

    def _entity_key(r):
        """The entity with INVISIBLE characters removed.

        FOUND LIVE 2026-08-17, reported by the user as "why is a negative edge
        bet on my board". Polymarket lists a LoL team as
        '⁠Movistar KOI Fenix' -- a leading U+2060 WORD JOINER, which renders
        as nothing -- while Kalshi lists the same team clean. Both rows pointed at
        the SAME lol_match_id (765), both were NO bets on the same outcome, and
        both were staked $10. Two identical propositions, double exposure. Exactly
        the failure this cap exists to prevent, walked straight past because the
        key compared RAW strings.

        Note the matcher was never fooled: normalize_team_name does NFKD +
        ascii-ignore, which drops U+2060, so the fixture join was correct all
        along. Only this key saw two different teams.

        STRIPS UNICODE CATEGORY Cf ONLY (format characters: word joiner, the
        zero-width space/joiner/non-joiner, BOM, the bidi marks). These carry no
        linguistic content whatsoever, so two names differing only by them are the
        same name -- no judgement call, and nothing legitimate can collapse.
        Deliberately NOT full normalize_team_name: lowercasing and stripping
        punctuation is a bigger hammer that risks merging genuinely distinct
        rosters, and this cap already zeroes real money when it fires.
        """
        e = _entity(r)
        if not isinstance(e, str):
            return e
        return "".join(c for c in e if unicodedata.category(c) != "Cf")

    def _key(r):
        # fixture_attr scopes the identity to ONE real-world event. Futures need
        # no such scope (a team has one season), but a tennis player appears in
        # many matches, so without it two different matches for the same player
        # would collapse into one and a legitimate second bet would be dropped.
        fixture = getattr(r, fixture_attr, None) if fixture_attr else None
        return (fixture, _entity_key(r), r.market_type, getattr(r, "line", None),
                getattr(r, "side", None))

    best: dict[tuple, object] = {}
    for r in rows:
        if not _entity(r) or not r.suggested_stake_dollars:
            continue
        key = _key(r)
        cur = best.get(key)
        if cur is None or (r.edge or -9) > (cur.edge or -9):
            best[key] = r
    zeroed = 0
    for r in rows:
        if not _entity(r) or not r.suggested_stake_dollars:
            continue
        if best.get(_key(r)) is r:
            continue
        _unstake(r, DUPLICATE_LISTING_NOTE)
        zeroed += 1
    return zeroed


def _unstake(row, note: str) -> None:
    """Drop the stake but keep the model number, so the row still shows for
    tracking and calibration. Any existing note is KEPT and appended to -- a row
    must never lose an earlier caveat (the approximate badge) to this pass."""
    row.suggested_stake_dollars = None
    row.suggested_stake_units = None
    row.kelly_fraction = None
    row.stake_pool = None
    # Not every payload carries model_note -- TennisMarketOut, for one, has no
    # such field, and assigning an undeclared attribute on a pydantic model
    # raises. The stake removal is the part that matters; annotate where we can.
    if hasattr(row, "model_note"):
        row.model_note = f"{row.model_note} {note}" if row.model_note else note
