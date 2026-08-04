"""Detects a real-world match/game/fight that has already started (or
concluded) using the MARKET'S OWN data, rather than any timestamp this app
stores -- a genuine structural tell, not a heuristic guess. Two independent
mechanisms live here: `find_resolved_entities`/`ladder_looks_resolved` use a
ladder market's cross-line PRICE SHAPE (needs 2+ thresholds on the same
question); `looks_already_live_by_trading` uses a single market's own
VOLUME+PRICE HISTORY instead, for market types (moneyline, first and
foremost) that have no second line to compare against. See each function's
own docstring for the real bug that motivated it.

Real finding (2026-07-19, user-reported): a Tennis ITF match ("Jake Delaney
vs Matt Hulme") showed Set 1 total-games priced at 99.55% "Over" across
THREE DIFFERENT lines (8.5, 9.5, AND 10.5) at once, and Set 2 similarly
pinned at 0.45% "Over" across the same three lines -- while this app's own
`estimated_start_time` for the match (Kalshi/Polymarket's own pre-match
guess, see tennis_markets.py/mma_markets.py's own "already started" checks)
still said the match hadn't begun. A real, still-undecided pregame ladder
NEVER prices multiple different thresholds of the same continuous number
identically -- Over 8.5 must be at least as likely as Over 9.5, which must
be at least as likely as Over 10.5, and in a genuinely uncertain pregame
market these probabilities differ by a real, visible amount. Two different
thresholds converging on the SAME extreme value only happens once the real
outcome is already locked in (Set 1 real game count already exceeds all
three lines; Set 2 already fell short of all three) and the market is just
echoing a known result, not pricing a future one.

This complements, not replaces, the existing "already started"/"already
decided"/status-based/staleness-based checks (see mma_markets.py/
tennis_markets.py's own docstrings for that whole history) -- those all
depend on some signal ABOUT the schedule; this one depends on nothing but
the shape of the market's own current prices, so it still fires even when
every timestamp this app has is wrong. Cheap and safe to run for every
sport: a genuinely still-live, undecided pregame ladder essentially never
produces two threshold rungs pinned to the same extreme value by chance, so
this has no real false-positive cost even where the schedule-based checks
(NFL/NBA/MLB's real official kickoff times) should already make it
redundant.
"""
EXTREME_LOW = 0.02
EXTREME_HIGH = 0.98
TIE_EPSILON = 0.005


def ladder_looks_resolved(rungs: list[tuple[float, float]]) -> bool:
    """`rungs` is a list of (line, implied_prob) for DIFFERENT thresholds of
    the SAME real-world ladder (same match/game/fight, same market_type,
    same side/team/set -- whatever actually identifies "the same underlying
    question" for that sport, see each router's own grouping). True if two
    DISTINCT lines both land at an extreme price within TIE_EPSILON of each
    other."""
    extreme = [(line, p) for line, p in rungs if p is not None and (p <= EXTREME_LOW or p >= EXTREME_HIGH)]
    for i in range(len(extreme)):
        line_a, p_a = extreme[i]
        for line_b, p_b in extreme[i + 1:]:
            if line_a != line_b and abs(p_a - p_b) <= TIE_EPSILON:
                return True
    return False


def find_resolved_entities(groups: dict[object, list[tuple[float, float]]]) -> set:
    """`groups` maps an entity id (game_id/fight_id/match_id -- whatever
    this sport's router uses to exclude a whole match's worth of markets
    at once) to that entity's own list of (line, implied_prob) ladder rungs,
    already pre-grouped by the caller (same real-world ladder: entity +
    market_type + team/side/set). Returns the subset of entity ids with at
    least one degenerate ladder per `ladder_looks_resolved` -- the caller
    excludes every market tied to those ids, not just the ladder ones,
    since a match/game/fight that's demonstrably already in progress or
    decided shouldn't show ANY of its markets as a fresh pregame estimate."""
    return {entity_id for entity_id, rungs in groups.items() if len(rungs) >= 2 and ladder_looks_resolved(rungs)}


