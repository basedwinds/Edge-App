"""Live-polling Kalshi client for WNBA markets -- parallel to
kalshi_nba_client.py. Scope is moneyline only (KXWNBAGAME game-winner), the
one WNBA market with a real team-Elo baseline; spread/total/futures are
deferred (moneyline-only integration, see poller_wnba.py).

The All-Star game (e.g. KXWNBAGAME-26JUL25SPNCOO, "Team Coop"/"Team Spoon")
is left in the raw feed and naturally drops out downstream: its ticker blob
("SPNCOO") splits to non-real abbreviations, so the matcher returns no game id
and the Elo has no rating for those pseudo-teams -- no special-casing needed.
"""
from app.clients.base import get_json, paginate

BASE = "https://api.elections.kalshi.com/trade-api/v2"
MONEYLINE_SERIES = "KXWNBAGAME"


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
