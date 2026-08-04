"""Live-polling Polymarket client for Valorant markets. Parallel to
polymarket_mma_client.py.

Confirmed live 2026-07-19 via tag_slug=valorant: match-level events bundle a
rich set of sibling markets (same "one event, many groupItemTitle-labeled
markets" shape as UFC), e.g. a real TEC Esports vs All Gamers event carried:
  - "Match Winner" -- outcomes are the two real team names (not ["Yes","No"])
  - "Map 1 Winner" / "Map 2 Winner" / ... -- per-map, same two-team-name shape
  - "O/U {N}.5 Games" -- total maps played ladder
  - "Map Handicap: {TEAM} ({+/-line}) vs {TEAM} ({+/-line})" -- map-margin
    spread, one market per line
Futures ("Valorant Team to Win an International in 2026", confirmed live:
$550k liquidity) are a SEPARATE event shape -- one event whose `markets`
array has one binary Yes/No market PER TEAM (groupItemTitle = team name),
same shape as this app's other sports' league_winner-style futures. See
market_catalog_valorant.py::upsert_polymarket_valorant_futures_row.
Distinguished from match-level events structurally (a futures event's own
title reads "Team to Win ..." with no "vs", never by hardcoding a specific
tournament's slug) -- same "structural distinction, not a per-event
hardcode" discipline as MLB/Soccer's league_winner detection.
"""
import re

from app.clients.base import get_json, paginate
from app.clients.polymarket_client import extract_market_prices, handicap_lines_in_outcome_order

GAMMA = "https://gamma-api.polymarket.com"

VALORANT_TAG_SLUG = "valorant"

_MAP_WINNER_RE = re.compile(r"^Map\s+(\d+)\s+Winner$", re.IGNORECASE)
_TOTAL_GAMES_RE = re.compile(r"^O/U\s+([\d.]+)\s+Games$", re.IGNORECASE)
_HANDICAP_RE = re.compile(r"^Map Handicap:\s*(.+?)\s*\(([+-][\d.]+)\)\s*vs\s*(.+?)\s*\(([+-][\d.]+)\)$", re.IGNORECASE)


def get_open_events(limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={VALORANT_TAG_SLUG}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def _is_futures_event(event: dict) -> bool:
    """See module docstring -- a futures event's title has no "vs" (it's a
    single team/tournament question, not two teams playing each other)."""
    title = (event.get("title") or "").lower()
    return "vs" not in title and "vs." not in title


def _market_status(m: dict) -> str:
    """Same real per-market-inside-an-open-event bug already fixed for
    Tennis/MMA (see polymarket_mma_client.py's own docstring) -- an event's
    own closed=false doesn't guarantee every market inside it is still open."""
    if m.get("closed") or not m.get("active", True):
        return "closed"
    return "active"


def _normalize_start_time(raw) -> str | None:
    """See polymarket_tennis_client.py's own version of this function --
    same Polymarket-wide `gameStartTime` format quirk (space separator, bare
    "+00" UTC offset, not reliably parsed as an instant by every JS Date
    implementation)."""
    if not raw:
        return None
    text = str(raw).strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "Z"
    return text


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
        # REAL BUG this fixes (user-reported 2026-07-20: esports recommended
        # bets missing a real match start time) -- this field was never
        # extracted at all despite Polymarket's own API carrying it on every
        # market here (confirmed live: same gameStartTime field Tennis/
        # Soccer's own Polymarket clients already use).
        "estimated_start_time": _normalize_start_time(m.get("gameStartTime")),
    }
    row.update(extra)
    return row


