"""Live-polling Kalshi client for Tennis moneyline markets. Parallel to
kalshi_mma_client.py -- same plain-binary-per-outcome shape (no team ladder/
floor_strike structure), fighter name replaced by player name.

Six real series confirmed live 2026-07-18 (25/19/7/4/7/7 open events
respectively that day): KXATPMATCH/KXWTAMATCH (tour-level), KXATPCHALLENGERMATCH/
KXWTACHALLENGERMATCH (Challenger), KXITFMATCH/KXITFWMATCH (ITF) -- Kalshi
splits ITF by gender into two series, unlike Polymarket's single combined
`itf` series_slug (see polymarket_tennis_client.py). Confirmed live catalog
scan (2026-07-18) also found real set-winner/game-spread/game-total/exact-
score series (KXATPSETWINNER/KXATPGSPREAD/KXATPGTOTAL/KXATPEXACTMATCH,
mirrored for WTA) with real open events -- NOT built in this pass (Phase 2
is moneyline only per the approved build plan; those are Phase 5/6).

Doubles series (KXATPDOUBLES/KXWTADOUBLES/KXITFDOUBLES/KXITFWDOUBLES) exist
but had ZERO open events on the day of the live audit -- deliberately not
built, same "thin/no current inventory" reasoning as this app's MMA futures.
"""
import re

from app.clients.base import get_json, paginate
from app.ingestion.market_matcher_tennis import kalshi_match_suffix, kalshi_set_winner_event_parts

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = {
    ("atp", "tour"): "KXATPMATCH",
    ("wta", "tour"): "KXWTAMATCH",
    ("atp", "challenger"): "KXATPCHALLENGERMATCH",
    ("wta", "challenger"): "KXWTACHALLENGERMATCH",
    ("atp", "itf"): "KXITFMATCH",
    ("wta", "itf"): "KXITFWMATCH",
}

# Confirmed live 2026-07-18: game spread/total are ATP-ONLY on Kalshi --
# KXWTAGSPREAD/KXWTAGTOTAL both 404, no WTA equivalent exists at all. Set
# winner and exact-match-score DO exist for both tours.
SET_WINNER_SERIES = {"atp": "KXATPSETWINNER", "wta": "KXWTASETWINNER"}
GAME_SPREAD_SERIES = "KXATPGSPREAD"
GAME_TOTAL_SERIES = "KXATPGTOTAL"
EXACT_MATCH_SERIES = "KXATPEXACTMATCH"

# Tournament-winner futures (confirmed live 2026-07-19): "Who wins THIS
# tournament" -- real, currently-open inventory on both tours, including a
# real Grand Slam ("US Open Men's/Women's Singles Winner", open ~2 months
# before the event even starts). Genuinely different shape from every other
# Tennis series in this app: one event per TOURNAMENT (not per match), one
# market per PLAYER in the field (not per player-pair) -- see
# bracket_sim_tennis.py for how this gets a real model_prob (a bracket Monte
# Carlo off the real scraped draw, not a moneyline-style Elo diff).
TOURNAMENT_WINNER_SERIES = {"atp": "KXATP", "wta": "KXWTA"}


def get_open_events(series_ticker: str) -> list[dict]:
    def url_builder(cursor):
        url = f"{BASE}/events?series_ticker={series_ticker}&status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        return url

    return paginate(url_builder, list_key="events", cursor_style="cursor")


def get_markets_for_event(event_ticker: str) -> list[dict]:
    d = get_json(f"{BASE}/markets?event_ticker={event_ticker}")
    return d.get("markets", [])


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_moneyline_markets() -> list[dict]:
    """One row per (event, player) across all 6 tour/tier combinations --
    real player name is in yes_sub_title (2 markets/event, one per player,
    same shape as every other real-fighter/player-name market in this app)."""
    rows = []
    for (tour, tier), series_ticker in MONEYLINE_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "event_title": ev.get("title", ""),
                    # Real tournament name, e.g. "Wimbledon Men Singles",
                    # "French Open Women Singles" -- confirmed live
                    # 2026-07-18 against settled KXATPMATCH/KXWTAMATCH
                    # events. Used to detect Grand Slam matches (see
                    # market_catalog_tennis.py::_infer_slam_attributes).
                    "competition": ev.get("product_metadata", {}).get("competition", ""),
                    "tour": tour,
                    "tier": tier,
                    "ticker": m["ticker"],
                    "player_name": m.get("yes_sub_title", ""),
                    # Real per-MATCH estimated start instant (confirmed live
                    # 2026-07-18 against a real market object) -- same field
                    # kalshi_mma_client.py already relies on for MMA's
                    # estimated_start_time, genuinely per-match here too (not
                    # a flat per-event value).
                    "estimated_start_time": m.get("occurrence_datetime"),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def _common_fields(m: dict) -> dict:
    return {
        "yes_bid": _to_float(m.get("yes_bid_dollars")),
        "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")),
        "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }


