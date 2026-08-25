"""DB upsert layer for Tennis matches/markets -- parallel to
market_catalog_mma.py, with one real structural difference: MMA's MmaFight
rows come from an authoritative external schedule (ufcstats' upcoming-card
page); no equivalent free schedule source exists for Tennis (tennis-data.co.uk
and tennisexplorer.com are both RESULTS archives, not draws/schedules --
confirmed live 2026-07-18, neither lists future not-yet-played matches).
Kalshi/Polymarket's own moneyline listings ARE the closest thing to a live
Tennis schedule this app has, so `find_or_create_upcoming_match` derives
TennisMatch rows directly from whichever platform's poller runs first, and
the OTHER platform's poller then matches onto that same row by player name
(via market_matcher_tennis.match_upcoming_tennis_match) rather than creating
a duplicate -- this is the opposite direction of every other sport's
"external schedule feeds both platforms' matchers" pattern in this app.

Every Market/PlacedBet row this writes gets sport="tennis". Market.team
holds the player's real full name (same repurposed-field pattern as
MmaFight's fighter names)."""
import datetime
import logging
import re

from sqlalchemy.orm import Session

from app.clients.polymarket_client import quote_fields
from app.db.models import Market, MarketSnapshot, TennisMatch
from app.ingestion.market_matcher_tennis import full_name_to_abbreviated_key, match_upcoming_tennis_match

log = logging.getLogger(__name__)


def _load_upcoming_matches(session: Session) -> list[dict]:
    rows = session.query(TennisMatch).filter(TennisMatch.winner_key.is_(None)).all()
    return [
        {"id": r.id, "player_a_name": r.player_a_name, "player_b_name": r.player_b_name}
        for r in rows
    ]


# Grand Slam surface, keyed by a lowercase substring found in whichever
# tournament-name text the caller has (Kalshi's product_metadata.competition,
# e.g. "Wimbledon Men Singles"; Polymarket's event title, e.g. "Australian
# Open Men's: A vs B" -- both confirmed live 2026-07-18). "us open" is safe
# as a bare substring here since this text only ever comes from Tennis event
# titles, never e.g. golf. Values use the SAME casing ("Hard"/"Clay"/"Grass")
# as TennisMatch.surface's historical values (tennis-data.co.uk/tennisexplorer,
# see models.py) -- elo_tennis.py keys its surface-rating dict on this string
# verbatim, so a casing mismatch would silently zero out the surface blend
# for every live Slam match rather than erroring.
_SLAM_SURFACE_BY_NAME = {
    "wimbledon": "Grass",
    "french open": "Clay",
    "roland garros": "Clay",
    "australian open": "Hard",
    "us open": "Hard",
}


def _infer_slam_attributes(tour: str, tournament_text: str) -> tuple[int, str] | None:
    """Best-effort best_of/surface for a live-created match. Fixes a real gap:
    without this, EVERY live-created TennisMatch defaulted to best_of=3 and
    surface=None (see tennis_markets.py::_match_best_of) since no free live
    source flags Grand Slam status -- silently wrong the moment a real Bo5
    men's Slam match went live (game_total/set_total would price off Bo3
    constants). Deliberately narrow: only resolves the 4 Slams, not every
    tournament's real surface -- the rest of this app already tolerates
    surface=None as an honest gap (elo_tennis.py blends gracefully), so this
    only needs to fix the one case that was actively wrong, not guess broadly.
    Qualifying rounds are always best-of-3 for both tours even at a Slam."""
    text = tournament_text.lower()
    surface = next((s for name, s in _SLAM_SURFACE_BY_NAME.items() if name in text), None)
    if surface is None:
        return None
    is_qualifying = "qualif" in text
    best_of = 3 if (tour == "wta" or is_qualifying) else 5
    return best_of, surface


def infer_tour_and_tier_from_text(title: str, default_tour: str) -> tuple[str, str]:
    """Best-effort tour/tier from a platform's own event title.

    For the Polymarket poller only, which gets no tier from its slug. This is
    still a GUESS -- it just beats the hardcoded "atp"/"tour" that was labelling
    `ITF W15 Tianjin 2 Women` as an ATP Tour match. Absent tokens fall back to
    the caller's default, so it can only add information, never remove it.
    """
    text = (title or "").lower()
    if "itf" in text:
        tier = "itf"
    elif "challenger" in text:
        tier = "challenger"
    else:
        tier = "tour"
    # ITF names its women's events "ITF W15 ..." / "... Women"; the men's are
    # "M15". Checked as a whole token so "Women" inside a venue name is the
    # only false positive available, and there is no men's marker to lose.
    tour = default_tour
    if "women" in text or "wta" in text or re.search(r"\bw\d{2}\b", text):
        tour = "wta"
    elif re.search(r"\bm\d{2}\b", text):
        tour = "atp"
    return tour, tier