# Real second finding (2026-07-19, same day, user-reported): a Tennis ITF
# moneyline (Thanaphat Boosarawongse vs Aniketh Venkataraman, Kalshi) was
# showing a live recommended bet at a 99%/1% price while the real match was
# already final -- `estimated_start_time` said the match hadn't started for
# another 3+ hours. The ladder trick above can't catch this: moneyline has
# no second threshold to compare against. A moneyline market has no
# structural "shape" tell at all -- but its OWN recent history does: real,
# live in-match trading looks completely different from a still-quiet
# pregame market, REGARDLESS of what any timestamp claims.
#
# REAL BUG this window-widening fixes (caught live 2026-07-19, same session,
# by re-running this exact validation a day later): the original 1-hour
# window missed a real, confirmed case ("Aditya Kothari vs Kabir Hans")
# whose price drifted from 0.88 to 0.99 gradually over ~4 real hours (traced
# via its full snapshot history -- continuous, accelerating volume growth
# the whole time, not a sudden spike), not concentrated in any single hour.
#
# Widening the window alone is NOT safe by itself, though -- confirmed live:
# "Aleksandar Vukic vs August Holmgren", a real, liquid, still-genuinely-
# pregame ATP Challenger match (estimated_start_time still ~7 real hours
# out), organically racked up sw=0.29/vd=770k+ over an 8-hour window just
# from real back-and-forth pregame trading -- its price OSCILLATED in a
# 0.49-0.72 range the entire time, never approaching either extreme. A big
# swing+volume over many hours is not by itself unusual for a genuinely
# still-open, liquid tour-level market. What Kothari/Hans has that Vukic/
# Holmgren doesn't: the CURRENT price is actually AT an extreme (0.99),
# meaning a real-world outcome looks already known -- Vukic/Holmgren's
# current price (0.68) is nowhere near settled. Requiring the current price
# to be extreme (same EXTREME_LOW/EXTREME_HIGH already used by
# ladder_looks_resolved above, for the same reason: a real, still-undecided
# match essentially never prices near 0 or 1) is what actually distinguishes
# "this looks resolved" from "this is just a liquid, volatile pregame
# market" -- re-validated with this gate added at 4h/6h/8h/12h/24h windows,
# all five produced the IDENTICAL result (only Kothari/Hans flagged, Vukic/
# Holmgren correctly excluded at every window size), and every other
# extreme-priced pregame Kalshi Tennis moneyline currently tracked showed
# volume delta <=2,650 -- a huge, unambiguous gap below Kothari/Hans's
# 395k-715k, not a close call. LIVE_TRADING_LOOKBACK (tennis_markets.py) is
# now 6 hours, comfortably covering a ~4-hour real swing with margin.
#
# Kalshi-only for now: Polymarket's volume figures are a completely
# different (much smaller, dollar-denominated) scale -- confirmed live
# elsewhere in this app -- so this specific numeric cutoff hasn't been
# validated there and would need its own real-data check before extending.
LIVE_TRADING_MIN_VOLUME_DELTA = 100_000.0
LIVE_TRADING_MIN_PRICE_SWING = 0.10

# Soccer-specific volume threshold (added 2026-07-19, this app's own first
# calibration pass for a sport OTHER than Tennis): checked live against
# real Kalshi Soccer moneyline volumes the same day this guard's shared
# default was reused for Soccer's own dead-market chain (soccer_markets.py)
# -- found a genuine, large scale mismatch, not a close call. Real current
# Kalshi Soccer moneyline market volumes topped out at 10,050 (single
# highest of 90 real tracked rows, MEDIAN just 10.45) -- 1-2 orders of
# magnitude below Tennis's own real validated incident (a confirmed live
# match's volume delta measured 395k-715k, comfortably above the shared
# 100,000 default). Applying Tennis's threshold to Soccer as-is would make
# this specific guard an effective no-op there: no real Soccer market
# currently tracked could ever cross 100,000 in a lifetime, let alone
# within one snapshot window.
#
# UNLIKE Tennis's number, this one is NOT validated against a real observed
# live-trading incident -- zero currently-tracked Soccer markets were
# anywhere near an extreme price at calibration time (checked live, 0/120),
# so there was no real case to check a candidate threshold against the way
# Kothari/Hans validated Tennis's. Scaled down from Tennis's number by
# roughly the real volume-scale gap just measured (roughly 1-2%, rounded to
# a clean number) rather than either leaving an ill-fitted number in place
# or inventing one with zero grounding -- explicitly PROVISIONAL, revisit
# once a real Soccer live-trading incident is observed and can be checked
# against it the same rigorous way Tennis's was.
SOCCER_LIVE_TRADING_MIN_VOLUME_DELTA = 2_000.0
SOCCER_LIVE_TRADING_MIN_PRICE_SWING = LIVE_TRADING_MIN_PRICE_SWING  # probability-space, not volume-scale-dependent -- no real reason to differ by sport

