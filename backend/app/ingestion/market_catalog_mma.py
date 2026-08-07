"""DB upsert layer for UFC fights/markets -- parallel to
market_catalog_mlb.py, same architecture-decision reasoning as
market_matcher_mma.py. Every Market/PlacedBet row this writes gets
sport="mma".

Market.team has no fixed roster to validate against for MMA (unlike a
30-team abbreviation set) -- it holds the fighter's real full name directly,
same field, repurposed string content. market.side holds the method bucket
("decision"/"kotko"/"submission"/"draw") for method-of-victory/finish rows,
where team alone isn't enough to disambiguate the outcome.
"""
import datetime

from sqlalchemy.orm import Session

from app.clients.polymarket_client import quote_fields
from app.db.models import Market, MarketSnapshot, MmaFight


def upsert_mma_fights(session: Session, fights: list[dict]) -> int:
    count = 0
    for f in fights:
        existing = session.get(MmaFight, f["id"])
        if existing is None:
            existing = MmaFight(id=f["id"])
            session.add(existing)
        existing.event_id = f["event_id"]
        existing.event_name = f["event_name"]
        existing.event_date = f["event_date"]
        existing.weight_class = f.get("weight_class")
        existing.is_title_bout = f.get("is_title_bout", 0)
        existing.fighter_a_id = f["fighter_a_id"]
        existing.fighter_a_name = f["fighter_a_name"]
        existing.fighter_b_id = f["fighter_b_id"]
        existing.fighter_b_name = f["fighter_b_name"]
        existing.winner_id = f.get("winner_id")
        existing.method = f.get("method")
        existing.round = f.get("round")
        existing.time = f.get("time")
        existing.scheduled_rounds = f.get("scheduled_rounds")
        existing.went_the_distance = f.get("went_the_distance")
        count += 1
    session.commit()
    return count


def _set_fight_id(market: Market, mma_fight_id: str | None) -> None:
    """Set the fight link, but NEVER downgrade a known link back to None.

    REAL REGRESSION this fixes (caught 2026-08-06 by a link-count monitor, not
    by anyone looking): poller_mma resolves fight ids from the MONEYLINE series
    only and reuses that mapping for every other series. When a suffix stops
    appearing in the moneyline series while its distance/method/rounds/
    round-of-victory events are still listed, the resolver returns None and this
    assignment used to overwrite perfectly good links with NULL -- 14 of them at
    once for 26AUG08JOHROS, un-pricing and un-settling the whole fight.

    CORRECTION to the first diagnosis, which is worth keeping because the wrong
    version is the plausible one: this was originally written up as "Kalshi
    retires the moneyline before the other series", as if it were a routine
    ordering. It is not. The next day 26AUG08JOHVAZ appeared -- Miles Johns vs
    Gianni VAZQUEZ. The Rosas bout had been REPLACED, Kalshi pulled its
    moneyline when the matchup died, and the other JOHROS series lingered a
    while as stale leftovers before being closed too (all 23 are now inactive).
    So the trigger is an opponent change, not a retirement schedule. The guard
    below is right either way -- a failed lookup is not evidence the old answer
    was wrong -- but do not go looking for a moneyline-retires-first rule,
    because there isn't one.

    A failed lookup is not evidence that the old answer was wrong. The poller
    now also recovers names from the other series' event titles (see
    poller_mma), so this is the belt to that braces -- and it is the reason the
    link count kept oscillating (48 -> 0 -> 14) across polls.
    """
    if mma_fight_id is not None or market.mma_fight_id is None:
        market.mma_fight_id = mma_fight_id


