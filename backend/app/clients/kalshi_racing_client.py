"""Kalshi racing markets client (F1 / IndyCar / NASCAR). Fetches the per-race
market types our validated models price directly off the field (which Kalshi
itself supplies -- one market per driver, so the set of drivers with markets
under an event IS the race field, same trick as the esports tournament sim):

  race_winner  -> P(win)      (racing_sim)
  top_n(3/5/10)-> P(top-N)    (racing_sim)   incl. F1 podium = top 3
  pole         -> P(pole)     (quali Elo)

Season-champion, constructor, fastest-lap, H2H markets are left for later
(season needs a season Monte Carlo; fastest-lap is ~noise; H2H needs pairing).
Everything ships tracking-only (priced/shown, not staked) -- racing can't be
historically backtested (thin retention), so forward CLV is the only judge.
"""
import logging

from app.clients.base import get_json

log = logging.getLogger("kalshi_racing_client")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ticker -> (series, market_type, top_n line)
SERIES_MAP = {
    "KXF1RACE": ("f1", "race_winner", None),      # F1 race winner (headline market)
    "KXF1TOP5": ("f1", "top_n", 5),
    "KXF1RACEPODIUM": ("f1", "top_n", 3),
    "KXF1POLE": ("f1", "pole", None),
    "KXINDYCARRACE": ("irl", "race_winner", None),
    "KXINDYCARTOP3": ("irl", "top_n", 3),   # IndyCar podium
    "KXINDYCARTOP10": ("irl", "top_n", 10),
    # IndyCar drivers' title. Priced by the standings-aware season sim, NOT the
    # per-race model -- see racing_championship. Kalshi lists no IndyCar
    # constructors'/entrant title, so there is no constructors_champion here.
    "KXINDYCARSERIES": ("irl", "drivers_champion", None),
    "KXNASCARRACE": ("nascar", "race_winner", None),  # NASCAR race winner (headline market)
    "KXNASCARTOP3": ("nascar", "top_n", 3),
    "KXNASCARTOP5": ("nascar", "top_n", 5),
    "KXNASCARTOP10": ("nascar", "top_n", 10),
}


def _to_float(v):
    """Kalshi returns these as STRINGS ("0.0500"), not numbers. Previously this
    module read the plain "yes_bid"/"yes_ask"/"last_price"/"volume" keys, which
    do not exist on this endpoint -- every value came back None, so no
    conversion was needed and none existed. Reading the real *_dollars/*_fp keys
    means the strings now have to be coerced or they reach the DB as text."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_racing_markets() -> list[dict]:
    """One row per open driver-market across the tracked racing series. Carries
    the RAW Kalshi price fields (cents 0-100) so poller_racing can write
    MarketSnapshots; the router derives implied_prob from the snapshot the same
    way every other sport does. `driver` is the Kalshi yes_sub_title."""
    out: list[dict] = []
    for ticker, (series, mtype, line) in SERIES_MAP.items():
        try:
            data = get_json(f"{KALSHI_BASE}/markets?series_ticker={ticker}&status=open&limit=200")
        except Exception:
            log.exception("kalshi racing fetch failed for %s", ticker)
            continue
        for m in data.get("markets", []):
            driver = m.get("yes_sub_title") or ""
            if not driver:
                continue
            out.append({
                "series": series,
                "market_type": mtype,
                "line": line,
                "driver": driver,
                "event_ticker": m.get("event_ticker"),
                "event_title": m.get("title"),
                "ticker": m.get("ticker"),
                "close_time": m.get("close_time") or m.get("expiration_time"),
                "status": m.get("status") or "active",
                "last_price": _to_float(m.get("last_price_dollars")),
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "volume": _to_float(m.get("volume_fp")),
            })
    return out