def get_set_winner_markets() -> list[dict]:
    """ONE EVENT PER SET (confirmed live, see market_matcher_tennis.py's
    kalshi_set_winner_event_parts docstring) -- returns one row per
    (match, set_number, player)."""
    rows = []
    for tour, series_ticker in SET_WINNER_SERIES.items():
        for ev in get_open_events(series_ticker):
            parts = kalshi_set_winner_event_parts(ev["event_ticker"])
            if parts is None:
                continue
            match_suffix, set_number = parts
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "match_suffix": match_suffix,
                    "set_number": set_number,
                    "tour": tour,
                    "ticker": m["ticker"],
                    "player_name": m.get("yes_sub_title", ""),
                    **_common_fields(m),
                })
    return rows


_GSPREAD_SUB_RE = re.compile(r"^(.+?)\s*[+-]([\d.]+)\s*games$", re.IGNORECASE)


def get_game_spread_markets() -> list[dict]:
    """ATP only (see module docstring). yes_sub_title is "{Player} -X.5
    games" (player must win by more than X.5 games) -- floor_strike gives
    the same line as a clean float, more reliable than parsing the text."""
    rows = []
    for ev in get_open_events(GAME_SPREAD_SERIES):
        match_suffix = kalshi_match_suffix(ev["event_ticker"])
        if match_suffix is None:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            sub = m.get("yes_sub_title", "")
            gspread_match = _GSPREAD_SUB_RE.match(sub)
            player_name = gspread_match.group(1).strip() if gspread_match else None
            rows.append({
                "event_ticker": ev["event_ticker"],
                "match_suffix": match_suffix,
                "ticker": m["ticker"],
                "player_name": player_name,
                "line": _to_float(m.get("floor_strike")),
                **_common_fields(m),
            })
    return rows


def get_game_total_markets() -> list[dict]:
    """ATP only. yes_sub_title is "Over X.5 games" (no player name -- game
    total is match-level, not per-player); title has both real player names
    embedded ("{A} vs {B}: Total Games"), used for match resolution."""
    rows = []
    for ev in get_open_events(GAME_TOTAL_SERIES):
        match_suffix = kalshi_match_suffix(ev["event_ticker"])
        if match_suffix is None:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append({
                "event_ticker": ev["event_ticker"],
                "match_suffix": match_suffix,
                "ticker": m["ticker"],
                "event_title": ev.get("title", ""),
                "line": _to_float(m.get("floor_strike")),
                **_common_fields(m),
            })
    return rows


_EXACT_SUB_RE = re.compile(r"^(.+?)\s+wins\s+(\d)-(\d)$", re.IGNORECASE)


def get_exact_match_markets() -> list[dict]:
    """ATP only. yes_sub_title is "{Player} wins X-Y" (set score) -- both
    the player name and the exact scoreline are directly readable from it,
    no need to decode the ticker's own ad-hoc suffix code."""
    rows = []
    for ev in get_open_events(EXACT_MATCH_SERIES):
        match_suffix = kalshi_match_suffix(ev["event_ticker"])
        if match_suffix is None:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            sub = m.get("yes_sub_title", "")
            exact_match = _EXACT_SUB_RE.match(sub)
            if not exact_match:
                continue
            rows.append({
                "event_ticker": ev["event_ticker"],
                "match_suffix": match_suffix,
                "ticker": m["ticker"],
                "player_name": exact_match.group(1).strip(),
                "player_sets": int(exact_match.group(2)),
                "opponent_sets": int(exact_match.group(3)),
                **_common_fields(m),
            })
    return rows


def get_tournament_winner_markets() -> list[dict]:
    """One row per (tournament, player-in-the-field). `competition` (e.g.
    "ATP Bastad", "2026 WTA Athens", "US Open") is the real tournament name,
    used both for display and to resolve the matching tennisexplorer draw
    page (see bracket_sim_tennis.py / market_catalog_tennis.py::
    tournament_name_to_slug) -- deliberately NOT derived from the event
    ticker's own suffix, which is inconsistent (confirmed live: Bastad is
    "KXATP-26BASTAD" but Athens is bare "KXATP-26", no city code at all)."""
    rows = []
    for tour, series_ticker in TOURNAMENT_WINNER_SERIES.items():
        for ev in get_open_events(series_ticker):
            competition = ev.get("product_metadata", {}).get("competition", "") or ev.get("title", "")
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "tour": tour,
                    "competition": competition,
                    "ticker": m["ticker"],
                    "player_name": m.get("yes_sub_title", ""),
                    **_common_fields(m),
                })
    return rows
