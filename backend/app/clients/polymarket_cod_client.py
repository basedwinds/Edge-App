"""Live-polling Polymarket client for Call of Duty. Sibling of
polymarket_cs2_client.py, and much smaller because CoD's real inventory is.

THE TAG IS `call-of-duty`, AND ONLY THAT. Checked live 2026-08-09 against the
obvious alternatives, because the CS2 client exists largely to document having
picked the wrong tag once and concluded a whole sport had no head-to-head
inventory:

    call-of-duty          2 open events   <- the real match events
    cod                   0
    callofduty            0
    call-of-duty-league   0

WHAT AN EVENT CARRIES, confirmed live on "Call of Duty: OpTic Gaming vs
100 Thieves (BO7) - Esports World Cup Playoffs":

    [Match Winner]      outcomes = the two real team names -> series_winner
    [O/U 4.5 Games]     outcomes = ["Over","Under"]        -> series_total
    [O/U 5.5 Games]     ...
    [Game N Winner]     outcomes = the two team names      -> DELIBERATELY SKIPPED

GAME N WINNER IS NOT INGESTED, and that is a decision rather than an omission.
elo_cod.SeriesDistribution.prob_map_n_win_a returns the SAME probability for
every game in the series -- the model has no per-map view at all, exactly the
flaw already found and gated in the other three titles. Ingesting six rows per
event that can never be staked would be pure noise on the board. The other
titles carry theirs only because they were built before that gate existed.

Confirming it is not a lost opportunity: on the live OpTic/100T event the six
Game N Winner markets carried volumes of 852, 7.7, 15.0, and three at null --
against $34,398 on Match Winner. The liquidity is entirely in the match line.

BEST OF 7, WHICH THE CS2 PARSER WOULD HAVE REJECTED. That client's
parse_best_of only accepts (1, 3, 5); CoD's Esports World Cup series are BO7,
so a copied parser would have returned None for every event and left best_of
unknown -- which is a hard gate on pricing. This one accepts odd 1..9.

PRICES OF EXACTLY 0.5 WITH NULL VOLUME appear here (three of the six Game N
markets on that event). That is the seeded no-book pattern, not a real market.
Rows are carried through with their real volume so the shared has_real_trading
gate can refuse them downstream, rather than being filtered silently here --
the router explains a refused row, this layer would not.
"""
from __future__ import annotations

import datetime
import logging
import re

from app.clients.base import paginate
from app.clients.polymarket_client import extract_market_prices

log = logging.getLogger("polymarket_cod_client")

GAMMA = "https://gamma-api.polymarket.com"

COD_TAG_SLUGS = ("call-of-duty",)

