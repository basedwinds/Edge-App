"""UFC markets API -- parallel to routers/nba_markets.py.

Moneyline has a real baseline (elo_mma.py, K=72, grid-searched against the
full ufcstats.com historical scrape -- see that module's docstring for the
honest 56.2% accuracy this pure win/loss Elo achieves, well below the
~65-66% a real sportsbook-odds favorite gets, since this has zero market/
style/physical-attribute information).

Distance (goes-the-distance) ALSO has a real baseline now
(distance_service_mma.py) -- this app's flagship differentiator market per
a separate, standalone research project's earlier finding, re-tested fresh
here with this app's own point-in-time features and its own walk-forward
harness: **+7.08pp accuracy over a naive baseline, 95% CI [+5.54pp,
+8.67pp] (excludes zero), won 17/17 yearly folds** -- see
scripts/backtest_mma_distance.py's docstring for the full validation.

Method of finish (KO/TKO vs Submission vs Decision) ALSO has a real
baseline now (method_service_mma.py), extending the distance work with
method-specific finish/loss rates PLUS weight class (added 2026-07-18,
heavier divisions finish more often -- a real, separately-validated
improvement, 0.6048 -> 0.6001 Brier): **Brier beats a naive base-rate
baseline in 17/17 yearly walk-forward folds** -- see
scripts/backtest_mma_method.py's docstring. The market's "draw" outcome
stays unmodeled (rare, and the validated feature set doesn't split it out
-- see mma_features.py).

Rounds ("ends before round N?" / O/U {N}.5) has a real, but weaker/noisier
baseline now too (rounds_service_mma.py): the raw 5-way round-of-finish
target only beats a naive baseline in 13/17 yearly Brier folds (vs. 17/17
for distance/method-of-finish), but the market's own summed ladder
question is more robust (10-15/17 depending on rung, always net-positive)
-- see scripts/backtest_mma_rounds.py's docstring. Surfaced with an
explicit "treat with more caution" caveat everywhere it's shown.

All four still model_validated: false (an internal accuracy edge is not
the same claim as "beats the market" -- no historical UFC odds archive
exists to check that here). Method_of_victory/round_of_victory still ship
model_prob=None -- their own models aren't built/validated yet. Same
honest "no baseline, not a guessed number" pattern this app already uses
for NFL preseason -- see NO_BASELINE_REASON below.

Reuses `_batch_latest_snapshots`/`_implied_prob` from routers/markets.py
directly, same as nba_markets.py/mlb_markets.py.
"""
import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routers.markets import _batch_latest_snapshots, _edge_sentence, _implied_prob, _seeded_choice
from app.api.routers.settings import get_mma_pool_dollars, get_staking_params, get_flat_params, get_unit_dollars
from app.api.schemas import FuturesMarketOut, MmaMarketOut, ReasoningFactorOut, ReasoningOut
from app.db.database import get_session
from app.db.models import Market, MmaFight
from app.ingestion.market_matcher_mma import resolve_fight_side
from app.models import distance_service_mma, method_service_mma, rounds_service_mma
from app.models import mma_model_disagreement
from app.models.baseline import elo_service_mma
from app.models.ladder_sanity import find_resolved_entities
from app.models.staking import has_real_trading, kelly_fraction, suggested_stake_dollars, size_stake_dollars
from app.models.clv_selection import bucket_clv_stats, gate_kelly

_NO_BASELINE_METHODOLOGY = "No detailed methodology available for this market type yet -- see the module docstring above."

router = APIRouter(prefix="/mma", tags=["mma"])

GAME_MARKET_TYPES = {
    "moneyline", "distance", "method_of_victory", "method_of_finish", "rounds", "round_of_victory",
}

NO_BASELINE_REASON = (
    "No baseline yet -- this market's model is still being built and validated against this app's "
    "own historical data, not shipped as a guessed number."
)

ROUNDS_OUT_OF_SCOPE_REASON = (
    "This rung asks whether the fight even reaches round 1 -- every completed fight trivially "
    "satisfies that, so the round-of-finish model (trained only on fights that actually happened) has "
    "no real information about it. The market's price here reflects withdrawal/no-show risk, not "
    "in-fight skill -- genuinely out of scope for this model, not a guessed number."
)


def _distance_model_prob(fight: MmaFight | None) -> float | None:
    if fight is None:
        return None
    return distance_service_mma.predict_went_distance(
        fight.fighter_a_id, fight.fighter_b_id, fight.weight_class,
        fight.scheduled_rounds, bool(fight.is_title_bout),
    )


