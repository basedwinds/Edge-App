"""Live-polling Polymarket client for UFC markets. Parallel to
polymarket_mlb_client.py.

Confirmed live 2026-07-17 via series_slug=ufc (24 open events, same card as
Kalshi's KXUFCFIGHT family): each event bundles a rich set of per-fight
markets as sibling `markets` entries (not separate events) --
  - Moneyline: outcomes are the two real fighter names (NOT ["Yes","No"] the
    way every other market on this platform/sport uses) -- identified by
    outcome shape, not groupItemTitle, since (unlike MLB's base moneyline
    market) this one's groupItemTitle is NON-null ("Fighter A vs. Fighter
    B"), a real structural difference from every other sport's Polymarket
    client in this app.
  - "Fight to Go the Distance?" -- binary, fight-level.
  - "Fight won by KO/TKO?" / "Fight won by submission?" -- binary,
    fight-level method markets.
  - "{Fighter} to win by KO/TKO?" -- binary, PER-FIGHTER method market (no
    per-fighter submission-only market exists on this platform, confirmed).
  - "O/U {N}.5 Rounds" -- ladder, one rung per half-integer line, more rungs
    on a 5-round (title/main-event) fight than a 3-round undercard fight
    (confirmed: 0.5-4.5 vs 0.5-2.5).
No method-of-finish-only (fight-level KO vs Sub vs Decision, matching
Kalshi's KXUFCMOF) or round-of-victory-grid equivalent exists on this
platform -- gracefully absent, same "poller tolerates one platform missing a
market type the other has" pattern as every other sport in this app.

Futures deliberately NOT built yet, same reasoning as kalshi_mma_client.py.
"""
import re

from app.clients.base import get_json, paginate
from app.clients.polymarket_client import extract_market_prices

GAMMA = "https://gamma-api.polymarket.com"

UFC_SERIES_SLUG = "ufc"

_ROUNDS_LINE_RE = re.compile(r"O/U\s+([\d.]+)\s+Rounds", re.IGNORECASE)
_PER_FIGHTER_KOTKO_RE = re.compile(r"^(.+?)\s+to win by KO/TKO\?$", re.IGNORECASE)


def get_open_events(limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?series_slug={UFC_SERIES_SLUG}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def _market_status(m: dict) -> str:
    """REAL BUG this fixes (caught live 2026-07-19, user-reported bogus
    near-0% UFC prices): the EVENT-level `closed` flag (what get_open_events
    already filters on via closed=false) does NOT guarantee every market
    INSIDE that event bundle is still open -- same root cause already found
    and fixed for polymarket_tennis_client.py. A resolved fight's market can
    sit at closed=true individually while its event container stays open.
    Kalshi already exposes this correctly per-market (`status`, e.g.
    "finalized" -- confirmed live against the exact fight that triggered
    this investigation); this is Polymarket's equivalent signal."""
    if m.get("closed") or not m.get("active", True):
        return "closed"
    return "active"


def _base_row(event: dict, m: dict, prices: dict, **extra) -> dict:
    row = {
        "event_slug": event.get("slug", ""),
        "event_title": event.get("title", ""),
        "group_item_title": m.get("groupItemTitle"),
        "outcomes": prices["outcomes"],
        "outcome_prices": prices["outcome_prices"],
        "condition_id": prices["condition_id"],
        "volume": prices["volume"],
        "status": _market_status(m),
    }
    row.update(extra)
    return row


def get_moneyline_markets() -> list[dict]:
    """Returns one row per (event, fighter) with that fighter's real name
    and price -- same shape as kalshi_mma_client's moneyline rows, so
    market_matcher_mma.match_fight can consume either uniformly."""
    rows = []
    for event in get_open_events():
        for m in event.get("markets", []):
            prices = extract_market_prices(m)
            outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
            if len(outcomes) != 2 or outcomes == ["Yes", "No"] or outcomes == ["Over", "Under"]:
                continue
            if len(outcome_prices) != 2:
                continue
            for fighter_name, price in zip(outcomes, outcome_prices):
                rows.append(_base_row(
                    event, m, prices,
                    fighter_name=fighter_name,
                    last_price=price,
                ))
    return rows


def get_distance_markets() -> list[dict]:
    rows = []
    for event in get_open_events():
        for m in event.get("markets", []):
            if m.get("groupItemTitle") != "Fight to Go the Distance?":
                continue
            prices = extract_market_prices(m)
            rows.append(_base_row(event, m, prices))
    return rows


def _resolve_full_fighter_name(partial: str, event: dict) -> str:
    """REAL BUG fixed here (caught via live testing, not assumed): the
    per-fighter KO/TKO market's groupItemTitle only carries a partial name
    ("Usman", not "Kamaru Usman" -- confirmed live 2026-07-17), which won't
    match ufcstats' full names in market_matcher_mma. Resolves it against
    THIS SAME event's own moneyline outcomes (the two real full names) by
    suffix match -- cheap and reliable since an event only ever has 2
    fighters."""
    for m in event.get("markets", []):
        prices = extract_market_prices(m)
        outcomes = prices["outcomes"]
        if len(outcomes) != 2 or outcomes in (["Yes", "No"], ["Over", "Under"]):
            continue
        for full_name in outcomes:
            if full_name.lower().endswith(partial.lower()):
                return full_name
    return partial  # no moneyline match found -- fall back to the partial name rather than dropping the row


def get_method_markets() -> list[dict]:
    """Fight-level KO/TKO + submission, and per-fighter KO/TKO -- returns
    all three kinds in one list, distinguished by `method_kind`
    ("fight_kotko" | "fight_submission" | "fighter_kotko") and, for the
    per-fighter kind, `fighter_name` resolved to the fighter's FULL name
    (see _resolve_full_fighter_name)."""
    rows = []
    for event in get_open_events():
        for m in event.get("markets", []):
            title = m.get("groupItemTitle") or ""
            prices = extract_market_prices(m)
            if title == "Fight won by KO/TKO?":
                rows.append(_base_row(event, m, prices, method_kind="fight_kotko"))
            elif title == "Fight won by submission?":
                rows.append(_base_row(event, m, prices, method_kind="fight_submission"))
            else:
                fighter_match = _PER_FIGHTER_KOTKO_RE.match(title)
                if fighter_match:
                    rows.append(_base_row(
                        event, m, prices,
                        method_kind="fighter_kotko",
                        fighter_name=_resolve_full_fighter_name(fighter_match.group(1), event),
                    ))
    return rows


def get_rounds_markets() -> list[dict]:
    """O/U {N}.5 Rounds ladder -- multiple lines per fight (more on 5-round
    fights than 3-round ones)."""
    rows = []
    for event in get_open_events():
        for m in event.get("markets", []):
            title = m.get("groupItemTitle") or ""
            line_match = _ROUNDS_LINE_RE.match(title)
            if not line_match:
                continue
            prices = extract_market_prices(m)
            rows.append(_base_row(event, m, prices, line=float(line_match.group(1))))
    return rows
