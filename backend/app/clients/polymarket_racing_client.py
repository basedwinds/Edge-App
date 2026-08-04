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


def _classify(event_title: str) -> "tuple[str | None, int | None]":
    """(market_type, line) from a per-race event title. None -> not modelled:
      * champion    -> season futures (fetch_polymarket_racing_futures)
      * fastest lap -> near-random (late free-stop on fresh tyres), skip
      * practice    -> low signal (sandbagging / tyre programmes), skip
      * scores 1st  -> a constructor combined-points question we don't model yet
    Modelled: podium->top_n(3), head-to-head->h2h, constructor pole, pole, winner."""
    t = event_title.lower()
    if "champion" in t:
        return None, None
    if "fastest lap" in t or "practice" in t:
        return None, None
    if "scores 1st" in t or "scores first" in t:
        return None, None
    if "podium" in t:
        return "top_n", 3
    if "head-to-head" in t or "head to head" in t:
        return "h2h", None
    if "constructor" in t and "pole" in t:
        return "constructor_pole", None
    if "pole" in t:
        return "pole", None
    if "winner" in t:
        return "race_winner", None
    return None, None


def _futures_type_for(event_title: str) -> str | None:
    """drivers_champion / constructors_champion from a season-title event."""
    t = event_title.lower()
    if "champion" not in t:
        return None
    if "constructor" in t:
        return "constructors_champion"
    if "driver" in t:
        return "drivers_champion"
    return None


def fetch_polymarket_racing_futures() -> list[dict]:
    """One row per (season-title event, driver/constructor) for the Drivers'/
    Constructors' Champion markets. Same row shape as fetch_polymarket_racing so
    market_catalog_racing persists it uniformly; priced by the championship sim,
    not racing_sim. F1 only (cumulative-points title); NASCAR's playoff title
    isn't a points question so it's left unpriced."""
    out: list[dict] = []
    try:
        events = _events("f1")
    except Exception:
        log.exception("polymarket f1 futures fetch failed")
        return out
    for e in events:
        ftype = _futures_type_for(e.get("title", ""))
        if not ftype:
            continue
        for m in e.get("markets", []):
            name = (m.get("groupItemTitle") or "").strip()  # driver or constructor
            if not name:
                continue
            p = extract_market_prices(m)
            if p["outcomes"] != ["Yes", "No"] or not p["outcome_prices"]:
                continue
            out.append({
                "series": "f1",
                "market_type": ftype,
                "line": None,
                "driver": name,  # constructor name for constructors_champion
                "event_ticker": e.get("slug", ""),
                "event_title": e.get("title", ""),
                "ticker": p["condition_id"] or m.get("slug"),
                "close_time": _norm_start(m.get("gameStartTime")),
                "status": "closed" if (m.get("closed") or not m.get("active", True)) else "active",
                "last_price": p["outcome_prices"][0],  # P(Yes) = P(champion), 0-1
                "yes_bid": None,
                "yes_ask": None,
                "volume": p["volume"],
                        "raw_bid": p["best_bid"],
                        "raw_ask": p["best_ask"],
                "source": "polymarket",
            })
    return out


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
            mtype, line = _classify(e.get("title", ""))
            if not mtype:
                continue
            for m in e.get("markets", []):
                git = (m.get("groupItemTitle") or "").strip()
                if not git:
                    continue
                p = extract_market_prices(m)
                if not p["outcome_prices"]:
                    continue
                driver = git  # driver / constructor label
                if mtype == "h2h":
                    # 2-way market: outcomes ARE the two driver surnames. Build the
                    # "A vs B" label FROM outcomes order (Polymarket's outcomes are
                    # NOT always in groupItemTitle order) so last_price = P(A) stays
                    # aligned with the first-named driver -- otherwise the model
                    # prices the wrong side and every h2h shows a phantom ~20pp edge.
                    outs = p["outcomes"]
                    if len(outs) != 2 or len(p["outcome_prices"]) != 2:
                        continue
                    driver = f"{outs[0]} vs {outs[1]}"
                elif p["outcomes"] != ["Yes", "No"]:
                    continue  # per-driver/constructor Yes/No markets only
                out.append({
                    "series": series,
                    "market_type": mtype,
                    "line": line,
                    "driver": driver,  # driver / constructor / "A vs B" for h2h
                    "event_ticker": e.get("slug", ""),
                    "event_title": e.get("title", ""),
                    "ticker": p["condition_id"] or m.get("slug"),
                    "close_time": _norm_start(m.get("gameStartTime")),
                    "status": "closed" if (m.get("closed") or not m.get("active", True)) else "active",
                    "last_price": p["outcome_prices"][0],  # P(Yes) / P(first driver higher)
                    "yes_bid": None,
                    "yes_ask": None,
                    "volume": p["volume"],
                        "raw_bid": p["best_bid"],
                        "raw_ask": p["best_ask"],
                    "source": "polymarket",
                })
    return out
