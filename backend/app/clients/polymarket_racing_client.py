"""Polymarket racing client (F1 + NASCAR).

Kalshi doesn't quote racing markets this far out (they sit unpriced), but
Polymarket lists PRICED, liquid per-race markets right now -- confirmed live
2026-07-24 via tag_slug=f1/nascar:
  - "{Race}: Driver Winner"  -> one binary Yes/No market PER DRIVER
    (groupItemTitle = driver name, e.g. "Pierre Gasly"), gameStartTime = the
    real race instant. Maps to our race_winner model (racing_sim).
  - "{Race}: Driver Pole Position" -> same per-driver Yes/No shape -> pole
    (quali-Elo model).
Season "Drivers'/Constructors' Champion" events (huge liquidity) are a separate
futures shape our model doesn't price yet -- skipped here.

Yes-price is P(driver wins/poles), already a 0-1 probability, so it's stored as
last_price directly (the racing router's _implied_prob reads it as-is, same as
every other Polymarket sport).
"""
import logging

from app.clients.base import paginate
from app.clients.polymarket_client import extract_market_prices

log = logging.getLogger("polymarket_racing_client")

GAMMA = "https://gamma-api.polymarket.com"
_TAGS = {"f1": "f1", "nascar": "nascar"}  # Polymarket has no IndyCar tag


def _events(tag: str, limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={tag}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def _norm_start(raw) -> str | None:
    """Polymarket's gameStartTime: space separator + bare "+00" UTC offset."""
    if not raw:
        return None
    t = str(raw).strip().replace(" ", "T", 1)
    if t.endswith("+00"):
        t = t[:-3] + "Z"
    return t


def _market_type_for(event_title: str) -> str | None:
    """race_winner / pole from the event title; None for season futures
    (Champion/Constructor) and anything we don't model."""
    t = event_title.lower()
    if "champion" in t or "constructor" in t:
        return None
    if "pole" in t:
        return "pole"
    if "winner" in t:
        return "race_winner"
    return None


def fetch_polymarket_racing() -> list[dict]:
    """One row per (race event, driver) for the Winner + Pole markets, shaped
    like kalshi_racing_client's rows so market_catalog_racing can persist either
    uniformly (source tags them apart)."""
    out: list[dict] = []
    for series, tag in _TAGS.items():
        try:
            events = _events(tag)
        except Exception:
            log.exception("polymarket racing fetch failed for tag %s", tag)
            continue
        for e in events:
            mtype = _market_type_for(e.get("title", ""))
            if not mtype:
                continue
            for m in e.get("markets", []):
                driver = (m.get("groupItemTitle") or "").strip()
                if not driver:
                    continue
                p = extract_market_prices(m)
                if p["outcomes"] != ["Yes", "No"] or not p["outcome_prices"]:
                    continue
                out.append({
                    "series": series,
                    "market_type": mtype,
                    "line": None,
                    "driver": driver,
                    "event_ticker": e.get("slug", ""),
                    "event_title": e.get("title", ""),
                    "ticker": p["condition_id"] or m.get("slug"),
                    "close_time": _norm_start(m.get("gameStartTime")),
                    "status": "closed" if (m.get("closed") or not m.get("active", True)) else "active",
                    "last_price": p["outcome_prices"][0],  # P(Yes) = P(driver wins/poles), 0-1
                    "yes_bid": None,
                    "yes_ask": None,
                    "volume": p["volume"],
                    "source": "polymarket",
                })
    return out
