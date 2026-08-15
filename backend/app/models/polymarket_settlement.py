"""Settles pending bets from POLYMARKET'S own resolution.

Companion to kalshi_settlement.py, and the bigger half: Polymarket carries a
large share of this app's bets and had no authoritative settlement path at all,
so a bet there could only settle if a third-party results scraper happened to
catch the match. Johnny Speeds vs Metizport (user-reported 2026-08-03) sat
pending for exactly that reason.

WHY THIS IS STRONGER THAN THE KALSHI VERSION. Kalshi gives a yes/no on a whole
market, so that fallback had to be limited to market types where "yes" plainly
means the bet's team won. Polymarket instead publishes the outcome VECTOR:

    outcomes      = ["Metizport", "Johnny Speeds"]
    outcomePrices = ["1", "0"]

and this app already stores which outcome a bet is on -- Market.source_ticker is
"<conditionId>-<outcome name>". So the specific leg can be resolved directly,
which makes this safe for handicaps and other side-bearing markets that the
Kalshi path deliberately skips.

GATING. Only markets Polymarket reports closed AND umaResolutionStatus
"resolved", AND whose prices are decisive (one outcome at ~1, the rest ~0). A
market that is closed but still disputed, or priced mid-range, is left pending --
a late settlement is recoverable, a wrong one is not.
"""
import logging

from sqlalchemy.orm import Session

from app.db.models import Market, PlacedBet
from app.models.bet_position import position_note, resolve_status_for_position

log = logging.getLogger("polymarket_settlement")

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"

# How close a resolved outcome price must be to 1 (or 0) to be treated as final.
_DECISIVE = 0.99


def _split_ticker(ticker: str) -> tuple[str, str] | None:
    """"0xabc...-Johnny Speeds" -> ("0xabc...", "Johnny Speeds"). Outcome names
    can contain "-", so split once only."""
    if not ticker or "-" not in ticker:
        return None
    cid, outcome = ticker.split("-", 1)
    if not cid.startswith("0x") or not outcome:
        return None
    return cid, outcome


def _resolved_outcomes(condition_id: str) -> dict[str, float] | None:
    """{outcome name: resolved price} once the market is genuinely resolved."""
    import json

    import httpx

    try:
        # closed=true is required -- a resolved market drops off the default
        # (open) listing, which is what made these invisible in the first place.
        resp = httpx.get(GAMMA_MARKETS, params={"condition_ids": condition_id, "closed": "true"}, timeout=20.0)
        if resp.status_code != 200:
            return None
        rows = resp.json()
    except Exception:
        log.debug("polymarket lookup failed for %s", condition_id, exc_info=True)
        return None
    if not isinstance(rows, list) or not rows:
        return None
    m = rows[0]
    if not m.get("closed") or str(m.get("umaResolutionStatus") or "").lower() != "resolved":
        return None
    try:
        names = m["outcomes"] if isinstance(m["outcomes"], list) else json.loads(m["outcomes"])
        prices = m["outcomePrices"] if isinstance(m["outcomePrices"], list) else json.loads(m["outcomePrices"])
        vals = [float(p) for p in prices]
    except (KeyError, TypeError, ValueError):
        return None
    if len(names) != len(vals) or not any(v >= _DECISIVE for v in vals):
        return None  # not decisively resolved -- leave it alone
    return dict(zip(names, vals))


def _match_outcome(stored: str, resolved: dict[str, float]) -> float | None:
    """Find the stored leg in Polymarket's resolved outcome vector.

    REAL BUG this fixes (measured 2026-08-03). An exact `resolved.get(stored)`
    matched NOTHING. Sampling 40 pending Polymarket tennis bets, 10 had markets
    that were genuinely resolved and ZERO of those matched exactly: 9 differed
    only in case (we store "over", Polymarket publishes "Over") and 1 stored a
    full name against a surname ("Matt Hulme" vs ["Bouzige", "Hulme"]). So every
    Polymarket tennis bet that COULD have auto-settled was instead logged as a
    drifted outcome and left pending -- 1,994 were sitting unsettled.

    Three passes, each stricter about ambiguity than the last:
      1. exact
      2. case-insensitive
      3. surname (last token), and ONLY when exactly one outcome matches

    The ambiguity guard on pass 3 matters: an outcome vector of two players who
    share a surname must not be resolved by guessing. Same reasoning as the
    Flashscore matcher refusing surname-only pairing after it would have
    conflated the Tsitsipas brothers -- settling the wrong leg pays out the
    wrong side of a real bet, which is worse than settling late.
    """
    if stored in resolved:
        return resolved[stored]
    lowered = {name.lower(): price for name, price in resolved.items()}
    if stored.lower() in lowered:
        return lowered[stored.lower()]
    parts = stored.split()
    if not parts:
        return None
    surname = parts[-1].lower()
    hits = []
    for name, price in resolved.items():
        other = name.split()
        if not other or other[-1].lower() != surname:
            continue
        # Only fall back to the surname when the other side has NO given name to
        # contradict it ("Hulme" for our "Matt Hulme"). If both carry a given
        # name they must agree on the initial, otherwise this happily matches
        # "Stefanos Tsitsipas" to "Petros Tsitsipas" -- a real pair of brothers
        # on the same tour, and settling the wrong leg pays out the wrong side.
        if len(other) > 1 and len(parts) > 1 and other[0][:1].lower() != parts[0][:1].lower():
            continue
        hits.append(price)
    return hits[0] if len(hits) == 1 else None