def find_or_create_upcoming_match(
    session: Session, tour: str, tier: str, player_a_name: str, player_b_name: str,
    tournament_text: str = "", authoritative_tier: bool = False,
) -> TennisMatch | None:
    if not player_a_name or not player_b_name:
        return None
    upcoming = _load_upcoming_matches(session)
    found = match_upcoming_tennis_match(player_a_name, player_b_name, upcoming)
    if found is not None:
        existing = session.get(TennisMatch, found["id"])
        # CORRECT A GUESSED TIER. Only one caller KNOWS the tier: the Kalshi
        # poller, whose series ticker states it outright
        # (KXATPCHALLENGERMATCH/KXITFWMATCH/...). The Polymarket poller cannot
        # tell them apart from a slug and passes a placeholder. Whichever runs
        # FIRST creates the row, and this function used to return an existing
        # row untouched -- so a placeholder written by Polymarket outlived
        # every later Kalshi pass that knew better. Measured 2026-08-10: 108 of
        # 218 active Kalshi tennis markets displayed as ATP Tour, including
        # ITF W15 women's matches labelled `tour=atp`.
        #
        # Only an authoritative caller may overwrite, and only downward from a
        # guess -- a guessing caller must never undo a known-good value.
        if existing is not None and authoritative_tier:
            if tier and existing.tier != tier:
                log.info("corrected tennis tier for %s vs %s: %s -> %s",
                         existing.player_a_key, existing.player_b_key, existing.tier, tier)
                existing.tier = tier
            if tour and existing.tour != tour:
                log.info("corrected tennis tour for %s vs %s: %s -> %s",
                         existing.player_a_key, existing.player_b_key, existing.tour, tour)
                existing.tour = tour
        return existing

    slam = _infer_slam_attributes(tour, tournament_text)
    resolved_date = datetime.date.today().isoformat()
    # REAL BUG this fixes (hit live 2026-08-03): the synthetic id carried no date,
    # so once a live-created match FINISHED its row kept the key forever -- and the
    # existence check above only looks at UNFINISHED matches (winner_key is NULL).
    # The same pair appearing again therefore matched nothing, then died on
    # "UNIQUE constraint failed: tennis_matches.source, tennis_matches.source_match_id",
    # aborting the whole Polymarket tennis refresh so no prices updated at all.
    # cs2/lol/soccer already date-stamp their synthetic ids for exactly this reason
    # (see market_catalog_cs2.py); tennis and valorant were the two that never did.
    #
    # Dating the key also keeps a genuine rematch as its OWN row rather than
    # overwriting the played fixture -- the failure the user hit on LoL, where a
    # settled bet became unsettleable because its match_date moved to the rematch.
    source_match_id = f"live:{tour}:{tier}:{player_a_name}:{player_b_name}:{resolved_date}"
    # Re-check against the DB, not the snapshot above: a row created earlier in
    # THIS run is invisible to it (same reasoning as the cs2 guard).
    existing = (
        session.query(TennisMatch)
        .filter_by(source="live", source_match_id=source_match_id)
        .one_or_none()
    )
    if existing is not None:
        return existing
    match = TennisMatch(
        source="live", source_match_id=source_match_id,
        tour=tour, tier=tier, tourney_name=tournament_text,
        best_of=slam[0] if slam else None,
        surface=slam[1] if slam else None,
        match_date=resolved_date,
        # Stored in the SAME abbreviated "surname i." key space
        # normalize_player_key() builds from tennis-data.co.uk/tennisexplorer's
        # own historical rows (see full_name_to_abbreviated_key's docstring) --
        # NOT the raw full name -- so elo_service_tennis's offline-trained
        # ratings are actually reachable for a live-created match. Falls back
        # to the lowercased full name only for the (rare) single-token-name
        # case full_name_to_abbreviated_key can't convert -- that player
        # simply won't have an offline rating to look up either way.
        player_a_key=full_name_to_abbreviated_key(player_a_name) or player_a_name.lower(),
        player_a_name=player_a_name,
        player_b_key=full_name_to_abbreviated_key(player_b_name) or player_b_name.lower(),
        player_b_name=player_b_name,
    )
    session.add(match)
    session.flush()
    return match


