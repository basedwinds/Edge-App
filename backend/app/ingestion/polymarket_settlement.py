"""Settles pending Polymarket bets from the market's OWN resolution -- the
Polymarket counterpart to settle_from_kalshi_resolution().

Until now Polymarket bets could only be settled by the per-sport graders, which
need a result scraped from somewhere else and a name join that works. Kalshi bets
have had an authoritative path since the resolution settler was built; Polymarket
had none, which is why 2,804 bets sat pending.

WHY THIS IS NOT JUST "READ outcomePrices". Polymarket's resolution is trivially
readable; mapping it onto OUR bet is where it goes wrong, and probing 2,804
pending bets against live Gamma data turned up three separate ways to grade the
wrong thing. Each has an explicit guard below, and anything that does not clear a
guard is LEFT PENDING rather than guessed at -- a bet that stays pending is
visibly unfinished, a bet graded wrong is invisible and poisons every ROI number
downstream.

  1. THE OUTCOME NAME RARELY MATCHES EXACTLY. Of 1,500 bets on named-outcome
     markets, only 214 matched Gamma's `outcomes` exactly. 920 differed by case,
     and 366 were stored as a full name where Gamma lists a surname ("Michele
     Mecarelli" vs ["Mecarelli", "Meerschen"]). Hence the tiered matcher in
     resolve_outcome_index, which REQUIRES a unique hit at whichever tier
     matches and refuses the bet otherwise -- two players sharing a surname
     produce an ambiguous match, and ambiguous means skip.

  2. SOME MARKETS ARE Yes/No QUESTIONS WITH THE SUBJECT IN THE TITLE. MLB f5
     markets are stored with a team code ("KC", "TIE") while Gamma lists
     ["Yes", "No"] -- the team lives in the question text, not the outcome. There
     is no way to know from this payload whether "KC" is the Yes side. 104 bets
     are in this class and every one is skipped; only Yes/No markets whose stored
     side is itself yes/no (184 bets, mostly mlb rfi) are graded.

  3. A "RESOLVED" MARKET CAN PAY 50/50. 124 conditions carry
     umaResolutionStatus "resolved" with outcomePrices ["0.5","0.5"]. That is
     Polymarket's refund: both sides pay half, nobody won. Grading those by
     `max(prices)` would hand every one of them a spurious win or loss. They map
     to VOID, matching how the Kalshi path treats a voided market.

Network happens BEFORE the write lock; only the grade+commit takes it.
"""
import datetime
import json
import logging

from app.clients.base import get_json
from app.db.database import SessionLocal
from app.db.models import Market, PlacedBet
from app.ingestion.poller_lock import db_write_lock
from app.ingestion.polymarket_resolution import _BATCH, _GAMMA_MARKETS, condition_id
from app.models.bet_position import position_note, resolve_status_for_position

log = logging.getLogger("polymarket_settlement")

# A decisive binary resolution is 1/0. Anything else -- notably the 0.5/0.5
# refund -- is not a winner, and must not be read as one.
_DECISIVE_HI = 0.999
_DECISIVE_LO = 0.001
_VOID_MID = 0.5
_VOID_TOL = 1e-6


def _norm(s: str) -> str:
    return s.strip().lower()


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).replace("-", " ").replace(".", " ").split() if t}


def resolve_outcome_index(stored_side: str, outcomes: list[str]) -> int | None:
    """Index of `stored_side` within `outcomes`, or None if not UNIQUELY known.

    Tiers, tried in order and each requiring exactly one hit:
      exact -> case-insensitive -> shared word (full name vs surname).

    None is returned for both "no match" and "more than one match". The caller
    must treat None as "leave this bet pending"; there is no fallback tier,
    because the next-best guess after an ambiguous name match is a coin flip on
    someone's money.
    """
    if not stored_side or not outcomes:
        return None

    if outcomes.count(stored_side) == 1:
        return outcomes.index(stored_side)

    lowered = [_norm(o) for o in outcomes]
    target = _norm(stored_side)
    if lowered.count(target) == 1:
        return lowered.index(target)

    stored_tokens = _tokens(stored_side)
    hits = [i for i, o in enumerate(outcomes) if _tokens(o) & stored_tokens]
    if len(hits) == 1:
        return hits[0]
    return None


