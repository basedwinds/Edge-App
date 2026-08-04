"""Live-polling Polymarket client for NBA markets. Parallel to
polymarket_client.py (NFL).

Confirmed live 2026-07-16 via tag_slug=nba (26 open events) and
tag_slug=nba-summer-league (13 open events, real Summer League games):
  - Championship ("nba-2027-champion", 36 markets: 30 real teams + 5
    unactivated "Team A".."Team E" placeholders + "Other") and Eastern
    Conference champion are open. NO Western Conference champion counterpart
    exists yet on this platform (checked directly, not just absent from one
    page) -- a real, confirmed platform asymmetry vs. Kalshi (which has
    both), not a bug in this client.
  - No division-winner markets exist on Polymarket at all (Kalshi has all
    6) -- same asymmetry category.
  - "Team to Make Playoffs" / "Team to Make Play-In" (30 markets each) are
    open -- direct season_sim.py equivalents.
  - Heavy volume of free-agency/trade-destination/2K-cover-athlete markets,
    same "different kind of model" scoping as NFL's player-movement/
    novelty exclusions -- not built.
  - Regular-season per-game markets: 0 open (season starts October).
  - Summer League per-game markets ARE open (tag_slug=nba-summer-league),
    but structured as a SINGLE binary market per game (no separate bundled
    spread/total the way NFL's/NBA's regular-season bundle works) --
    confirmed live, not assumed.
"""
from app.clients.polymarket_client import extract_market_prices
from app.clients.base import paginate
from app.ingestion.market_matcher_nba import resolve_polymarket_team_name

GAMMA = "https://gamma-api.polymarket.com"

NBA_TAG_SLUG = "nba"
SUMMER_LEAGUE_TAG_SLUG = "nba-summer-league"

# Title patterns (matched case-sensitively against the real live titles) --
# NOT hardcoded exact slugs, unlike NFL's polymarket_client.py::
# FUTURES_EVENT_SLUGS. Confirmed live 2026-07-16 that 3 of these 4 events'
# slugs carry an unstable-looking numeric suffix (e.g.
# "nba-2027-eastern-conference-champion-20260624155838911") that isn't
# predictable/hardcodable the way NFL's clean slugs were -- discovered
# dynamically via the tag_slug=nba listing instead, same pattern as
# get_week1_qb_markets uses on the NFL side for its own unstable event set.
FUTURES_TITLE_PATTERNS = {
    "championship": "NBA: 2027 Champion",
    "conference_champion": "Eastern Conference Champion",  # no Western counterpart yet, see docstring
    "playoff_qualifier": "Team to Make Playoffs",
    "play_in_qualifier": "Team to Make Play-In",
}

_PLACEHOLDER_NAMES = {"Other", "Team A", "Team B", "Team C", "Team D", "Team E"}


def get_open_events(tag_slug: str, limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={tag_slug}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def get_futures_markets() -> list[dict]:
    """Returns a flat list of dicts, one per real-team futures sub-market --
    skips Polymarket's unactivated "Team A".."Team E" placeholder slots and
    the "Other" catch-all (neither resolves to a real team)."""
    events = get_open_events(NBA_TAG_SLUG)
    rows = []
    for kind, pattern in FUTURES_TITLE_PATTERNS.items():
        matches = [e for e in events if pattern in (e.get("title") or "")]
        for event in matches:
            slug = event.get("slug", "")
            group_label = event.get("title", "")
            for m in event.get("markets", []):
                team_name = m.get("groupItemTitle")
                if not team_name or team_name in _PLACEHOLDER_NAMES:
                    continue
                prices = extract_market_prices(m)
                outcomes = prices["outcomes"]
                outcome_prices = prices["outcome_prices"]
                if "Yes" not in outcomes or not outcome_prices:
                    continue
                yes_idx = outcomes.index("Yes")
                rows.append(
                    {
                        "market_kind": kind,
                        "event_slug": slug,
                        "group_label": group_label,
                        "team_full_name": team_name,
                        "team_espn_abbr": resolve_polymarket_team_name(team_name),
                        "yes_price": outcome_prices[yes_idx] if yes_idx < len(outcome_prices) else None,
                        "condition_id": prices["condition_id"],
                        "slug": prices["slug"],
                        "question": prices["question"],
                        "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"],
                    }
                )
    return rows


def get_summer_league_moneyline_markets() -> list[dict]:
    """Summer League per-game markets -- single binary market per game
    (confirmed live: no bundled spread/total the way the regular-season
    bundle works), title format "NBA Summer League: {Away} vs. {Home}",
    slug format "nbasl-{away}-{home}-{yyyy}-{mm}-{dd}" (see
    market_matcher_nba.py::parse_polymarket_slug)."""
    events = get_open_events(SUMMER_LEAGUE_TAG_SLUG)
    rows = []
    for event in events:
        slug = event.get("slug", "")
        markets = event.get("markets", [])
        if not markets:
            continue
        prices = extract_market_prices(markets[0])
        outcomes = prices["outcomes"]
        outcome_prices = prices["outcome_prices"]
        if len(outcomes) != 2 or len(outcome_prices) != 2:
            continue
        for team_name, price in zip(outcomes, outcome_prices):
            rows.append(
                {
                    "event_slug": slug,
                    "event_title": event.get("title", ""),
                    "team_full_name": team_name,
                    "team_espn_abbr": resolve_polymarket_team_name(team_name),
                    "last_price": price,
                    "condition_id": prices["condition_id"],
                    "volume": prices["volume"],
                        "raw_bid": prices["best_bid"],
                        "raw_ask": prices["best_ask"],
                }
            )
    return rows