def _rounds_model_prob(fight: MmaFight | None, side: str | None, line: float | None) -> float | None:
    """side/line semantics differ by platform (see market_catalog_mma.py):
    Kalshi's "ends before round N?" stores side="under", line=N (integer)
    -- wants P(round_of_finish < N). Polymarket's O/U {N}.5 ladder stores
    side="over", line=N.5 (half-integer) -- wants P(round_of_finish >
    N.5). Both route through the same underlying round distribution."""
    if fight is None or line is None:
        return None
    if side == "under":
        return rounds_service_mma.predict_ends_before_round(fight.fighter_a_id, fight.fighter_b_id, fight.scheduled_rounds, line)
    if side == "over":
        return rounds_service_mma.predict_over_rounds(fight.fighter_a_id, fight.fighter_b_id, fight.scheduled_rounds, line)
    return None


def _method_model_prob(fight: MmaFight | None, side: str | None) -> float | None:
    """side is "kotko"/"submission"/"decision"/"draw"/None (see
    market_catalog_mma.py's upsert_kalshi_mma_mof_market/
    upsert_polymarket_mma_method_row). "draw" and None stay unmodeled --
    the validated feature set doesn't split draws out (rare, see
    mma_features.py), never guess a number for it."""
    if fight is None or side not in ("kotko", "submission", "decision"):
        return None
    probs = method_service_mma.predict_method(fight.fighter_a_id, fight.fighter_b_id, fight.weight_class, fight.scheduled_rounds)
    return probs.get(side) if probs is not None else None


def _moneyline_model_prob(market: Market, fight: MmaFight | None) -> float | None:
    """REAL BUG fixed here (caught via live testing, not assumed): an exact
    string match dropped real rows -- e.g. Polymarket's "Jose Miguel
    Delgado" vs ufcstats' canonical "Jose Delgado" for the same fighter
    (same middle-name-inclusion mismatch already handled for FIGHT matching
    in market_matcher_mma.py, just not reused here originally). Uses the
    same token-subset fuzzy matcher for consistency.

    Now delegates to resolve_fight_side so that side-resolution here is the SAME
    comparison the fight matcher used. It was strictly weaker before, and that
    gap was visible in the payload: markets on the UFC 329/330 cards linked to
    the right fight and still came back model=None on whichever fighter the
    platform spelled differently ("Yadier Delvalle", "Giovanna Canuto"), so half
    of each fight priced and half didn't."""
    if fight is None or market.team is None:
        return None
    side = resolve_fight_side(market.team, fight.fighter_a_name, fight.fighter_b_name)
    if side == "a":
        fighter_id, opponent_id = fight.fighter_a_id, fight.fighter_b_id
    elif side == "b":
        fighter_id, opponent_id = fight.fighter_b_id, fight.fighter_a_id
    else:
        return None  # name doesn't pick out exactly one side -- don't guess
    p = elo_service_mma.get_fight_win_prob(fighter_id, opponent_id)
    return round(p, 4) if p is not None else None


