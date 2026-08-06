"""Polymarket racing client (F1 + NASCAR + IndyCar).

Kalshi doesn't quote racing markets this far out (they sit unpriced), but
Polymarket lists PRICED, liquid per-race markets right now -- confirmed live
2026-07-24 via tag_slug=f1/nascar:
  - "{Race}: Driver Winner"  -> one binary Yes/No market PER DRIVER
    (groupItemTitle = driver name, e.g. "Pierre Gasly"), gameStartTime = the
    real race instant. Maps to our race_winner model (racing_sim).
  - "{Race}: Driver Pole Position" -> same per-driver Yes/No shape -> pole
    (quali-Elo model).
Season "Drivers'/Constructors' Champion" events are a separate futures shape,
handled by fetch_polymarket_racing_futures and priced by the championship sim
(F1 + IndyCar only -- see that function on why NASCAR is excluded).

Yes-price is P(driver wins/poles), already a 0-1 probability, so it's stored as
last_price directly (the racing router's _implied_prob reads it as-is, same as
every other Polymarket sport).
"""
import logging

from app.clients.base import paginate
from app.clients.polymarket_client import extract_market_prices

log = logging.getLogger("polymarket_racing_client")

GAMMA = "https://gamma-api.polymarket.com"
# IndyCar was excluded here on the note "Polymarket has no IndyCar tag". That is
# NO LONGER TRUE (re-checked live 2026-08-06): tag_slug="indycar" returns 5 open
# events -- the season title plus Race Winner and Pole Position for each of the
# next two rounds, ~200 markets. ("indy-car" and "indy-500" really do return 0,
# which is probably how the original note was arrived at.)
#
# Nothing else needed changing: _classify already reads the event TITLE, and
# IndyCar's titles are the same "<Race>: Race Winner" / "<Race>: Pole Position"
# shape as F1's, so the existing parse + persist path handles them as-is. Worth
# re-testing a "platform doesn't have this" note before building around it.
_TAGS = {"f1": "f1", "nascar": "nascar", "irl": "indycar"}


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
    # A bare "<Series>: 2026 Champion" is the DRIVERS' title. F1 names both of
    # its titles explicitly, so the two checks above claim those first and this
    # can only be reached by a series that runs a single championship --
    # IndyCar's event is "NTT IndyCar Series: 2026 Champion", which returned None
    # here and left 46 markets unread.
    return "drivers_champion"


def fetch_polymarket_racing_futures() -> list[dict]:
    """One row per (season-title event, driver/constructor) for the Drivers'/
    Constructors' Champion markets. Same row shape as fetch_polymarket_racing so
    market_catalog_racing persists it uniformly; priced by the championship sim,
    not racing_sim.

    SCOPED TO THE SERIES THE CHAMPIONSHIP MODEL CAN ACTUALLY PRICE, which is
    exactly racing_championship.PRICED_SERIES.

    NASCAR was excluded here on purpose for a long time, and the reason was
    sound: its title is an elimination playoff ending in a winner-take-all
    Championship 4, so the cumulative-points sim would have been WRONG rather
    than merely unvalidated. It is included from 2026-08-06 because it finally
    has the right model behind it (racing_playoff_sim), not because the
    objection was dropped.
    """
    out: list[dict] = []
    for series, tag in (("f1", "f1"), ("irl", "indycar"), ("nascar", "nascar")):
        try:
            events = _events(tag)
        except Exception:
            log.exception("polymarket %s futures fetch failed", series)
            continue
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
                    "series": series,
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