def _leg_from_market(market: Market, outcomes: list[str]) -> str | None:
    """Which outcome a bare-ticker market represents, derived from the Market row.

    REAL GAP this closes. Most Polymarket tickers are "<conditionId>-<outcome>",
    but several upsert paths store the bare condition id with no outcome at all --
    1,454 markets across soccer (team_total, game_spread, half team totals),
    nascar (race_winner), nfl (super_bowl_champion, conference_champion,
    division_winner, playoff_qualifier, mvp) and f1 (h2h, drivers_champion,
    race_winner, constructor_pole). Settlement could not tell which side those
    bets were on, so they could never grade -- 230 were sitting pending.

    Deriving the leg here rather than changing the ticker is deliberate: the
    ticker IS the market's identity, so rewriting it would either orphan every
    existing row or create a duplicate market alongside each one. The Market row
    already carries everything needed.

    Three real shapes, confirmed against live Gamma responses:
      ["Yes", "No"]                 -> a per-entity futures/prop question
                                       ("Will Bottas be champion?"); our row is
                                       always the affirmative side.
      ["Over", "Under"]             -> a total; these paths store the OVER price
                                       (see _upsert_polymarket_spread_shaped_row),
                                       and Market.side is honoured when set.
      two named competitors         -> a spread; Market.team names our side.

    Validated on 30 randomly sampled bare-ticker markets: every one resolved
    (18 Over, 9 Yes, 3 by team name), none ambiguous. Returns None rather than
    guessing on any shape it does not recognise.
    """
    if not outcomes:
        return None
    lowered = [o.strip().lower() for o in outcomes]
    if lowered == ["yes", "no"]:
        return "Yes"
    if set(lowered) == {"over", "under"}:
        side = (market.side or "over").strip().lower()
        want = "under" if side.startswith("u") else "over"
        for original, low in zip(outcomes, lowered):
            if low == want:
                return original
        return None
    if market.team:
        target = market.team.strip().lower()
        hits = [o for o, low in zip(outcomes, lowered) if low == target]
        if len(hits) == 1:
            return hits[0]
        # Head-to-head rows store BOTH competitors in `team` ("Russell vs
        # Antonelli"), so the exact match above cannot fire. The FIRST name is
        # our side, and that is a guarantee rather than a guess:
        # polymarket_racing_client builds this label FROM the outcomes order
        # precisely so last_price stays aligned with the first-named driver
        # ("Polymarket's outcomes are NOT always in groupItemTitle order").
        # bet_settlement._grade_racing_h2h splits the same field the same way.
        import re as _re
        halves = _re.split(r"\s+vs\.?\s+", market.team.strip(), flags=_re.IGNORECASE)
        if len(halves) == 2:
            first = halves[0].strip().lower()
            hits = [o for o, low in zip(outcomes, lowered) if low == first]
            if len(hits) == 1:
                return hits[0]
    return None


def settle_pending_from_polymarket(session: Session, bets: list[PlacedBet]) -> int:
    """Grade `bets` from Polymarket's resolution. Returns how many settled."""
    import datetime

    settled = 0
    for bet in bets:
        market = session.get(Market, bet.market_id) if bet.market_id else None
        if market is None or market.source != "polymarket" or not market.source_ticker:
            continue
        parts = _split_ticker(market.source_ticker)
        if parts is None:
            # Bare condition id -- no outcome suffix. Recoverable from the Market
            # row itself; see _leg_from_market.
            if not market.source_ticker.startswith("0x"):
                continue
            resolved = _resolved_outcomes(market.source_ticker)
            if not resolved:
                continue
            leg = _leg_from_market(market, list(resolved))
            if leg is None:
                log.warning("polymarket bare ticker, leg not derivable: %s %s outcomes=%s",
                            market.sport, market.market_type, sorted(resolved))
                continue
        else:
            resolved = _resolved_outcomes(parts[0])
            if not resolved:
                continue
            leg = parts[1]
        price = _match_outcome(leg, resolved)
        if price is None:
            # Outcome name drifted from what we stored -- do not guess which leg
            # this bet was on.
            log.warning("polymarket outcome %r not in %s", leg, sorted(resolved))
            continue
        if price >= _DECISIVE:
            bet.status = resolve_status_for_position(bet, "won")
        elif price <= 1 - _DECISIVE:
            bet.status = resolve_status_for_position(bet, "lost")
        else:
            continue
        bet.settled_at = datetime.datetime.utcnow()
        bet.settlement_note = (
            f"auto-settled from Polymarket resolution ({parts[1]} @ {price:g})"
            + position_note(bet)
        )
        market.status = "closed"
        settled += 1

    if settled:
        session.commit()
        log.info("polymarket settlement: settled %d pending bets", settled)
    return settled
