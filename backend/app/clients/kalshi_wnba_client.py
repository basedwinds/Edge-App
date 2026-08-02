"""Live-polling Kalshi client for WNBA markets -- parallel to
kalshi_nba_client.py. Scope is moneyline only (KXWNBAGAME game-winner), the
one WNBA market with a real team-Elo baseline; spread/total/futures are
deferred (moneyline-only integration, see poller_wnba.py).

The All-Star game (e.g. KXWNBAGAME-26JUL25SPNCOO, "Team Coop"/"Team Spoon")
is left in the raw feed and naturally drops out downstream: its ticker blob
("SPNCOO") splits to non-real abbreviations, so the matcher returns no game id
and the Elo has no rating for those pseudo-teams -- no special-casing needed.
"""
import re

from app.clients.base import get_json, paginate
from app.clients.kalshi_client import get_open_markets_for_series

BASE = "https://api.elections.kalshi.com/trade-api/v2"
MONEYLINE_SERIES = "KXWNBAGAME"
SPREAD_SERIES = "KXWNBASPREAD"
TOTAL_SERIES = "KXWNBATOTAL"
# Half markets. All six are live with real settled history (528/528/176/282/
# 698/658 settled 2026-08-02), priced by game_lines_wnba's measured half
# constants. The winner series carry no floor_strike (they are "which team wins
# the half", not a threshold), so they use the moneyline fetch shape rather than
# the ladder one.
HALF_WINNER_SERIES = {1: "KXWNBA1HWINNER", 2: "KXWNBA2HWINNER"}
HALF_SPREAD_SERIES = {1: "KXWNBA1HSPREAD", 2: "KXWNBA2HSPREAD"}
HALF_TOTAL_SERIES = {1: "KXWNBA1HTOTAL", 2: "KXWNBA2HTOTAL"}

# Spread market tickers glue the team code to a rung index with no separator
# (confirmed live 2026-08-02: "KXWNBASPREAD-26AUG03PHXCHI-PHX7" = Phoenix, and
# "...-CHI7" = Chicago, both on the same event). Splitting on the letter/digit
# boundary resolves the team without needing a WNBA abbreviation table, unlike
# the NBA client's prefix-matching approach.
_SPREAD_SUFFIX_RE = re.compile(r"^([A-Z]+)\d+$")


def get_open_events(series_ticker: str = MONEYLINE_SERIES) -> list[dict]:
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


def get_moneyline_markets(series: str = MONEYLINE_SERIES) -> list[dict]:
    """Flat list of dicts, one per team-side market -- same row shape as
    kalshi_nba_client.get_moneyline_markets so the catalog upsert mirrors."""
    events = get_open_events(series)
    rows = []
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "event_title": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "team_abbr_kalshi": m["ticker"].rsplit("-", 1)[-1],
                    "team_full_name": m.get("yes_sub_title", ""),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def _ladder_rows(series: str, with_team: bool) -> list[dict]:
    """Shared spread/total ladder fetch. Uses the BULK per-series markets call
    (see kalshi_client.get_open_markets_for_series) rather than one request per
    event -- the per-event pattern is what stalled the cs2 refresh with Kalshi
    429s (fixed 2026-08-02), so WNBA is built without inheriting it.

    Row shape mirrors kalshi_nba_client's ladder rows so the catalog upsert can
    follow the NBA one. `line` comes from floor_strike, which Kalshi populates
    directly on both series (confirmed live: spread "greater/6.5", total
    "greater/186.5")."""
    events = {ev["event_ticker"]: ev for ev in get_open_events(series)}
    rows = []
    for m in get_open_markets_for_series(series):
        ev_ticker = m.get("event_ticker")
        if ev_ticker not in events or m.get("floor_strike") is None:
            continue
        row = {
            "event_ticker": ev_ticker,
            "event_title": events[ev_ticker].get("title", ""),
            "ticker": m["ticker"],
            "line": float(m["floor_strike"]),
            "yes_bid": _to_float(m.get("yes_bid_dollars")),
            "yes_ask": _to_float(m.get("yes_ask_dollars")),
            "last_price": _to_float(m.get("last_price_dollars")),
            "volume": _to_float(m.get("volume_fp")),
            "status": m.get("status"),
        }
        if with_team:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            match = _SPREAD_SUFFIX_RE.match(suffix)
            if not match:
                continue  # unparseable team code -> skip rather than guess
            row["team_abbr_kalshi"] = match.group(1)
        rows.append(row)
    return rows


def get_spread_markets() -> list[dict]:
    """Per-TEAM ladder: "<Team> wins the game by over X.5 points?"."""
    return _ladder_rows(SPREAD_SERIES, with_team=True)


def get_total_markets() -> list[dict]:
    """Game-level ladder: "Over X.5 points scored" (no team side)."""
    return _ladder_rows(TOTAL_SERIES, with_team=False)


def get_half_winner_markets(half: int) -> list[dict]:
    """Per-TEAM: "which team wins the Nth half". Same shape as the game
    moneyline, so it reuses that fetch."""
    return get_moneyline_markets(HALF_WINNER_SERIES[half])


def get_half_spread_markets(half: int) -> list[dict]:
    """Per-TEAM ladder: "<Team> wins the Nth half by over X.5 points?"."""
    return _ladder_rows(HALF_SPREAD_SERIES[half], with_team=True)


def get_half_total_markets(half: int) -> list[dict]:
    """Game-level ladder: "Over X.5 points in the Nth half"."""
    return _ladder_rows(HALF_TOTAL_SERIES[half], with_team=False)
