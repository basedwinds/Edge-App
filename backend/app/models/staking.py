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


def kelly_fraction(
    model_prob: float | None,
    market_price: float | None,
    fractional_kelly: float = FRACTIONAL_KELLY,
    max_stake_fraction: float = MAX_STAKE_FRACTION,
    min_edge_to_bet: float = MIN_EDGE_TO_BET,
    has_traded: bool = True,
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
    if model_prob is None or market_price is None:
        return None
    if market_price <= 0.0 or market_price >= 1.0:
        return None
    if not has_traded:
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
# Deliberately does NOT scale UP past 1u for bigger edges: a large disagreement
# is a red flag (probable model error), not a reason to bet more -- see above.


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
) -> float | None:
    """The single sizing dispatch every router calls. `kelly_frac is None`
    means the bet didn't qualify (min-edge/has-traded/CLV gate) -> no bet in
    EITHER mode. In "flat" mode a qualified bet is sized by unit tier
    (independent of the per-sport pool, so bets are meaningful on a small
    bankroll); in "kelly" mode it's the classic kelly_frac * pool."""
    if kelly_frac is None:
        return None
    if mode == "flat":
        if unit_dollars is None or unit_dollars <= 0:
            return None
        units = flat_stake_units(model_prob, market_price, flat_marginal_edge, flat_full_edge)
        return round(units * unit_dollars, 2) if units else None
    return suggested_stake_dollars(kelly_frac, pool)
