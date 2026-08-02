"""Kalshi college-football markets client.

Only KXNCAAFGAME (per-game moneyline) is fetched. KXNCAAFSPREAD and
KXNCAAFTOTAL exist as series but had ZERO open markets when checked
2026-08-02 -- they list closer to kickoff. They're declared below so that
adding them later is a one-line change, and market_matcher_cfb's event-ticker
regex is already permissive enough to parse them.

Uses the bulk /markets?series_ticker=... endpoint rather than
get_open_events + get_markets_for_event per event. That N+1 pattern is what
caused the Kalshi 429s that stalled CS2 for 189 hours; one call returns every
open market in the series.
"""
import logging

from app.clients.base import get_json

log = logging.getLogger("kalshi_cfb_client")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = "KXNCAAFGAME"
# Present but empty as of 2026-08-02 -- see module docstring.
SPREAD_SERIES = "KXNCAAFSPREAD"
TOTAL_SERIES = "KXNCAAFTOTAL"


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def get_moneyline_markets() -> list[dict]:
    """One row per (game, team). `team_abbr_kalshi` is the ticker's own suffix
    and `display_name` is yes_sub_title -- market_matcher_cfb.resolve_team needs
    BOTH, since a 130-team sport can't be covered by an alias table alone and the
    display name is the fallback."""
    try:
        data = get_json(f"{KALSHI_BASE}/markets?series_ticker={MONEYLINE_SERIES}&status=open&limit=1000")
    except Exception:
        log.exception("kalshi cfb moneyline fetch failed")
        return []
    out = []
    for m in data.get("markets", []):
        ticker = m.get("ticker") or ""
        if not ticker or not m.get("event_ticker"):
            continue
        out.append({
            "ticker": ticker,
            "event_ticker": m["event_ticker"],
            "team_abbr_kalshi": ticker.rsplit("-", 1)[-1] if "-" in ticker else None,
            "display_name": m.get("yes_sub_title"),
            "close_time": m.get("close_time") or m.get("expiration_time"),
            "status": m.get("status") or "active",
            "last_price": _to_float(m.get("last_price")),
            "yes_bid": _to_float(m.get("yes_bid")),
            "yes_ask": _to_float(m.get("yes_ask")),
            "volume": _to_float(m.get("volume")),
        })
    return out
