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

# SHORT-WINDOW ARM: catches a live match that is still UNDECIDED, which the
# extremity requirement above cannot.
#
# User-reported 2026-08-10: "Sophia Santos vs Sigrist recommended to me now"
# while the match was in play. Santos moved 0.09 -> 0.46 with volume 16,684 ->
# 227,370 in fifty minutes, and the app staked $10 on a +40.3pp "edge" that was
# nothing but the model still holding its PRE-MATCH number (0.8631) against a
# market that had watched her lose a set. Every existing gate missed it: the
# stored start time said 19:00Z, four hours away; Flashscore does not cover that
# ITF women's event; and the arms above returned False at the first line because
# 0.46 is not extreme.
#
# The extremity requirement is NOT removed -- it was added deliberately, against
# a real counter-example (Vukic/Holmgren, a liquid PREGAME market that wandered
# 0.49-0.72 with big swing and volume and must not be flagged). Instead this arm
# adds the thing that actually separates the two: RATE. Vukic/Holmgren drifted
# over many hours; a live match reprices violently inside one.
#
# Calibrated against FLASHSCORE'S OWN live labels as ground truth, over 250
# tennis moneyline markets with 2+ snapshots in a 60-minute window:
#     flashscore LIVE (n=12):  median volume delta 56,868   median swing 0.165
#     not live       (n=238):  median volume delta     19   median swing 0.000
# a ~3,000x gap in medians, so these bars sit in empty space rather than on a
# boundary. Set below the live median, far above anything quiet.
LIVE_TRADING_SHORT_WINDOW_MIN_VOLUME_DELTA = 50_000.0
LIVE_TRADING_SHORT_WINDOW_MIN_PRICE_SWING = 0.10

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


BIG_SWING_PRICE = 0.35
BIG_SWING_MIN_VOLUME = 10_000.0