def get_match_winner_markets() -> list[dict]:
    """One row per (event, team) with that team's real name and price --
    same shape kalshi_valorant_client's map-winner rows use for team
    identity, so market_matcher_valorant can consume either uniformly."""
    rows = []
    for event in get_open_events():
        if _is_futures_event(event):
            continue
        for m in event.get("markets", []):
            if (m.get("groupItemTitle") or "").strip().lower() != "match winner":
                continue
            prices = extract_market_prices(m)
            if len(prices["outcomes"]) != 2 or len(prices["outcome_prices"]) != 2:
                continue
            for team_name, price in zip(prices["outcomes"], prices["outcome_prices"]):
                rows.append(_base_row(event, m, prices, team_name=team_name, last_price=price))
    return rows


def get_map_winner_markets() -> list[dict]:
    rows = []
    for event in get_open_events():
        if _is_futures_event(event):
            continue
        for m in event.get("markets", []):
            title_match = _MAP_WINNER_RE.match((m.get("groupItemTitle") or "").strip())
            if not title_match:
                continue
            prices = extract_market_prices(m)
            if len(prices["outcomes"]) != 2 or len(prices["outcome_prices"]) != 2:
                continue
            map_number = int(title_match.group(1))
            for team_name, price in zip(prices["outcomes"], prices["outcome_prices"]):
                rows.append(_base_row(event, m, prices, map_number=map_number, team_name=team_name, last_price=price))
    return rows


def get_total_maps_markets() -> list[dict]:
    """O/U {N}.5 Games ladder -- team-less, game-level, same "side='over'"
    convention as this app's other sports' totals."""
    rows = []
    for event in get_open_events():
        if _is_futures_event(event):
            continue
        for m in event.get("markets", []):
            line_match = _TOTAL_GAMES_RE.match((m.get("groupItemTitle") or "").strip())
            if not line_match:
                continue
            prices = extract_market_prices(m)
            rows.append(_base_row(event, m, prices, line=float(line_match.group(1))))
    return rows


def get_map_handicap_markets() -> list[dict]:
    """"Map Handicap: AG (-1.5) vs TEC (+1.5)" -- one market covers BOTH
    sides of the same line (outcomes are the two team names, not Yes/No),
    same two-outcome shape as match/map winner. Returns one row per
    (event, team) with that team's own handicap line."""
    rows = []
    for event in get_open_events():
        if _is_futures_event(event):
            continue
        for m in event.get("markets", []):
            title = (m.get("groupItemTitle") or "").strip()
            handicap_match = _HANDICAP_RE.match(title)
            if not handicap_match:
                continue
            prices = extract_market_prices(m)
            if len(prices["outcomes"]) != 2 or len(prices["outcome_prices"]) != 2:
                continue
            # REAL BUG fixed 2026-08-02: the old code looked each line up by
            # exact team name, but the handicap TITLE abbreviates ("AG",
            # "NS", "ZETA") while `outcomes` spells names out ("All Gamers",
            # "Nongshim RedForce") -- so it silently dropped 16 of 17 real
            # open Valorant handicap markets (94%). See
            # polymarket_client.handicap_lines_in_outcome_order's own
            # docstring for the measurements and the positional-fallback
            # safety argument.
            lines = handicap_lines_in_outcome_order(handicap_match, prices["outcomes"])
            if lines is None:
                continue
            for team_name, price, line in zip(prices["outcomes"], prices["outcome_prices"], lines):
                rows.append(_base_row(event, m, prices, team_name=team_name, line=line, last_price=price))
    return rows


def get_futures_markets() -> list[dict]:
    """"Valorant Team to Win an International in 2026" -- team-less-of-a-
    single-match futures shape, see module docstring."""
    rows = []
    for event in get_open_events():
        if not _is_futures_event(event):
            continue
        for m in event.get("markets", []):
            team_name = m.get("groupItemTitle")
            if not team_name:
                continue
            prices = extract_market_prices(m)
            if prices["outcomes"] != ["Yes", "No"] or not prices["outcome_prices"]:
                continue
            rows.append(_base_row(
                event, m, prices,
                team_name=team_name, group_label=event.get("title", ""),
                yes_price=prices["outcome_prices"][0],
            ))
    return rows