def grade(stored_side: str, gamma_market: dict) -> tuple[str | None, str]:
    """('won'|'lost'|'void'|None, reason). None means LEAVE PENDING.

    Pure: takes the stored side and one Gamma market row, touches nothing.
    Written this way so it can be back-tested against already-settled bets
    without a database write in sight.
    """
    if not gamma_market.get("closed"):
        return None, "market not closed"
    if gamma_market.get("umaResolutionStatus") != "resolved":
        return None, f"uma status {gamma_market.get('umaResolutionStatus')!r}"
    try:
        outcomes = json.loads(gamma_market.get("outcomes") or "[]")
        prices = [float(x) for x in json.loads(gamma_market.get("outcomePrices") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "unparseable outcomes/prices"
    if not outcomes or len(outcomes) != len(prices):
        return None, "outcomes/prices length mismatch"

    # The 50/50 refund, checked BEFORE any winner is picked.
    if all(abs(p - _VOID_MID) < _VOID_TOL for p in prices):
        return "void", "polymarket resolved 50/50 (refund)"

    if not (max(prices) >= _DECISIVE_HI and min(prices) <= _DECISIVE_LO):
        return None, f"resolution not decisive: {prices}"

    # A Yes/No market carries its subject in the question, not the outcomes, so
    # it is only gradeable when the stored side is itself yes/no.
    if {_norm(o) for o in outcomes} == {"yes", "no"} and _norm(stored_side) not in ("yes", "no"):
        return None, f"yes/no market but bet side is {stored_side!r} -- subject not in outcomes"

    idx = resolve_outcome_index(stored_side, outcomes)
    if idx is None:
        return None, f"side {stored_side!r} not uniquely matched in {outcomes}"
    return ("won" if prices[idx] >= _DECISIVE_HI else "lost"), f"outcome {outcomes[idx]!r} paid {prices[idx]}"


def fetch_closed_markets(condition_ids: list[str]) -> dict:
    """{conditionId: gamma market row} for the closed ones among `condition_ids`."""
    out: dict = {}
    for i in range(0, len(condition_ids), _BATCH):
        chunk = condition_ids[i : i + _BATCH]
        query = "&".join(f"condition_ids={c}" for c in chunk)
        try:
            rows = get_json(f"{_GAMMA_MARKETS}?{query}&closed=true&limit={_BATCH * 5}")
        except Exception:
            log.exception("polymarket resolution fetch failed for a chunk")
            continue
        for m in rows or []:
            if m.get("conditionId"):
                out[m["conditionId"]] = m
    return out


def stored_side(source_ticker: str) -> str:
    """The outcome half of "{conditionId}-{outcome}", or "" when there is none.

    82 pending bets sit on a bare conditionId with no outcome suffix. There is no
    side to grade there, so they return "" and get skipped.
    """
    cid = condition_id(source_ticker)
    return source_ticker[len(cid):].lstrip("-") if cid else ""


def settle_from_polymarket_resolution() -> int:
    """Grade every pending bet whose Polymarket market has resolved decisively."""
    session = SessionLocal()
    try:
        rows = (
            session.query(PlacedBet.id, Market.source_ticker)
            .join(Market, PlacedBet.market_id == Market.id)
            .filter(PlacedBet.status == "pending", Market.source == "polymarket",
                    Market.source_ticker.isnot(None))
            .all()
        )
    finally:
        session.close()
    if not rows:
        return 0

    cids = sorted({c for c in (condition_id(t) for _bid, t in rows) if c})
    if not cids:
        return 0
    gamma = fetch_closed_markets(cids)
    if not gamma:
        return 0

    now = datetime.datetime.utcnow()
    settled = 0
    skipped: dict = {}
    with db_write_lock():
        session = SessionLocal()
        try:
            for bid, ticker in rows:
                g = gamma.get(condition_id(ticker) or "")
                if g is None:
                    continue  # not resolved yet -- normal, not a problem
                status, reason = grade(stored_side(ticker), g)
                if status is None:
                    skipped[reason.split(" -- ")[0][:40]] = skipped.get(reason.split(" -- ")[0][:40], 0) + 1
                    continue
                bet = session.get(PlacedBet, bid)
                if bet is None or bet.status != "pending":
                    continue
                bet.status = resolve_status_for_position(bet, status)
                bet.settled_at = now
                bet.settlement_note = (
                    f"auto-settled from Polymarket resolution ({reason})" + position_note(bet)
                )
                settled += 1
            if settled:
                session.commit()
                log.info("settled %d bets from Polymarket market resolution", settled)
        finally:
            session.close()
    if skipped:
        log.info("polymarket settlement left bets pending: %s", skipped)
    return settled