# PRECEDENCE, because "whichever poller ran last wins" is not a rule.
#
# REAL BUG (user-reported 2026-08-10): Haliak vs Liu flipped between 05:00Z and
# 11:00Z and back, over and over. Reproduced in one cycle -- the Kalshi poller
# wrote 11:00Z, the Polymarket poller wrote 05:00Z, and both tagged the row
# "platform", so neither could tell it was undoing the other. 141 of 341 tennis
# fixtures with live markets are listed on BOTH platforms and could do this.
#
# Kalshi ranks LOWEST on purpose, and not as a tie-break preference: for this
# very match its occurrence_datetime (11:00Z) is byte-identical to its
# expected_expiration_time. Kalshi's tennis "start" is an EXPIRY -- late by a
# whole match -- which is the same structural fact measured across every soccer
# series. Polymarket's value is not provably an expiry, so it wins over Kalshi
# and loses to a real order of play.
#
# Kalshi is not silenced, only outranked: 105 of those 341 fixtures are listed
# ONLY on Kalshi, and blanking their start would disable the already-started
# gate entirely rather than merely making it late.
#
# Legacy rows tagged plainly "platform" rank with Polymarket, so an existing
# value stops being clobbered by Kalshi from the first pass.
_START_SOURCE_RANK = {
    "kalshi": 1,
    # Legacy rows written before this function knew which platform it was
    # hearing from. Ranked WITH Kalshi, not above it: ranking them higher would
    # have frozen the 105 fixtures listed only on Kalshi, whose rescheduled
    # times could then never be picked up again. Ambiguity resolves itself
    # within one cycle -- the first real write retags the row, after which
    # precedence applies normally.
    "platform": 1,
    "polymarket": 2,
    "flashscore": 3,     # real order of play; agrees with tennisexplorer where
    "tennisexplorer": 3,  # both carry a match (see flashscore_tennis_client)
}


def update_match_estimated_start_time(match: TennisMatch | None, estimated_start_time: str | None,
                                      source: str = "platform") -> None:
    """Keeps TennisMatch.estimated_start_time fresh every poll while the match
    is still upcoming; never touched once the match is decided (winner_key set).

    A write lands only if `source` ranks at least as high as whatever wrote the
    current value. Equal ranks still overwrite, so a source refreshing its own
    estimate (a genuine reschedule) is never frozen out -- what is blocked is a
    WEAKER source undoing a better one, which is what made rows flicker.
    """
    if match is None or match.winner_key is not None or not estimated_start_time:
        return
    incoming = _START_SOURCE_RANK.get(source, 2)
    current = _START_SOURCE_RANK.get(match.start_time_source or "", 0)
    if incoming < current:
        return
    match.estimated_start_time = estimated_start_time
    match.start_time_source = source


def update_match_expected_expiration(match: TennisMatch | None, expected_expiration_time: str | None) -> None:
    """Kalshi revises expected_expiration_time on a reschedule but never
    occurrence_datetime, so keeping both is what lets the router tell a trusted
    start from a stale one."""
    if match is not None and match.winner_key is None and expected_expiration_time:
        match.expected_expiration_time = expected_expiration_time