# Esports (2026-07-20, user-reported: LoL recommended bets pricing off
# already-decided matches, e.g. a 0.05%/99.95% map-winner price still shown
# as a fresh pregame estimate). Root cause found live: unlike every other
# sport here, none of the 3 esports titles (Valorant/CS2/LoL) had EITHER
# ladder_sanity.py mechanism wired in at all -- only the schedule-based
# `_match_already_started`/`_match_already_decided` checks existed, both of
# which depend on real data these 3 titles frequently don't have yet
# (vlr.gg/Leaguepedia/Liquipedia often leave estimated_start_time/winner
# unpopulated for a match still being actively scraped/matched, unlike
# nflverse/ESPN's much more complete official schedules) -- confirmed live:
# dozens of active esports markets sat at an extreme price with a real,
# validated live-trading pattern (started mid-range, swung hard, pinned at
# an extreme, real volume growth throughout) and NOTHING excluded them.
#
# Each title/platform combo got its OWN threshold, not one shared number,
# because their real observed volume scales are genuinely different --
# checked live against every currently-tracked market's own snapshot
# history (2026-07-20), same rigor as Soccer's own calibration above:
#   - CS2 Kalshi: real confirmed live-match deltas ranged 1,011-159,191
#     (series_winner "1WIN" swung 0.63->0.23->0.99 with volume 5->159,197).
#   - Valorant Kalshi: real confirmed deltas 12,066-19,184 (map_winner rows).
#   - LoL Kalshi: real confirmed deltas 472-2,067 (LoL's own Kalshi
#     inventory trades at a noticeably smaller real scale than CS2/
#     Valorant's -- confirmed live, not assumed).
#   - Valorant Polymarket: real confirmed deltas 20-482,905 (its OWN scale,
#     same "don't reuse Kalshi's number across platforms" rule as Tennis/
#     Soccer above) -- a real match (Gentle Mates GC vs G2 Gozen) swung
#     0.40-0.99 across series_winner/map_winner/series_total/
#     series_handicap simultaneously, with volume growing 66->49,606 on just
#     one of its 4 markets.
# Every threshold below sits clearly beneath its own title/platform's
# smallest real confirmed case, with margin -- same "provisional, revisit
# with more real incidents" status as Soccer's number, not treated as final.
# No dedicated CS2/LoL Polymarket numbers: neither title has any real
# Polymarket match-level inventory at all (see market_catalog_cs2.py/
# market_catalog_lol.py's own docstrings), so there's nothing to calibrate.
CS2_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA = 1_000.0
VALORANT_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA = 10_000.0
LOL_KALSHI_LIVE_TRADING_MIN_VOLUME_DELTA = 400.0
VALORANT_POLYMARKET_LIVE_TRADING_MIN_VOLUME_DELTA = 500.0
ESPORTS_LIVE_TRADING_MIN_PRICE_SWING = LIVE_TRADING_MIN_PRICE_SWING


def looks_already_live_by_trading(
    current_price: float | None,
    snapshots: list[tuple[float | None, float | None]],
    min_volume_delta: float = LIVE_TRADING_MIN_VOLUME_DELTA,
    min_price_swing: float = LIVE_TRADING_MIN_PRICE_SWING,
) -> bool:
    """`current_price` is this market's own latest snapshot price (checked
    against the same EXTREME_LOW/EXTREME_HIGH as ladder_looks_resolved --
    see the module-level comment above for why this gate is required, not
    optional, once the window is wide enough to cover a slow real swing).
    `snapshots` is this ONE market's own (last_price, volume) pairs from
    every snapshot within the recent lookback window (any order) -- NOT
    just the oldest vs newest point. REAL bug caught building the first cut
    of this (2026-07-19, same investigation): comparing only the single
    snapshot closest to exactly "1 hour ago" against the current one missed
    the real swing on the reported case (Boosarawongse vs Venkataraman) --
    by coincidence the hour-ago snapshot had already partway recovered from
    the price's actual low point, understating the swing (0.05 measured vs
    a real 0.19+ peak-to-trough move within the same window). Using the
    MAX and MIN price/volume across every snapshot in the window is what
    was actually validated against real data (see module comment above),
    not a single before/after pair."""
    if current_price is None or not (current_price <= EXTREME_LOW or current_price >= EXTREME_HIGH):
        return False
    prices = [p for p, v in snapshots if p is not None]
    volumes = [v for p, v in snapshots if v is not None]
    if len(prices) < 2 or len(volumes) < 2:
        return False
    volume_delta = max(volumes) - min(volumes)
    price_swing = max(prices) - min(prices)
    return volume_delta >= min_volume_delta and price_swing >= min_price_swing


