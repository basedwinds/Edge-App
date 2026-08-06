"""Polymarket WNBA client -- per-game moneyline.

WNBA was the last sport the health check flagged as "Polymarket lists this sport
but we ingest none of it": a whole platform's prices, cross-platform divergences
and CLV missing. Confirmed live 2026-08-06 under tag_slug="wnba": 53 open events,
43 of them per-game and 10 season futures.

MARKET SHAPE (measured, not assumed). Each game event carries exactly ONE binary
market -- the moneyline, with the two full team names as its outcomes. There is
no bundled spread/total the way NBA's regular-season events work, so this client
extracts moneyline only. Slug format is "wnba-{away}-{home}-{yyyy}-{mm}-{dd}",
e.g. "wnba-la-min-2026-08-06".

TEAM RESOLUTION GOES THROUGH THE FULL NAME, NOT THE SLUG CODE -- see
market_matcher_wnba.resolve_polymarket_team_name for why ("la" is the LA Sparks,
"las" is the Las Vegas Aces, and Polymarket ships "PortlandFire" without a space
in some events).

`tag_slug="womens-nba"` returns 0 events; "basketball" returns the same WNBA
events mixed in with NBA ones. "wnba" is the correct and complete tag.
"""
import logging

from app.clients.base import paginate
from app.clients.polymarket_client import GAMMA, extract_market_prices
from app.ingestion.market_matcher_wnba import resolve_polymarket_team_name

log = logging.getLogger("polymarket_wnba")

TAG_SLUG = "wnba"


def get_open_events(limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={TAG_SLUG}&closed=false&limit={limit}&offset={offset}"
    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def get_moneyline_markets() -> list[dict]:
    """One row per (game, team) with that team's own price.

    A row is emitted only when BOTH outcomes resolve to a known team. That is
    deliberate: the same listing contains season futures whose outcomes are
    "Yes"/"No", and a partial resolve would attach a real price to the wrong
    side. Unresolvable names are counted and logged rather than guessed.
    """
    rows: list[dict] = []
    unresolved: set[str] = set()
    for event in get_open_events():
        slug = event.get("slug") or ""
        markets = event.get("markets") or []
        if not markets:
            continue
        prices = extract_market_prices(markets[0])
        outcomes = prices["outcomes"]
        outcome_prices = prices["outcome_prices"]
        if len(outcomes) != 2 or len(outcome_prices) != 2:
            continue
        abbrs = [resolve_polymarket_team_name(o) for o in outcomes]
        if not all(abbrs):
            # Futures events ("Yes"/"No") land here and are correctly skipped;
            # a real team we failed to map is worth surfacing.
            for name, ab in zip(outcomes, abbrs):
                if ab is None and name not in ("Yes", "No"):
                    unresolved.add(name)
            continue
        for team_name, abbr, price in zip(outcomes, abbrs, outcome_prices):
            rows.append({
                "event_slug": slug,
                "event_title": event.get("title", ""),
                "team_full_name": team_name,
                "team_espn_abbr": abbr,
                "last_price": price,
                "condition_id": prices["condition_id"],
                "volume": prices["volume"],
                "raw_bid": prices["best_bid"],
                "raw_ask": prices["best_ask"],
                "game_start_time": markets[0].get("gameStartTime"),
            })
    if unresolved:
        log.warning("polymarket wnba: %d unmapped team name(s): %s",
                    len(unresolved), sorted(unresolved))
    return rows