def looks_already_live_by_trading(
    current_price: float | None,
    snapshots: list[tuple[float | None, float | None]],
    min_volume_delta: float = LIVE_TRADING_MIN_VOLUME_DELTA,
    min_price_swing: float = LIVE_TRADING_MIN_PRICE_SWING,
    short_window_snapshots: list[tuple[float | None, float | None]] | None = None,
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
    # SHORT-WINDOW ARM FIRST -- it is the only one that fires on a live match
    # whose price is still mid-range, and it deliberately does NOT require
    # extremity. See LIVE_TRADING_SHORT_WINDOW_* for the calibration against
    # Flashscore's own live labels and for why rate, not price level, is what
    # separates a live match from a liquid pregame one.
    if short_window_snapshots:
        sp = [p for p, v in short_window_snapshots if p is not None]
        sv = [v for p, v in short_window_snapshots if v is not None]
        if len(sp) >= 2 and len(sv) >= 2:
            if (max(sv) - min(sv) >= LIVE_TRADING_SHORT_WINDOW_MIN_VOLUME_DELTA
                    and max(sp) - min(sp) >= LIVE_TRADING_SHORT_WINDOW_MIN_PRICE_SWING):
                return True

    if current_price is None or not (current_price <= EXTREME_LOW or current_price >= EXTREME_HIGH):
        return False
    prices = [p for p, v in snapshots if p is not None]
    volumes = [v for p, v in snapshots if v is not None]
    if len(prices) < 2 or len(volumes) < 2:
        return False
    volume_delta = max(volumes) - min(volumes)
    price_swing = max(prices) - min(prices)
    if volume_delta >= min_volume_delta and price_swing >= min_price_swing:
        return True
    # SECOND ARM: a huge swing catches a live match EARLIER than the volume bar can.
    #
    # User-reported 2026-08-04: Hyunyee Lee vs Jialan Cai was recommended while
    # already in play, priced 0.02/0.98. The gate above DID eventually fire and
    # drop it -- but only after ~240,000 of volume had accumulated. On a thin ITF
    # women's match that takes hours, and the whole time the match is live and
    # being offered. The detector was lagging, not missing.
    #
    # A pre-match favourite sits quietly at an extreme price; it does not TRAVEL
    # 35 points to get there. That swing, at an already-extreme current price, is
    # the live/decided signature and it appears long before the volume bar is met.
    # The volume floor here only rules out a near-dead market whose handful of
    # stray quotes could otherwise manufacture a swing.
    #
    # Measured over 331 Kalshi tennis moneylines sitting at an extreme price: the
    # existing rule catches 37, and this arm adds just 3 -- all with 40k-81k of
    # real volume already traded, i.e. genuinely active markets that simply had
    # not yet crossed 100k. Loosening to 0.30 adds only 2 more, so the threshold
    # is not perched on a cliff.
    return price_swing >= BIG_SWING_PRICE and volume_delta >= BIG_SWING_MIN_VOLUME


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


# NINTH gap (2026-08-04, user-reported): Stella Hanttu vs Andrea Roots, a Kalshi
# ITF women's moneyline, recommended at $20 with the match nearly over. Priced
# 0.08/0.96 on 64k volume while estimated_start_time still claimed a start three
# hours in the FUTURE.
#
# Every existing check misses it by construction:
#   - pair_looks_resolved wants 0.02/0.98; this pair was 0.08/0.96.
#   - looks_already_live_by_trading returns early unless the CURRENT price is
#     itself past 0.02/0.98, and this one sat at 0.12.
#   - its volume arm needs 100k; only 64k had traded.
#   - _start_time_untrusted compares start against expiry, and Kalshi had both
#     set to the same wrong instant, so they agreed and the start looked sound.
#
# The signal that does separate them is that the price TRAVELLED to the extreme
# instead of starting there. Two live controls, same six-hour window:
#   Hanttu (in play):      0.08/0.96, swing 0.31, volume 2.7k -> 64.8k
#   Sabalenka v Uchijima:  0.06/0.95, swing 0.010, volume 34.4k -> 38.4k
# Sabalenka is a genuine upcoming match with a world-#1 favourite -- lopsided and
# well traded, and it MUST keep showing. The swing is what tells them apart, by a
# factor of 30.
#
# Requiring all three (opposite extremes, real volume, and a real swing) is what
# makes each threshold safe on its own:
#   - the extremes bound rules out a market that merely opened and started
#     trading (Rottoli vs Ferrari: 0.50/0.58, volume 0 -> 13k, plainly pre-match);
#   - the swing bound rules out a standing favourite like Sabalenka;
#   - the volume bound rules out a thin market whose stray quotes fake a swing.
# Measured over every active Kalshi tennis moneyline: fires on 81 matches, and
# the only two whose start is still in the future are this one and a 0.99/0.02
# market -- both genuinely live. Stable at 79/81/91 for swings of 0.25/0.20/0.15,
# so it is not perched on a cliff.
PAIR_LIVE_LOW = 0.12
PAIR_LIVE_HIGH = 0.88
PAIR_LIVE_MIN_VOLUME = 20_000.0
PAIR_LIVE_MIN_SWING = 0.20


def pair_looks_live_by_travel(
    sides: list[tuple[float | None, float | None, float | None]],
) -> bool:
    """Both sides of a moneyline now at opposite extremes, having MOVED there.

    `sides` is one (current_price, volume, price_swing) triple per side, where
    price_swing is that side's max-minus-min price across the recent window.

    A price of exactly 0.0 must be excluded from the caller's swing as unpriced,
    not treated as a real quote -- otherwise a market that simply had no trades
    early in the window shows a full-scale swing it never made.
    """
    prices = [p for p, _, _ in sides if p is not None]
    if len(prices) < 2:
        return False
    if not (min(prices) <= PAIR_LIVE_LOW and max(prices) >= PAIR_LIVE_HIGH):
        return False
    volumes = [v for _, v, _ in sides if v is not None]
    if not volumes or max(volumes) < PAIR_LIVE_MIN_VOLUME:
        return False
    swings = [w for _, _, w in sides if w is not None]
    return bool(swings) and max(swings) >= PAIR_LIVE_MIN_SWING


# TENTH gap (2026-08-04, user-reported): Ovcharenko vs Broadus, a Kalshi ITF
# women's moneyline, recommended at $20 while the match was ON COURT. Recorded
# start 20:00Z, five hours away; the two sides had travelled 0.22->0.65 and
# 0.80->0.35 in forty-five minutes on 107k and 60k of volume, with the lead
# changing hands.
#
# pair_looks_live_by_travel cannot see this and never could: it requires the two
# sides to sit at OPPOSITE EXTREMES, so it only ever catches a match that is
# live AND effectively decided. A live match that is merely CLOSE never
# qualifies. Flashscore had both players, but only in doubles draws, so the
# positive-signal gate had no opinion either.
#
# What gives it away is not where the price is but how the market got there: a
# ten-fold jump in traded volume ON A REAL BASE, together with a large swing.
# The base is what makes the volume test meaningful -- an earlier attempt at a
# surge rule was rejected because it fired on markets that had merely OPENED
# (Rottoli vs Ferrari: 0.50/0.58, volume 0 -> 13k, plainly pre-match), where the
# "growth" was division by nothing. Requiring real CURRENT volume as well as
# growth separates the two cleanly.
#
# Deliberately price-blind, which is the whole point of adding it beside the
# travel rule rather than widening that one.
#
# Measured over every active Kalshi tennis moneyline: fires on 86 matches, of
# which only 6 still claim a future start -- and all 6 are ITF matches carrying
# 50k-280k volume with swings of 0.29-0.78, i.e. every one genuinely in play.
# The controls stay out: Sabalenka vs Uchijima (a real upcoming match with a
# world-#1 favourite) swings 0.010, and Rottoli vs Ferrari holds only 13k.
# A REAL BASE, not a big absolute number, is what makes the growth meaningful.
# The first version required 50k of current volume, which was really a proxy for
# "this market had already been trading" -- and it missed the second reported
# case, Ahn vs Fakih, a thin ITF match in its SECOND SET carrying only 15k. Its
# base was 145 and 753, i.e. genuinely traded before the surge; the market that
# had merely OPENED (Rottoli vs Ferrari) started from 0. Requiring the base
# directly says what was meant, and lets the absolute floor drop to 5k so a thin
# live match is caught. Measured with the base rule: 82 matches fire and only 3
# still claim a future start -- Ahn/Fakih, Victoria Gobbi/Vig and Rain/Pereira,
# all ITF, all with swings of 0.27-0.78 on 15k-110k, i.e. all genuinely in play.
PAIR_SURGE_MIN_SWING = 0.25
PAIR_SURGE_MIN_BASE = 50.0
PAIR_SURGE_MIN_VOLUME = 5_000.0
PAIR_SURGE_GROWTH = 3.0


def pair_looks_live_by_surge(
    sides: list[tuple[float | None, float | None, float | None]],
) -> bool:
    """The FIXTURE has really traded up while its price moved a long way.

    `sides` is one (price_swing, current_volume, base_volume) triple per side,
    measured across the recent window. Volume is SUMMED across both sides and
    the swing is the largest on either.

    Judged per fixture, not per side, and that is the correction rather than a
    detail. The first version asked one side to satisfy every condition alone,
    and a third reported live match (Dang vs Hui, ITF women, recommended at its
    "5pm" start while into the first set) slipped through because each side
    failed a DIFFERENT one: Dang's swing cleared at 0.250 but its base was 36
    against a floor of 50, while Hui had base 232, 23k traded and 99x growth but
    swung 0.240, ten-thousandths under the bar. Meanwhile the match itself went
    from 268 to 48,415 traded in under half an hour.

    Both sides of a moneyline are the same market being traded, so the evidence
    is naturally joint: which side carries the volume, and which side's price
    happens to move furthest, is arbitrary. Summing removes that arbitrariness
    instead of chasing it with per-side thresholds.

    The controls still hold at these levels: Sabalenka vs Uchijima (a genuine
    upcoming match) swings 0.010, and Rottoli vs Ferrari (a market that had
    merely opened) has a combined base of 0.
    """
    swings = [s for s, _, _ in sides if s is not None]
    currents = [c for _, c, _ in sides if c is not None]
    bases = [b for _, _, b in sides if b is not None]
    if not swings or not currents or not bases:
        return False
    if max(swings) < PAIR_SURGE_MIN_SWING:
        return False
    current, base = sum(currents), sum(bases)
    if current < PAIR_SURGE_MIN_VOLUME:
        return False
    # The base gate is the one doing the real work: it demands the market was
    # ALREADY trading before the surge. A market opening from nothing has a base
    # of 0 and is not a live match, however fast its volume then climbs.
    if base < PAIR_SURGE_MIN_BASE:
        return False
    return current >= PAIR_SURGE_GROWTH * base

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

# How much a DECIDED field's dead legs may still be quoted at, per leg. A
# finished market does not go to zero -- BLAST Bounty's 31 dead legs still
# carried ~0.05 each, summing the field to 2.51.
FUTURES_DEAD_LEG_ALLOWANCE = 0.05


def futures_group_decided(market_type: str | None, prices) -> bool:
    """True when a one-winner futures group has already been won.

    REAL BUG this fixes (user-reported 2026-08-04): the BLAST Bounty 2026 Season 2
    Finals champion market had MOUZ at 0.995 -- the tournament was over -- yet all
    32 legs still showed, and the app was recommending $5 stakes on Team Falcons
    (0.025), FURIA (0.005) and Aurora (0.005). Kalshi still reported every leg
    `active`, so the platform's own status could not catch it, and no futures
    endpoint applied any resolved-group check at all.

    Two guards added after the first version, because `market_type` alone does
    not mean what it says: Kalshi files "Who Will Qualify For Champs 2026?",
    "Team to Qualify for Worlds 2026?" and "LPL Summer 2026: Player to Penta"
    all as `tournament_winner`, and MANY of those legs win. Treating them as
    one-winner marked live groups as finished -- 166 legs across three groups,
    every one of them a real bet the page was throwing away.

      1. Exactly ONE leg may be at the decided price. A single-winner field
         cannot have two outcomes at 97%+; "Qualify For Champs" had two at 0.98
         and "Player to Penta" two more.
      2. The field's prices must SUM to about one. Mutually exclusive outcomes
         do; a multi-qualifier field does not ("Team to Qualify for Worlds"
         sums to 21.02 across 56 legs). The allowance is 1 + 0.05 per leg,
         because a genuinely decided field keeps stale non-zero prices on its
         dead legs -- BLAST Bounty, the real reported case, sums to 2.51 across
         32 legs against a 2.60 bound and still passes.
    """
    if market_type not in ONE_WINNER_MARKET_TYPES:
        return False
    real = [p for p in prices if p is not None]
    if not real:
        return False
    if sum(1 for p in real if p >= FUTURES_DECIDED_PRICE) != 1:
        return False
    return sum(real) <= 1.0 + FUTURES_DEAD_LEG_ALLOWANCE * len(real)