@router.get("/markets", response_model=list[MmaMarketOut])
def list_mma_markets(session: Session = Depends(get_session)):
    markets = session.query(Market).filter(Market.sport == "mma", Market.market_type.in_(GAME_MARKET_TYPES)).all()
    fight_ids = {m.mma_fight_id for m in markets if m.mma_fight_id}
    fights_by_id = {f.id: f for f in session.query(MmaFight).filter(MmaFight.id.in_(fight_ids)).all()} if fight_ids else {}

    # Same "don't keep predicting an already-decided event" fix this app
    # already applies to NFL/NBA/MLB (see mlb_markets.py Phase 5 note) --
    # here it also matters for a market whose fight has actually happened
    # (winner_id set), not just a frozen stale price.
    def _fight_already_decided(m: Market) -> bool:
        fight = fights_by_id.get(m.mma_fight_id) if m.mma_fight_id else None
        return fight is not None and fight.winner_id is not None

    # SECOND, related gap (audited 2026-07-19 alongside NFL/NBA/MLB's own
    # version of this fix): winner_id only gets set once ufcstats posts a
    # real result, which can lag a fight's actual conclusion -- and unlike
    # a multi-hour game, an MMA fight can end in seconds (an early
    # knockout), so the window where a market is genuinely live/in-progress
    # (or already over) but not yet decided per our data is real even though
    # short. Excluded once the fight's own real estimated start instant
    # (MmaFight.estimated_start_time, Kalshi's per-fight occurrence_datetime
    # estimate -- already a full ISO UTC instant, no timezone table needed)
    # is confirmed in the past.
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def _fight_already_started(m: Market) -> bool:
        fight = fights_by_id.get(m.mma_fight_id) if m.mma_fight_id else None
        if fight is None or not fight.estimated_start_time:
            return False
        try:
            start = datetime.datetime.fromisoformat(fight.estimated_start_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        return now_utc >= start

    # THIRD gap, and the one that actually explains a real user-reported bug
    # (2026-07-19): confirmed live that Kalshi's own `occurrence_datetime`
    # (what `_fight_already_started` relies on) is a PRE-fight ESTIMATE that
    # does NOT get corrected once the fight actually happens -- a real
    # settled fight (Kalshi's own `result`/`status` already "finalized",
    # last_price at the $0.01 floor, real $1.79M+ volume) still showed a
    # FUTURE `estimated_start_time`, so the check above alone let it through.
    # Kalshi's `status` field is the authoritative signal here (it only ever
    # becomes not-"active" once truly resolved); Polymarket's equivalent is
    # its per-market `closed`/`active` pair (see polymarket_mma_client.py::
    # _market_status -- same fix already applied to Tennis). Both upsert
    # paths now store this instead of Polymarket's old hardcoded "active".
    #
    # FOURTH gap (2026-07-19, a real fight the user personally watched end
    # live still showed a 0.1% price hours later): status only updates while
    # the poller can still SEE the market -- but our poller only fetches
    # each Kalshi/Polymarket series' currently-OPEN listing, so a market that
    # closes simply drops out of every future poll and its last-known
    # status/price sit frozen in the DB forever, exactly like the orphaned
    # rows found and manually cleaned up earlier. Confirmed live: Kalshi's
    # OWN real-time status for that exact fight was "finalized" (closed
    # 1h15m before this was written), but our stored `status` was still the
    # "active" value from the last successful poll before it closed.
    # `Market.updated_at` can't fix this (SQLAlchemy's `onupdate` only fires
    # on an actual value change, so a market whose price is UNCHANGED poll to
    # poll never bumps it -- already learned the hard way earlier this
    # session, see project memory); `MarketSnapshot.ts` is the real fix,
    # since a fresh row gets INSERTED every poll cycle regardless of whether
    # the price moved. The poller runs every 5 minutes -- a market with no
    # snapshot in the last 20 (4 missed cycles' worth of slack) essentially
    # certainly isn't in the platform's live listing anymore.
    all_snapshots = _batch_latest_snapshots(session, [m.id for m in markets])
    now_for_staleness = datetime.datetime.now(datetime.timezone.utc)
    # MEASURED AGAINST THE FEED, NOT THE WALL CLOCK -- same fix as
    # tennis_markets.py, applied here because this sport was measured to have the
    # same defect. Over 6 hours of real snapshot history the poll gap for this
    # sport reached 28 minutes against a 20-minute threshold, so every
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

    # FIFTH gap (2026-07-19, found while investigating the Tennis version of
    # this same class of bug -- see ladder_sanity.py): a fight can be
    # genuinely IN PROGRESS -- not decided, not stale, status still "active"
    # -- while `estimated_start_time` is simply wrong for that specific
    # fight, same root cause as the THIRD gap above but here nothing has
    # closed yet. Detected structurally instead: `rounds` (the one real
    # ladder market MMA has) pricing two DIFFERENT round-count thresholds
    # at the same extreme value simultaneously (e.g. Over 1.5 AND Over 2.5
    # both near 100%) is something a real, still-undecided pregame market
    # never does -- it only happens once the fight has progressed far
    # enough that both thresholds are already a foregone conclusion.
    ladder_groups: dict[tuple, list[tuple[float, float]]] = {}
    for m in markets:
        if m.line is None or m.mma_fight_id is None or m.market_type != "rounds":
            continue
        snap = all_snapshots.get(m.id)
        implied = _implied_prob(snap)
        if implied is None:
            continue
        ladder_groups.setdefault((m.mma_fight_id, m.market_type), []).append((m.line, implied))
    resolved_group_keys = find_resolved_entities(ladder_groups)
    fights_with_resolved_ladder = {key[0] for key in resolved_group_keys}

    def _fight_ladder_resolved(m: Market) -> bool:
        return m.mma_fight_id in fights_with_resolved_ladder

    markets = [
        m for m in markets
        if not _fight_already_decided(m)
        and not _fight_already_started(m)
        and not _fight_ladder_resolved(m)
        and (m.status or "active") == "active"
        and not _market_stale(m)
    ]
    # Hoisted: as an inline set literal this was rebuilt once per
    # all_snapshots entry -- quadratic, and the dominant cost of the
    # tennis endpoint at 34k markets (183M attribute reads, ~40s).
    _kept_market_ids = {m.id for m in markets}
    snapshots_by_market = {mid: s for mid, s in all_snapshots.items() if mid in _kept_market_ids}
    weekly_pool, futures_pool = get_mma_pool_dollars(session)
    unit_dollars = get_unit_dollars(session)
    fractional_kelly, max_stake_fraction, min_edge_to_bet = get_staking_params(session)
    staking_mode, flat_marginal, flat_full = get_flat_params(session)
    clv_stats = bucket_clv_stats(session)

    out = []
    for m in markets:
        fight = fights_by_id.get(m.mma_fight_id) if m.mma_fight_id else None
        snap = snapshots_by_market.get(m.id)
        implied = _implied_prob(snap)
        if m.market_type == "moneyline":
            model_prob = _moneyline_model_prob(m, fight)
        elif m.market_type == "distance":
            model_prob = _distance_model_prob(fight)  # market.side is always "yes" (see market_catalog_mma.py) -- no complement needed
        elif m.market_type == "method_of_finish":
            model_prob = _method_model_prob(fight, m.side)
        elif m.market_type == "rounds":
            model_prob = _rounds_model_prob(fight, m.side, m.line)
        else:
            model_prob = None
        # ADVISORY FLAG on moneylines: a fuller model that also reads style and
        # DEFENCE (takedown defence, strikes absorbed, control time) is measurably
        # more accurate on past fights (-1.63% log loss) but has never been shown
        # to beat the market, so it prices nothing. Where it disagrees sharply
        # with the shipped Elo price, that is worth knowing before betting --
        # same posture as flagging an MLB game with no announced pitcher: the bet
        # stands, you just know more about its uncertainty. Fails soft to None.
        disagreement_note = None
        if m.market_type == "moneyline" and fight is not None:
            disagreement_note = mma_model_disagreement.note_for(
                fight.fighter_a_id, fight.fighter_b_id)

        if model_prob is not None:
            no_baseline_reason = None
        elif m.market_type == "rounds" and m.line is not None and m.line <= 1.0:
            no_baseline_reason = ROUNDS_OUT_OF_SCOPE_REASON
        else:
            no_baseline_reason = NO_BASELINE_REASON
        edge = round(model_prob - implied, 4) if (model_prob is not None and implied is not None) else None
        has_traded = has_real_trading(m.source, snap.volume if snap else None, snap.last_price if snap else None)
        kelly = gate_kelly(kelly_fraction(model_prob, implied, fractional_kelly, max_stake_fraction, min_edge_to_bet, has_traded, snap.yes_ask if snap else None), clv_stats, "mma", m.market_type)
        stake_dollars = size_stake_dollars(staking_mode, kelly, weekly_pool, model_prob, implied, unit_dollars, flat_marginal, flat_full)  # every GAME_MARKET_TYPES entry is per-fight, "weekly" pool
        out.append(
            MmaMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                side=m.side,
                line=m.line,
                fight_label=f"{fight.fighter_a_name} vs {fight.fighter_b_name}" if fight else None,
                event_name=fight.event_name if fight else None,
                mma_fight_id=m.mma_fight_id,
                event_date=fight.event_date if fight else None,
                estimated_start_time=fight.estimated_start_time if fight else None,
                weight_class=fight.weight_class if fight else None,
                is_title_bout=bool(fight.is_title_bout) if fight else False,
                scheduled_rounds=fight.scheduled_rounds if fight else None,
                model_note=disagreement_note,
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
    out.sort(key=lambda m: (m.event_date or "9999", m.fight_label or "", m.market_type))
    return out


def _moneyline_insight_mma(
    fighter_a: str, fighter_b: str, a_rating: float | None, a_age: float | None,
    b_rating: float | None, b_age: float | None, model_prob: float | None, market_prob: float | None,
) -> str:
    seed = f"{fighter_a}|{fighter_b}|{a_rating}|{b_rating}"
    has_age = a_age is not None and b_age is not None and abs(a_age - b_age) >= 3

    def age_clause(connector: bool) -> str:
        if not has_age:
            return ""
        younger, older = (fighter_a, fighter_b) if a_age < b_age else (fighter_b, fighter_a)
        yng, old = min(a_age, b_age), max(a_age, b_age)
        return _seeded_choice(seed + "a", [
            f"There's a real age gap in play too: {younger} is the younger fighter at {yng:.0f} to {older}'s {old:.0f}, and history shows Elo under-credits youth on its own -- so the age adjustment below leans {younger}'s way.",
            f"Age is the other thread here -- {younger} ({yng:.0f}) has a meaningful edge on {older} ({old:.0f}), something raw Elo systematically underrates, which is why the age adjustment nudges toward {younger}.",
            f"Worth noting the {abs(a_age - b_age):.0f}-year age split -- {younger} at {yng:.0f} to {older}'s {old:.0f} -- since Elo alone tends to miss it; the age adjustment picks that up for {younger}.",
        ]) if connector else ""

    story = ""
    if a_rating is not None and b_rating is not None:
        gap = a_rating - b_rating
        if abs(gap) < 30:
            story = _seeded_choice(seed, [
                f"By win/loss history alone this is a genuine toss-up -- Elo has {fighter_a} and {fighter_b} rated almost dead even ({a_rating:.0f} to {b_rating:.0f}).",
                f"On the records, there's next to nothing between these two: Elo puts {fighter_a} and {fighter_b} nearly level ({a_rating:.0f} to {b_rating:.0f}).",
                f"Purely by results, this one's a coin flip -- {fighter_a} and {fighter_b} sit almost even on Elo ({a_rating:.0f} to {b_rating:.0f}).",
            ])
        else:
            stronger, s_r, weaker, w_r = (fighter_a, a_rating, fighter_b, b_rating) if gap > 0 else (fighter_b, b_rating, fighter_a, a_rating)
            story = _seeded_choice(seed, [
                f"On pure win/loss Elo, {stronger} is the clearly stronger fighter here ({s_r:.0f} to {w_r:.0f} for {weaker}).",
                f"The records favor {stronger} plainly -- Elo has them well ahead of {weaker} ({s_r:.0f} to {w_r:.0f}).",
                f"{stronger} carries the stronger résumé by Elo, comfortably above {weaker} ({s_r:.0f} to {w_r:.0f}).",
            ])
        if has_age:
            story += " " + age_clause(True)
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _distance_insight_mma(fighter_a: str, fighter_b: str, scheduled_rounds: int | None, is_title_bout: bool, model_prob: float | None, market_prob: float | None) -> str:
    seed = f"{fighter_a}|{fighter_b}|dist"
    rounds_note = " (a title fight, so 5 rounds)" if is_title_bout else f" ({scheduled_rounds} rounds)" if scheduled_rounds else ""
    story = _seeded_choice(seed, [
        f"This is one of the models with a real, checked edge. It reads whether the fight sees the final bell off each fighter's record, form, finish rate, striking and takedown volume, layoff, age and reach, plus this bout's scheduled length{rounds_note} and weight class -- and it's beaten a naive baseline by 7.08pp with the confidence interval clear of zero, winning all 17 yearly walk-forward folds.",
        f"Whether this one goes the distance comes from a logistic model built on both fighters' records, form, finish rates, striking and takedown volume, layoff, age and reach, together with the scheduled rounds{rounds_note} and the division. It's a genuine signal -- +7.08pp over a naive baseline, 95% CI excluding zero, and a clean 17-for-17 across the yearly walk-forward tests.",
        f"The go-the-distance read here is one this app actually trusts: it weighs each fighter's history, form, finishing and volume numbers, layoff, age and reach against the scheduled length{rounds_note} and weight class, and it's held up in testing -- 7.08pp of real accuracy over baseline and 17/17 walk-forward folds.",
    ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _method_insight_mma(fighter_a: str, fighter_b: str, side: str | None, model_prob: float | None, market_prob: float | None) -> str:
    side_label = {"kotko": "KO/TKO", "submission": "submission", "decision": "decision"}.get(side, side or "this outcome")
    seed = f"{fighter_a}|{fighter_b}|{side}|meth"
    story = _seeded_choice(seed, [
        f"For how it ends -- {side_label} here -- the model leans on each fighter's own KO/TKO and submission rates on both sides of the ledger (their finishing tendency plus a durability/chin proxy) and the weight class, since heavier divisions finish more often. It's a real signal, beating a base-rate baseline on Brier in all 17 yearly walk-forward folds.",
        f"The method question ({side_label}) comes from a multinomial fit on both fighters' KO/TKO and submission win-and-loss rates -- offense and chin -- plus the division, where the bigger weights finish more. That's a checked edge: it out-Briers a naive base rate 17-for-17 across the walk-forward years.",
        f"Scoring this specific finish ({side_label}) draws on each fighter's finishing and getting-finished rates, read as offensive tendency and durability, weighted by a division where heavier means more stoppages. It's held up in testing -- Brier ahead of a base-rate baseline in every one of 17 yearly folds.",
    ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _method_of_victory_insight_mma(fighter: str | None, side: str | None, model_prob: float | None, market_prob: float | None) -> str:
    """method_of_victory = a specific fighter winning by a specific method
    (the 7-way winner x method market). Deliberately UNMODELED: the app has a
    validated winner model (Elo) and a validated fight-level method model, but
    multiplying them assumes how the fight ends is independent of who wins it,
    which has never been backtested -- so no number is invented (project rule:
    ship only validated signals, flag the rest). This just explains that."""
    method_label = {"kotko": "KO/TKO", "submission": "submission", "decision": "decision"}.get(side, None)
    if side == "draw" or fighter is None:
        story = _seeded_choice(f"{side}|mov_draw", [
            "This is the draw / no-contest outcome. The validated method model doesn't split draws out -- they're rare enough that forcing a number would be guessing -- so it's shown for reference, without a model price.",
            "This one covers a draw or no contest, which the app's validated models leave unpriced on purpose (too rare to model honestly), so there's no model number here to lean on.",
        ])
    else:
        pick = f"{fighter} by {method_label}" if method_label else f"{fighter} by this method"
        story = _seeded_choice(f"{fighter}|{side}|mov", [
            f"This pairs a winner with a method -- {pick} -- so it's really two questions at once: who wins, and how it ends. The app has a validated read on each piece on its own (fighter Elo for the winner, the KO/submission model for the method), but it doesn't multiply them into a single winner-by-method price -- that would assume the finish is independent of who wins, which hasn't been backtested. So this is shown for reference; lean on the moneyline and method-of-finish markets for the model's actual edge.",
            f"Winning AND the method together ({pick}) is a joint question the app leaves unpriced on purpose. Who-wins (Elo) and how-it-ends (the method model) are each validated separately, but combining them assumes independence between the two that's never been checked, so no number is invented here -- the moneyline and method-of-finish markets are where the validated edges live.",
            f"This asks for both the winner and the finish at once -- {pick}. Both halves are modeled and validated on their own, but the app won't stitch them into one winner-by-method price without evidence that the method is independent of the winner, so treat this as reference only and use the moneyline / method-of-finish reads for a real edge.",
        ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


def _rounds_insight_mma(fighter_a: str, fighter_b: str, side: str | None, line: float | None, model_prob: float | None, market_prob: float | None) -> str:
    question = f"ends before round {line:.0f}" if side == "under" and line is not None else f"lasts beyond round {line}" if line is not None else "this rung"
    seed = f"{fighter_a}|{fighter_b}|{side}|{line}|rounds"
    story = _seeded_choice(seed, [
        f"Whether the fight {question} comes from a round-of-finish model that extends the go-the-distance and method work into a full per-round distribution. It's real but noisier than the app's other MMA models -- 13 of 17 walk-forward folds on the raw round target versus 17/17 for distance and method, with the ladder question itself landing 10-15 of 17 depending on the rung -- so lean on it a bit more cautiously than the others.",
        f"This rung ({question}) is scored off a per-round-finish model, the same finishing logic stretched across every round rather than just yes/no on the distance. Treat it more gently than the other markets here: it's net positive but noisier, winning 13/17 folds on the raw target (vs 17/17 for distance/method) and 10-15/17 on the summed ladder.",
        f"The read on whether it {question} extends the distance and method models into a round-by-round distribution. It's a genuine signal but the shakiest of the MMA set -- 13 of 17 walk-forward folds raw, 10-15 of 17 on the ladder itself, always net Brier-positive -- worth a little extra caution.",
    ])
    return f"{story} {_edge_sentence(model_prob, market_prob)}".strip()


@router.get("/markets/{market_id}/reasoning", response_model=ReasoningOut)
def get_mma_market_reasoning(
    market_id: int,
    model_prob: float | None = None,
    market_prob: float | None = None,
    session: Session = Depends(get_session),
):
    """MMA equivalent of routers/markets.py::get_market_reasoning -- same
    "explain how the number passed in was derived, don't recompute it"
    contract."""
    m = session.get(Market, market_id)
    if m is None or m.sport != "mma":
        raise HTTPException(404, "market not found")
    fight = session.get(MmaFight, m.mma_fight_id) if m.mma_fight_id else None
    label = f"{fight.fighter_a_name} vs {fight.fighter_b_name}" if fight else (m.group_label or m.market_type)
    edge = round(model_prob - market_prob, 4) if (model_prob is not None and market_prob is not None) else None

    factors: list[ReasoningFactorOut] = []
    caveats = [
        "model_validated: false -- no model in this app has been shown to beat the market in backtesting "
        "(no free historical UFC odds archive exists to even run that check against for MMA -- see Backtests)."
    ]
    methodology = _NO_BASELINE_METHODOLOGY
    insight = ""

    if m.market_type == "moneyline" and fight is not None:
        methodology = (
            "Fighter-level Elo (K=72, grid-searched against walk-forward Brier on 8,780 historical UFC "
            "fights), plus a real, validated age adjustment (checked to actually improve walk-forward "
            "Brier before shipping, not just correlate in isolation -- see elo_mma.py's docstring). No "
            "market/style/physical-attribute information beyond age."
        )
        a_rating = elo_service_mma.get_fighter_rating(fight.fighter_a_id)
        b_rating = elo_service_mma.get_fighter_rating(fight.fighter_b_id)
        a_age = elo_service_mma.get_current_age(fight.fighter_a_id)
        b_age = elo_service_mma.get_current_age(fight.fighter_b_id)
        if a_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{fight.fighter_a_name} Elo rating", detail=f"{a_rating:.0f}"))
        if b_rating is not None:
            factors.append(ReasoningFactorOut(label=f"{fight.fighter_b_name} Elo rating", detail=f"{b_rating:.0f}"))
        if a_age is not None:
            factors.append(ReasoningFactorOut(label=f"{fight.fighter_a_name} age", detail=f"{a_age:.1f} years"))
        if b_age is not None:
            factors.append(ReasoningFactorOut(label=f"{fight.fighter_b_name} age", detail=f"{b_age:.1f} years"))
        insight = _moneyline_insight_mma(fight.fighter_a_name, fight.fighter_b_name, a_rating, a_age, b_rating, b_age, model_prob, market_prob)

    elif m.market_type == "distance" and fight is not None:
        methodology = (
            "Logistic regression on point-in-time fighter features (real signal, +7.08pp accuracy over "
            "a naive base-rate baseline, 95% CI excludes zero, won 17/17 yearly walk-forward folds -- "
            "see scripts/backtest_mma_distance.py)."
        )
        factors.append(ReasoningFactorOut(label="Weight class", detail=fight.weight_class or "unknown"))
        factors.append(ReasoningFactorOut(label="Scheduled rounds", detail=str(fight.scheduled_rounds) if fight.scheduled_rounds else "not yet known"))
        factors.append(ReasoningFactorOut(label="Title bout", detail="yes" if fight.is_title_bout else "no"))
        insight = _distance_insight_mma(fight.fighter_a_name, fight.fighter_b_name, fight.scheduled_rounds, bool(fight.is_title_bout), model_prob, market_prob)

    elif m.market_type == "method_of_finish" and fight is not None:
        methodology = (
            "Multinomial logistic regression on point-in-time KO/TKO and submission win/loss rates plus "
            "weight class (real signal, Brier beats a naive base-rate baseline in 17/17 yearly walk-forward "
            "folds -- see scripts/backtest_mma_method.py). The market's 'draw' outcome stays unmodeled."
        )
        factors.append(ReasoningFactorOut(label="Weight class", detail=fight.weight_class or "unknown"))
        factors.append(ReasoningFactorOut(label="Outcome", detail={"kotko": "KO/TKO", "submission": "Submission", "decision": "Decision"}.get(m.side, m.side or "unknown")))
        factors.append(ReasoningFactorOut(label="Scheduled rounds", detail=str(fight.scheduled_rounds) if fight.scheduled_rounds else "not yet known"))
        insight = _method_insight_mma(fight.fighter_a_name, fight.fighter_b_name, m.side, model_prob, market_prob)

    elif m.market_type == "method_of_victory":
        methodology = (
            "Winner x method (a specific fighter winning a specific way) -- deliberately UNMODELED. The "
            "winner (fighter Elo) and the fight-level method (KO/submission model) are each validated on "
            "their own, but combining them into a single winner-by-method price assumes the finish is "
            "independent of who wins, which has never been backtested -- so no number is invented (see "
            "the method-of-finish and moneyline markets for the validated reads)."
        )
        if fight is not None:
            factors.append(ReasoningFactorOut(label="Weight class", detail=fight.weight_class or "unknown"))
        if m.side == "draw":
            outcome_detail = "Draw / No contest"
        else:
            method_word = {"kotko": "KO/TKO", "submission": "Submission", "decision": "Decision"}.get(m.side, m.side or "method")
            outcome_detail = f"{m.team or 'a fighter'} by {method_word}"
        factors.append(ReasoningFactorOut(label="Outcome", detail=outcome_detail))
        insight = _method_of_victory_insight_mma(m.team, m.side, model_prob, market_prob)

    elif m.market_type == "rounds" and fight is not None:
        methodology = (
            "Multinomial round-of-finish regression, extended from the distance/method-of-finish feature "
            "sets to predict a full per-round distribution. Real but noisier than this app's other MMA "
            "signals (13/17 yearly Brier folds on the raw round target; the market's own 'ends before "
            "round N' ladder question wins 10-15/17 folds depending on rung -- see "
            "scripts/backtest_mma_rounds.py). Treat with more caution than moneyline/distance/method-of-finish."
        )
        factors.append(ReasoningFactorOut(label="Line", detail=f"{m.line}" if m.line is not None else "unknown"))
        factors.append(ReasoningFactorOut(label="Scheduled rounds", detail=str(fight.scheduled_rounds) if fight.scheduled_rounds else "not yet known"))
        insight = _rounds_insight_mma(fight.fighter_a_name, fight.fighter_b_name, m.side, m.line, model_prob, market_prob)

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


TITLE_NO_BASELINE_REASON = (
    "No baseline yet -- pricing 'who holds the belt on Dec 31' needs the current champion, "
    "the title fights scheduled before then, and retention chained forward. This app has "
    "fight-level win probability (elo_service_mma), which is the INPUT to that model, not "
    "the answer. Shown unpriced rather than as a guessed number."
)


@router.get("/futures", response_model=list[FuturesMarketOut])
def list_mma_futures(session: Session = Depends(get_session)):
    """UFC weight-class title futures -- "Who will be the {Weight} Title Holder
    on Dec 31?", one leg per candidate fighter, grouped by weight class.

    INVENTORY WITHOUT A MODEL, deliberately. 81 live markets across 8 weight
    classes were being ingested by nothing at all until 2026-08-08; now they are
    stored and surfaced, but every row carries model_prob=None and is never
    staked, because no model for belt retention exists yet (see #109/#110 and
    TITLE_NO_BASELINE_REASON). Same posture this app already takes for
    method_of_victory and NFL preseason: show the market, refuse to invent a
    number for it.

    Surfacing them unpriced is not cosmetic -- it is what starts them accruing
    forward observation-log evidence, so the day a retention model lands there
    is already a history of market prices to score it against.
    """
    markets = (
        session.query(Market)
        .filter(Market.sport == "mma", Market.market_type == "title_holder", Market.status == "active")
        .all()
    )
    snapshots_by_market = _batch_latest_snapshots(session, [m.id for m in markets])
    out = []
    for m in markets:
        snap = snapshots_by_market.get(m.id)
        out.append(
            FuturesMarketOut(
                id=m.id,
                market_type=m.market_type,
                source=m.source,
                team=m.team,
                group_label=m.group_label,
                line=None,
                side=None,
                implied_prob=_implied_prob(snap),
                yes_bid=snap.yes_bid if snap else None,
                yes_ask=snap.yes_ask if snap else None,
                last_price=snap.last_price if snap else None,
                volume=snap.volume if snap else None,
                updated_at=m.updated_at.isoformat() if m.updated_at else None,
                model_prob=None,
                model_validated=False,
                edge=None,
                no_baseline_reason=TITLE_NO_BASELINE_REASON,
                kelly_fraction=None,
                suggested_stake_dollars=None,
                suggested_stake_units=None,
                stake_pool=None,
                line_move_pp=None,
            )
        )
    return out