_TOTAL_GAMES_RE = re.compile(r"^O/U\s+([\d.]+)\s+Games$", re.IGNORECASE)
# "Call of Duty: OpTic Gaming vs 100 Thieves (BO7) - Esports World Cup Playoffs"
_BEST_OF_RE = re.compile(r"\(BO(\d)\)", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_open_events(limit: int = 100) -> list[dict]:
    """Live CoD match events. end_date_min filters the months-dead events that
    Gamma still reports as active -- the same server-side gate the CS2 client
    documents, applied here for the same reason."""
    seen: dict[str, dict] = {}
    for tag_slug in COD_TAG_SLUGS:
        def url_builder(offset, ts=tag_slug):
            return (
                f"{GAMMA}/events?tag_slug={ts}&closed=false"
                f"&end_date_min={_now_iso()}&limit={limit}&offset={offset}"
            )

        for event in paginate(url_builder, list_key=None, limit=limit, cursor_style="offset"):
            slug = event.get("slug")
            if slug:
                seen[slug] = event
    return list(seen.values())


def _is_match_event(event: dict) -> bool:
    """POSITIVE selection on " vs ", never "not a futures event" -- the same
    discipline the CS2 client's docstring insists on. A CoD futures event
    (were one ever listed) would not match."""
    return " vs " in (event.get("title") or "")


def _market_status(m: dict) -> str:
    """An event's own closed=false does not guarantee every market inside it is
    open -- already a real bug in Tennis and MMA."""
    if m.get("closed") or not m.get("active", True):
        return "closed"
    return "active"


def _normalize_start_time(raw) -> str | None:
    if not raw:
        return None
    text = str(raw).strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "Z"
    return text


def _is_stale(start_iso: str | None) -> bool:
    if not start_iso:
        return False  # unknown -> keep it; the router still gates on its own clock
    try:
        dt = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return False
    return dt < datetime.datetime.utcnow() - datetime.timedelta(days=2)


def parse_best_of(event_title: str | None) -> int | None:
    """Series length straight off the title.

    Accepts any ODD 1..9, unlike the CS2 parser's (1, 3, 5) -- CoD's Esports
    World Cup series are BO7 and would every one have been rejected. best_of is
    a hard gate on building a series distribution at all, so a too-narrow
    whitelist here does not degrade pricing, it disables it."""
    if not event_title:
        return None
    m = _BEST_OF_RE.search(event_title)
    if not m:
        return None
    best_of = int(m.group(1))
    return best_of if best_of in (1, 3, 5, 7, 9) else None


def _base_row(event: dict, m: dict, prices: dict, **extra) -> dict:
    row = {
        "event_slug": event.get("slug", ""),
        "event_title": event.get("title", ""),
        "group_item_title": m.get("groupItemTitle"),
        "outcomes": prices["outcomes"],
        "outcome_prices": prices["outcome_prices"],
        "condition_id": prices["condition_id"],
        "volume": prices["volume"],
        "raw_bid": prices["best_bid"],
        "raw_ask": prices["best_ask"],
        "status": _market_status(m),
        "estimated_start_time": _normalize_start_time(m.get("gameStartTime")),
        "best_of": parse_best_of(event.get("title")),
    }
    row.update(extra)
    return row


def _iter_match_markets(pattern_check, events: list[dict] | None = None):
    """Live match events only, non-stale markets only. `events` is threaded
    through so one fetch can serve every getter."""
    for event in (get_open_events() if events is None else events):
        if not _is_match_event(event):
            continue
        for m in event.get("markets", []):
            git = (m.get("groupItemTitle") or "").strip()
            checked = pattern_check(git)
            if not checked:
                continue
            if _is_stale(_normalize_start_time(m.get("gameStartTime"))):
                continue
            yield event, m, extract_market_prices(m), checked


def _is_two_sided(prices: dict) -> bool:
    return len(prices["outcomes"]) == 2 and len(prices["outcome_prices"]) == 2


def get_match_winner_markets(events: list[dict] | None = None) -> list[dict]:
    """ONE ROW PER (event, team), carrying that team's own name and price --
    the same shape kalshi_cod_client emits, so the catalog can consume either
    uniformly. Returning one row per MARKET instead would leave the storage
    layer to work out which side it was looking at."""
    rows = []
    for event, m, prices, _ in _iter_match_markets(
        lambda git: git.lower() == "match winner", events
    ):
        if not _is_two_sided(prices):
            continue
        for team_name, price in zip(prices["outcomes"], prices["outcome_prices"]):
            rows.append(_base_row(event, m, prices, team_name=team_name, last_price=price))
    return rows


def get_total_maps_markets(events: list[dict] | None = None) -> list[dict]:
    """"O/U N.5 Games" -- total maps played. Outcomes are Over/Under, so the
    line comes from the label rather than from an outcome name."""
    rows = []
    for event, m, prices, match in _iter_match_markets(
        lambda git: _TOTAL_GAMES_RE.match(git), events
    ):
        if not _is_two_sided(prices):
            continue
        # Team-less, and stored on the "over" side -- the same convention every
        # other sport's totals use here.
        over_price = prices["outcome_prices"][0] if prices["outcomes"][0].lower() == "over" else prices["outcome_prices"][1]
        rows.append(_base_row(event, m, prices, line=float(match.group(1)), last_price=over_price))
    return rows


def get_all_markets() -> dict[str, list[dict]]:
    """One listing fetch, sliced into both market types -- the per-type getters
    would otherwise re-paginate the same events once each."""
    events = get_open_events()
    return {
        "match_winner": get_match_winner_markets(events),
        "total_maps": get_total_maps_markets(events),
    }