# Minimum traded volume before an extreme two-sided price is treated as decided.
# Kalshi tennis volumes on a real ITF match run into the tens of thousands (the
# reported case: 33,541 and 41,714), while an untraded market sits at 0.
PAIR_RESOLVED_MIN_VOLUME = 1_000.0


def pair_looks_resolved(sides: list[tuple[float | None, float | None]]) -> bool:
    """A two-outcome market (moneyline: exactly one side wins) whose BOTH sides
    are quoted, priced at opposite extremes, and have really traded.

    Why this exists even though ladder_looks_resolved and
    looks_already_live_by_trading already do related jobs:

      - ladder_looks_resolved needs 2+ thresholds of the SAME quantity. A
        moneyline has no thresholds, so it never applies.
      - looks_already_live_by_trading needs a recent price SWING. A market that
        has been pinned near 0.01 for hours shows no swing inside the window, so
        it silently passes.

    Real case (user-reported 2026-08-03): Firman vs Vladson, Kalshi ITF women's
    moneyline. Kalshi had it status=finalized, result Vladson, prices 0.01/0.99
    on 33k/41k volume -- while this app still showed a recommended bet, because
    estimated_start_time claimed the match began ~4 hours LATER than it had
    already finished, no winner had been scraped, and the ticker date was today
    so market_cleanup (which keys on the ticker date being in the PAST) could not
    fire until tomorrow.

    Requiring BOTH sides, opposite extremes AND real volume is what keeps this
    off a genuine pre-match heavy favourite: an untraded lopsided market has no
    volume, and a live-but-undecided one does not sit with both sides pinned.
    """
    prices = [p for p, _ in sides if p is not None]
    if len(prices) < 2:
        return False
    lo, hi = min(prices), max(prices)
    if not (lo <= EXTREME_LOW and hi >= EXTREME_HIGH):
        return False
    volumes = [v for _, v in sides if v is not None]
    return bool(volumes) and max(volumes) >= PAIR_RESOLVED_MIN_VOLUME

# Futures market types where exactly ONE entity can win, so a leg trading at
# near-certainty means the whole group is decided. Deliberately explicit rather
# than inferred: "sum of prices ~ 1" looks like a clean test for mutual
# exclusivity and is NOT one here -- the real BLAST Bounty group summed to 2.51
# because the losing legs kept stale quotes (MOUZ 0.995 while OG and Wildcard
# still showed 0.42), so a sum test would have missed the exact case this exists
# for. Multi-winner groups (win_total, playoff qualifiers, "team to make
# postseason") must NOT be listed: a near-certain leg there is perfectly normal.
ONE_WINNER_MARKET_TYPES = {
    "tournament_winner", "league_winner", "division_winner", "conference_champion",
    "super_bowl_champion", "drivers_champion", "constructors_champion",
    "race_winner", "pole", "mvp",
}

# A one-winner group with a leg at or above this is over. Chosen from live data:
# across 304 futures groups only 9 have any leg >= 0.97, and the genuine
# pre-event favourites sit well below -- so this separates decided groups from
# heavy favourites without needing a second signal.
FUTURES_DECIDED_PRICE = 0.97


def futures_group_decided(market_type: str | None, prices) -> bool:
    """True when a one-winner futures group has already been won.

    REAL BUG this fixes (user-reported 2026-08-04): the BLAST Bounty 2026 Season 2
    Finals champion market had MOUZ at 0.995 -- the tournament was over -- yet all
    32 legs still showed, and the app was recommending $5 stakes on Team Falcons
    (0.025), FURIA (0.005) and Aurora (0.005). Kalshi still reported every leg
    `active`, so the platform's own status could not catch it, and no futures
    endpoint applied any resolved-group check at all.
    """
    if market_type not in ONE_WINNER_MARKET_TYPES:
        return False
    real = [p for p in prices if p is not None]
    return bool(real) and max(real) >= FUTURES_DECIDED_PRICE