def upsert_kalshi_tennis_moneyline_market(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="moneyline", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = row["player_name"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        # KALSHI ROWS ARE ALREADY ORIENTED, so their quote is passed straight
        # through. quote_fields() is a POLYMARKET helper that reads raw_bid/
        # raw_ask and orients them against the row's own price -- and the Kalshi
        # tennis client never sets raw_bid/raw_ask at all, so calling it here
        # silently returned (None, None) and DISCARDED the bid/ask the client
        # had correctly read from yes_bid_dollars/yes_ask_dollars.
        #
        # WHAT THAT COST (found 2026-08-25). Every Kalshi tennis snapshot stored
        # a null book. The spread guard cannot fire on a MISSING book -- absence
        # is deliberately not treated as a wide spread -- so these rows fell
        # through to the volume gate, which passes on stale cumulative volume,
        # and were then priced against a stale last_price. Live result: 13 staked
        # US Open game_total bets with "edges" of +42 to +67pp against Kalshi
        # books that were actually 0.00 bid / 0.99 ask -- empty, zero open
        # interest, zero liquidity. Every other sport's Kalshi catalog already
        # passes the quote through directly; tennis was the only one that did not.
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_tennis_moneyline_row(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    source_ticker = f"{row['condition_id']}-{row['player_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="moneyline", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = row["player_name"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, row.get("last_price")),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_tennis_set_winner_market(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """market.line holds the SET NUMBER (1, 2, ...) -- Kalshi structures
    this as one event PER SET, not one per match, see kalshi_tennis_client.py."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="set_winner", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = row["player_name"]
    market.line = float(row["set_number"])
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        # KALSHI ROWS ARE ALREADY ORIENTED, so their quote is passed straight
        # through. quote_fields() is a POLYMARKET helper that reads raw_bid/
        # raw_ask and orients them against the row's own price -- and the Kalshi
        # tennis client never sets raw_bid/raw_ask at all, so calling it here
        # silently returned (None, None) and DISCARDED the bid/ask the client
        # had correctly read from yes_bid_dollars/yes_ask_dollars.
        #
        # WHAT THAT COST (found 2026-08-25). Every Kalshi tennis snapshot stored
        # a null book. The spread guard cannot fire on a MISSING book -- absence
        # is deliberately not treated as a wide spread -- so these rows fell
        # through to the volume gate, which passes on stale cumulative volume,
        # and were then priced against a stale last_price. Live result: 13 staked
        # US Open game_total bets with "edges" of +42 to +67pp against Kalshi
        # books that were actually 0.00 bid / 0.99 ask -- empty, zero open
        # interest, zero liquidity. Every other sport's Kalshi catalog already
        # passes the quote through directly; tennis was the only one that did not.
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_tennis_game_spread_market(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """market.line holds the game-spread threshold; market.team is the
    player this specific YES side favors (same "wins by more than line"
    convention as game_lines_tennis.py::prob_game_spread_cover)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="game_spread", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = row.get("player_name")
    market.line = row.get("line")
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        # KALSHI ROWS ARE ALREADY ORIENTED, so their quote is passed straight
        # through. quote_fields() is a POLYMARKET helper that reads raw_bid/
        # raw_ask and orients them against the row's own price -- and the Kalshi
        # tennis client never sets raw_bid/raw_ask at all, so calling it here
        # silently returned (None, None) and DISCARDED the bid/ask the client
        # had correctly read from yes_bid_dollars/yes_ask_dollars.
        #
        # WHAT THAT COST (found 2026-08-25). Every Kalshi tennis snapshot stored
        # a null book. The spread guard cannot fire on a MISSING book -- absence
        # is deliberately not treated as a wide spread -- so these rows fell
        # through to the volume gate, which passes on stale cumulative volume,
        # and were then priced against a stale last_price. Live result: 13 staked
        # US Open game_total bets with "edges" of +42 to +67pp against Kalshi
        # books that were actually 0.00 bid / 0.99 ask -- empty, zero open
        # interest, zero liquidity. Every other sport's Kalshi catalog already
        # passes the quote through directly; tennis was the only one that did not.
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_tennis_game_total_market(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """market.line holds the total-games threshold; market.side="over"
    (single-sided ladder, same convention as this app's other totals)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="game_total", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = None
    market.line = row.get("line")
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        # KALSHI ROWS ARE ALREADY ORIENTED, so their quote is passed straight
        # through. quote_fields() is a POLYMARKET helper that reads raw_bid/
        # raw_ask and orients them against the row's own price -- and the Kalshi
        # tennis client never sets raw_bid/raw_ask at all, so calling it here
        # silently returned (None, None) and DISCARDED the bid/ask the client
        # had correctly read from yes_bid_dollars/yes_ask_dollars.
        #
        # WHAT THAT COST (found 2026-08-25). Every Kalshi tennis snapshot stored
        # a null book. The spread guard cannot fire on a MISSING book -- absence
        # is deliberately not treated as a wide spread -- so these rows fell
        # through to the volume gate, which passes on stale cumulative volume,
        # and were then priced against a stale last_price. Live result: 13 staked
        # US Open game_total bets with "edges" of +42 to +67pp against Kalshi
        # books that were actually 0.00 bid / 0.99 ask -- empty, zero open
        # interest, zero liquidity. Every other sport's Kalshi catalog already
        # passes the quote through directly; tennis was the only one that did not.
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_kalshi_tennis_exact_match_market(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """market.team is the player this scoreline is about; market.side
    encodes the exact scoreline as "{player_sets}-{opponent_sets}" (e.g.
    "2-1") -- a string encoding since there's no existing numeric field
    shaped for a two-part score."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="exact_score", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = row["player_name"]
    market.side = f"{row['player_sets']}-{row['opponent_sets']}"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        # KALSHI ROWS ARE ALREADY ORIENTED, so their quote is passed straight
        # through. quote_fields() is a POLYMARKET helper that reads raw_bid/
        # raw_ask and orients them against the row's own price -- and the Kalshi
        # tennis client never sets raw_bid/raw_ask at all, so calling it here
        # silently returned (None, None) and DISCARDED the bid/ask the client
        # had correctly read from yes_bid_dollars/yes_ask_dollars.
        #
        # WHAT THAT COST (found 2026-08-25). Every Kalshi tennis snapshot stored
        # a null book. The spread guard cannot fire on a MISSING book -- absence
        # is deliberately not treated as a wide spread -- so these rows fell
        # through to the volume gate, which passes on stale cumulative volume,
        # and were then priced against a stale last_price. Live result: 13 staked
        # US Open game_total bets with "edges" of +42 to +67pp against Kalshi
        # books that were actually 0.00 bid / 0.99 ask -- empty, zero open
        # interest, zero liquidity. Every other sport's Kalshi catalog already
        # passes the quote through directly; tennis was the only one that did not.
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_tennis_set_winner_row(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """market.line holds the SET NUMBER, same convention as Kalshi's
    set_winner (see upsert_kalshi_tennis_set_winner_market)."""
    source_ticker = f"{row['condition_id']}-{row['player_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="set_winner", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = row["player_name"]
    market.line = float(row["set_number"])
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, row.get("last_price")),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_tennis_match_total_row(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """Same market_type ("game_total") as Kalshi's match-level total --
    reuses the exact same model_prob function in tennis_markets.py."""
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="game_total", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, row.get("over_price")),
        last_price=row.get("over_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_tennis_set_handicap_row(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """Same market_type ("set_spread") as Kalshi's KXATPGSPREAD -- confirmed
    live to be the same real concept (games differential for the whole
    match), just named "Set Handicap" by Polymarket and structured as a
    single +/-1.5 line rather than a ladder."""
    source_ticker = f"{row['condition_id']}-{row['player_name']}"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="set_spread", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = row["player_name"]
    market.line = row["line"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, row.get("last_price")),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_tennis_set_game_total_row(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """PER-SET game total ("set_total" market_type) -- no Kalshi equivalent.
    market.side encodes the set number (e.g. "set_1") since market.line is
    already used for the O/U threshold itself."""
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="set_total", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = None
    market.line = row["line"]
    market.side = f"set_{row['set_number']}"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, row.get("over_price")),
        last_price=row.get("over_price"), volume=row.get("volume"),
    ))
    return market


def upsert_polymarket_tennis_total_sets_row(session: Session, row: dict, tennis_match_id: int | None) -> Market:
    """Whether the match goes the full distance in SET count -- no Kalshi
    equivalent. market_type="total_sets", market.line is the O/U threshold."""
    source_ticker = f"{row['condition_id']}-over"
    market = session.query(Market).filter_by(source="polymarket", source_ticker=source_ticker).one_or_none()
    if market is None:
        market = Market(
            source="polymarket", source_ticker=source_ticker, source_event_id=row["event_slug"],
            market_type="total_sets", sport="tennis",
        )
        session.add(market)
    market.tennis_match_id = tennis_match_id
    market.team = None
    market.line = row["line"]
    market.side = "over"
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        **quote_fields(row, row.get("over_price")),
        last_price=row.get("over_price"), volume=row.get("volume"),
    ))
    return market


# Real tournament-name -> tennisexplorer.com slug (confirmed live 2026-07-19
# against every currently-open ATP/WTA tournament-winner event: "ATP Bastad"
# -> bastad, "ATP Umag" -> umag, "ATP Gstaad" -> gstaad, "WTA Iasi" -> iasi,
# "2026 WTA Athens" -> athens, "US Open" -> us-open, all real 200s). NOT
# derived from the Kalshi event ticker's own suffix, which is inconsistent
# (confirmed live: Bastad's ticker is "KXATP-26BASTAD" but Athens' is a bare
# "KXATP-26", no city code at all) -- the `competition` text Kalshi already
# provides is the one reliable source for this.
def tournament_name_to_slug(competition: str) -> str:
    import re

    text = re.sub(r"^\d{4}\s+", "", competition)  # strip a leading year, e.g. "2026 WTA Athens"
    text = re.sub(r"^(ATP|WTA)\s+", "", text, flags=re.IGNORECASE)
    # Strip the "(Men's)"/"(Women's)" display suffix this module's own
    # upsert appends to Grand Slam group_labels (see
    # upsert_kalshi_tennis_tournament_winner_market) -- the slug itself is
    # gender-agnostic (tennisexplorer's own /atp-men//wta-women/ path suffix
    # already carries that distinction).
    text = re.sub(r"\s*\((Men's|Women's)\)\s*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", "-", text.strip().lower())


def upsert_kalshi_tennis_tournament_winner_market(session: Session, row: dict) -> Market:
    """market_type="tournament_winner", market.team is the player's real
    full name, market.group_label is the real tournament name (e.g. "ATP
    Bastad") -- same shape FuturesMarketOut/other sports' futures already
    use, no tennis_match_id (this isn't tied to one specific match)."""
    market = session.query(Market).filter_by(source="kalshi", source_ticker=row["ticker"]).one_or_none()
    if market is None:
        market = Market(
            source="kalshi", source_ticker=row["ticker"], source_event_id=row["event_ticker"],
            market_type="tournament_winner", sport="tennis",
        )
        session.add(market)
    market.team = row["player_name"]
    # Grand Slam competition text (e.g. "US Open") carries NO gender marker
    # at all, unlike every other tournament ("ATP Bastad"/"WTA Iasi") --
    # confirmed live 2026-07-19 once the real Women's US Open market
    # appeared alongside the Men's, both under the identical bare "US Open"
    # text. Appends "(Men's)"/"(Women's)" so the two don't display as if
    # they were the same tournament -- tournament_name_to_slug() already
    # strips this same "ATP "/"WTA " prefix pattern, so checking for its
    # absence here is the same real signal, not a guess.
    import re as _re

    competition = row["competition"]
    # Strip a leading year (e.g. "2026 WTA Athens") before checking for the
    # ATP/WTA prefix -- without this, a year-prefixed competition string
    # would look "bare" by this check even though it already names its own
    # tour, producing a redundant "2026 WTA Athens (Women's)" label.
    competition_sans_year = _re.sub(r"^\d{4}\s+", "", competition)
    if not _re.match(r"^(ATP|WTA)\s", competition_sans_year, _re.IGNORECASE):
        gender_label = "Men's" if row["tour"] == "atp" else "Women's"
        competition = f"{competition} ({gender_label})"
    market.group_label = competition
    # `side` is otherwise unused for this market_type -- repurposed to carry
    # "atp"/"wta" (needed to pick the right tennisexplorer tour suffix,
    # atp-men vs wta-women, when fetching the real draw -- see
    # tennis_markets.py's futures endpoint), same "reuse an existing column
    # for a sport-specific meaning" pattern as MmaFight's fighter-name fields.
    market.side = row["tour"]
    market.status = row.get("status") or "active"
    session.flush()
    session.add(MarketSnapshot(
        market_id=market.id, ts=datetime.datetime.utcnow(),
        # KALSHI ROWS ARE ALREADY ORIENTED, so their quote is passed straight
        # through. quote_fields() is a POLYMARKET helper that reads raw_bid/
        # raw_ask and orients them against the row's own price -- and the Kalshi
        # tennis client never sets raw_bid/raw_ask at all, so calling it here
        # silently returned (None, None) and DISCARDED the bid/ask the client
        # had correctly read from yes_bid_dollars/yes_ask_dollars.
        #
        # WHAT THAT COST (found 2026-08-25). Every Kalshi tennis snapshot stored
        # a null book. The spread guard cannot fire on a MISSING book -- absence
        # is deliberately not treated as a wide spread -- so these rows fell
        # through to the volume gate, which passes on stale cumulative volume,
        # and were then priced against a stale last_price. Live result: 13 staked
        # US Open game_total bets with "edges" of +42 to +67pp against Kalshi
        # books that were actually 0.00 bid / 0.99 ask -- empty, zero open
        # interest, zero liquidity. Every other sport's Kalshi catalog already
        # passes the quote through directly; tennis was the only one that did not.
        yes_bid=row.get("yes_bid"),
        yes_ask=row.get("yes_ask"),
        last_price=row.get("last_price"), volume=row.get("volume"),
    ))
    return market