def upsert_kalshi_mma_moneyline_market(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="moneyline", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    market.team = row["fighter_name"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mma_moneyline_row(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['fighter_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="moneyline", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    market.team = row["fighter_name"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, row.get("last_price")),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mma_distance_market(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="distance", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    market.team = None
    market.side = "yes"  # single binary market, "Yes" = goes the distance
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mma_distance_row(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    source_ticker = f"{row['condition_id']}-yes"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="distance", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    market.team = None
    market.side = "yes"
    market.status = row.get("status") or "active"
    session.flush()
    yes_price = None
    if len(row["outcomes"]) == 2 and len(row["outcome_prices"]) == 2:
        for outcome, price in zip(row["outcomes"], row["outcome_prices"]):
            if outcome == "Yes":
                yes_price = price
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, yes_price),
        last_price=yes_price, volume=row.get("volume"),
    ))
    return market


_MOV_METHOD_BY_TICKER_SUFFIX_HINT = (("DEC", "decision"), ("KOTKODQ", "kotko"), ("SUB", "submission"))


def upsert_kalshi_mma_mov_market(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    """7-way: fighter x {decision, kotko, submission} + a single draw
    market. Ticker suffix carries the real outcome code (e.g. "-USMDEC",
    "-DRAWDRAW") -- more reliable than parsing yes_sub_title's free text."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="method_of_victory", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    if row.get("is_draw_outcome"):
        market.team = None
        market.side = "draw"
    else:
        # yes_sub_title is "{Fighter Name} by {Method}" -- split on the last " by "
        sub = row.get("yes_sub_title", "")
        if " by " in sub:
            fighter_name, method_label = sub.rsplit(" by ", 1)
        else:
            fighter_name, method_label = sub, ""
        market.team = fighter_name
        method_label_lower = method_label.lower()
        if "decision" in method_label_lower:
            market.side = "decision"
        elif "submission" in method_label_lower:
            market.side = "submission"
        elif "ko" in method_label_lower or "tko" in method_label_lower:
            market.side = "kotko"
        else:
            market.side = None
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mma_mof_market(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    """4-way, fight-level (not per-fighter): KO/TKO/DQ, Submission,
    Decision, Draw/No Contest."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="method_of_finish", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    market.team = None
    sub_lower = row.get("yes_sub_title", "").lower()
    if "decision" in sub_lower:
        market.side = "decision"
    elif "submission" in sub_lower:
        market.side = "submission"
    elif "ko" in sub_lower or "tko" in sub_lower:
        market.side = "kotko"
    elif "draw" in sub_lower or "no contest" in sub_lower:
        market.side = "draw"
    else:
        market.side = None
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mma_method_row(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    """Covers all three of polymarket_mma_client.get_method_markets()'s
    method_kind values -- market_type/team/side chosen per kind so this
    lines up with the Kalshi method_of_victory/method_of_finish split even
    though Polymarket has no fight-level "method of finish only" market
    itself (fight_kotko/fight_submission ARE that, just split across two
    binary markets instead of one 4-way one)."""
    source_ticker = f"{row['condition_id']}-yes"
    kind = row["method_kind"]
    market_type = "method_of_victory" if kind == "fighter_kotko" else "method_of_finish"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type=market_type, sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    if kind == "fighter_kotko":
        market.team = row["fighter_name"]
        market.side = "kotko"
    elif kind == "fight_kotko":
        market.team = None
        market.side = "kotko"
    else:  # fight_submission
        market.team = None
        market.side = "submission"
    market.status = row.get("status") or "active"
    session.flush()
    yes_price = None
    if len(row["outcomes"]) == 2 and len(row["outcome_prices"]) == 2:
        for outcome, price in zip(row["outcomes"], row["outcome_prices"]):
            if outcome == "Yes":
                yes_price = price
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, yes_price),
        last_price=yes_price, volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mma_rounds_market(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    """Ladder: "ends before round N?" -- market.line holds N."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="rounds", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    market.team = None
    market.line = float(row["before_round"])
    market.side = "under"  # "ends BEFORE round N" == fight duration under N
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_mma_rounds_row(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="rounds", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()
    over_price = None
    if len(row["outcomes"]) == 2 and len(row["outcome_prices"]) == 2:
        for outcome, price in zip(row["outcomes"], row["outcome_prices"]):
            if outcome == "Over":
                over_price = price
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, over_price),
        last_price=over_price, volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_mma_round_of_victory_market(session: Session, row: dict, mma_fight_id: str | None) -> Market:
    """Fighter x round-of-victory grid + a single "OTHER" (decision/draw/NC)
    bucket. yes_sub_title is "{Fighter} to win in Round {N}" or "Decision /
    Draw / No Contest" for OTHER."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="round_of_victory", sport="mma",
        )
        session.add(market)
    _set_fight_id(market, mma_fight_id)
    if row.get("is_other_outcome"):
        market.team = None
        market.side = "other"
    else:
        sub = row.get("yes_sub_title", "")
        if " to win in Round " in sub:
            fighter_name, round_str = sub.split(" to win in Round ")
            market.team = fighter_name
            market.line = float(round_str.strip()) if round_str.strip().isdigit() else None
        else:
            market.team = None
        market.side = None
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        yes_bid=row.get("yes_bid"), yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market
