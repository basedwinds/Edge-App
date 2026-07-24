"""Live-polling Polymarket client for League of Legends. Parallel to
polymarket_valorant_client.py (LoL's Polymarket events use the same
"one event, many groupItemTitle-labeled markets" shape).

Confirmed live 2026-07-24 via tag_slug=league-of-legends: a match event (title
has "vs") bundles:
  - the SERIES winner -- git is the matchup itself ("TeamA vs TeamB - Event"),
    outcomes are the two real team names (NOT ["Yes","No"]);
  - "Game N Winner" -- per-game, same two-team-name shape -> map_winner;
  - "O/U N.5" -- games-played total (outcomes Over/Under) -> series_total;
  - "Game Handicap: {TEAM} ({+/-line}) vs {TEAM} ({+/-line})" -> series_handicap.
    ("Series: … Inhibitor Handicap" is a different, niche market -- skipped.)
Futures events (title has NO "vs", e.g. "LCK 2026 Season Winner") carry one
binary Yes/No market PER TEAM (git = team) -> tournament_winner.

Stale guard: LoL's Polymarket listing includes long-past events still marked
open, so match rows whose gameStartTime is >2 days in the past are dropped (the
ticker-date market cleanup can't reach Polymarket's non-dated tickers).
"""
import datetime
import re

from app.clients.base import paginate
from app.clients.polymarket_client import extract_market_prices

GAMMA = "https://gamma-api.polymarket.com"
LOL_TAG_SLUG = "league-of-legends"

# Only the market types our LoL model actually prices. Everything else in a LoL
# event (First Blood, Total Kills O/U, Baron/Dragon, Penta Kill, Odd/Even…) is a
# novelty prop we don't model and must NOT be mistaken for a series/game bet.
_GAME_WINNER_RE = re.compile(r"^Game\s+(\d+)\s+Winner$", re.IGNORECASE)
_TOTAL_RE = re.compile(r"^O/U\s+([\d.]+)\s+Games$", re.IGNORECASE)  # games-played total, e.g. "O/U 2.5 Games"
_HANDICAP_RE = re.compile(r"^Game Handicap:\s*(.+?)\s*\(([+-][\d.]+)\)\s*vs\s*(.+?)\s*\(([+-][\d.]+)\)$", re.IGNORECASE)


def get_open_events(limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={LOL_TAG_SLUG}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def _is_futures_event(event: dict) -> bool:
    title = (event.get("title") or "").lower()
    return " vs" not in title


def _market_status(m: dict) -> str:
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
        return False  # no time -> can't tell; keep it
    try:
        dt = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return False
    return dt < datetime.datetime.utcnow() - datetime.timedelta(days=2)


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
        "estimated_start_time": _normalize_start_time(m.get("gameStartTime")),
    }
    row.update(extra)
    return row


def get_series_winner_markets() -> list[dict]:
    """One row per (event, team). Series winner is the market explicitly labeled
    "Match Winner" (outcomes = the two team names), same as Valorant."""
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
            if _is_stale(_normalize_start_time(m.get("gameStartTime"))):
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
            git = (m.get("groupItemTitle") or "").strip()
            mt = _GAME_WINNER_RE.match(git)
            if not mt:
                continue
            prices = extract_market_prices(m)
            if len(prices["outcomes"]) != 2 or len(prices["outcome_prices"]) != 2:
                continue
            if _is_stale(_normalize_start_time(m.get("gameStartTime"))):
                continue
            for team_name, price in zip(prices["outcomes"], prices["outcome_prices"]):
                rows.append(_base_row(event, m, prices, map_number=int(mt.group(1)), team_name=team_name, last_price=price))
    return rows


def get_total_maps_markets() -> list[dict]:
    rows = []
    for event in get_open_events():
        if _is_futures_event(event):
            continue
        for m in event.get("markets", []):
            git = (m.get("groupItemTitle") or "").strip()
            mt = _TOTAL_RE.match(git)
            if not mt:
                continue
            if _is_stale(_normalize_start_time(m.get("gameStartTime"))):
                continue
            prices = extract_market_prices(m)
            rows.append(_base_row(event, m, prices, line=float(mt.group(1))))
    return rows


def get_map_handicap_markets() -> list[dict]:
    rows = []
    for event in get_open_events():
        if _is_futures_event(event):
            continue
        for m in event.get("markets", []):
            git = (m.get("groupItemTitle") or "").strip()
            hm = _HANDICAP_RE.match(git)
            if not hm:
                continue
            if _is_stale(_normalize_start_time(m.get("gameStartTime"))):
                continue
            team_1, line_1, team_2, line_2 = hm.groups()
            prices = extract_market_prices(m)
            if len(prices["outcomes"]) != 2 or len(prices["outcome_prices"]) != 2:
                continue
            lines_by_team = {team_1: float(line_1), team_2: float(line_2)}
            for team_name, price in zip(prices["outcomes"], prices["outcome_prices"]):
                line = lines_by_team.get(team_name)
                if line is None:
                    continue
                rows.append(_base_row(event, m, prices, team_name=team_name, line=line, last_price=price))
    return rows


def get_futures_markets() -> list[dict]:
    """"LCK 2026 Season Winner" etc. -- one Yes/No market per team (git=team)."""
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
