"""Live-polling Kalshi client for UFC markets. Parallel to
kalshi_mlb_client.py, but the per-fight market shape is genuinely different
from every other sport in this app -- no team ladder/floor_strike structure
at all, every outcome is its own plain binary market with a real fighter
name (or method/round label) in yes_sub_title.

Series tickers confirmed live 2026-07-17 (UFC Fight Night: Du Plessis vs.
Usman card, 2026-07-18, 12 fights, all 6 series populated for every fight):
  KXUFCFIGHT     - moneyline, one binary market per fighter (2/fight)
  KXUFCDISTANCE  - "fight goes the distance", ONE binary market per fight
  KXUFCMOV       - method of victory, 7-way (fighter x {Decision, KO/TKO/DQ,
                   Submission} + a single Draw/No-Contest market)
  KXUFCMOF       - method of FINISH (fight-level, not per-fighter), 4-way
                   (KO/TKO/DQ, Submission, Decision, Draw/No Contest) --
                   confirmed a genuinely separate series from KXUFCMOV, not
                   a duplicate (that series ticker distinction was flagged
                   as "relationship not yet checked" by an earlier, separate
                   research project -- now confirmed live: MOV is
                   fighter x method, MOF is method only).
  KXUFCROUNDS    - "will the fight end before round N?" for N in 2..5, a
                   MONOTONIC ladder (these are NOT mutually exclusive/
                   independent -- "ends before round 3" implies "ends
                   before round 4"). Fewer scheduled rounds (3-round fights)
                   only list N=2,3.
  KXUFCVICROUND  - fighter x round-of-victory (1-5) grid PLUS a single
                   "OTHER" (decision/draw/NC) bucket -- 11-way for a 5-round
                   fight, 7-way for a 3-round fight.

Every series shares the SAME event-ticker SUFFIX for a given fight (e.g.
"26JUL18DUUSM") -- see market_matcher_mma.py::kalshi_fight_suffix. Real
fighter names are read from yes_sub_title (KXUFCFIGHT) rather than decoded
from that suffix, which uses an ad-hoc abbreviation scheme not worth
reverse-engineering.

Kalshi keeps these markets open THROUGH the live fight itself (confirmed via
a real market's early_close_condition: "closes after a champion is
declared"), not at a normal pre-fight closing time -- a real quirk an
earlier, separate research project on this exact platform already found and
had to work around for backtesting (define favorite/underdog from a 24h-out
snapshot, not closing price). Worth remembering once this app's own
backtest/CLV logic touches MMA fight-tied markets.

Futures (KXUFCTITLE + 8 weight-class title series) deliberately NOT built
yet -- user asked to build MMA futures last/low-priority, see Phase 4.
"""
from app.clients.base import get_json, paginate, markets_for_event
from app.ingestion.market_matcher_mma import kalshi_fight_suffix

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = "KXUFCFIGHT"
DISTANCE_SERIES = "KXUFCDISTANCE"
METHOD_OF_VICTORY_SERIES = "KXUFCMOV"
METHOD_OF_FINISH_SERIES = "KXUFCMOF"
ROUNDS_SERIES = "KXUFCROUNDS"
ROUND_OF_VICTORY_SERIES = "KXUFCVICROUND"


def get_open_events(series_ticker: str) -> list[dict]:
    def url_builder(cursor):
        url = f"{BASE}/events?series_ticker={series_ticker}&status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        return url

    return paginate(url_builder, list_key="events", cursor_style="cursor")


def get_markets_for_event(event_ticker: str) -> list[dict]:
    """Batched per SERIES in base.markets_for_event -- see its comment for the
    measured 429/latency problem the per-event version caused."""
    return markets_for_event(BASE, event_ticker)


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _market_row(event_ticker: str, fight_suffix: str, m: dict, **extra) -> dict:
    row = {
        "event_ticker": event_ticker,
        "fight_suffix": fight_suffix,
        "ticker": m["ticker"],
        "yes_sub_title": m.get("yes_sub_title", ""),
        "yes_bid": _to_float(m.get("yes_bid_dollars")),
        "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")),
        "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
        # Real, per-FIGHT estimated start time (not a flat event-level
        # time) -- confirmed live 2026-07-18: staggered realistically
        # across a card (e.g. 02:00Z opener through 06:20Z main event on
        # the same card). Polymarket's own equivalent field is flat
        # per-EVENT (same value for every fight on a card), so Kalshi is
        # the only source with real per-fight granularity -- see
        # poller_mma.py::_infer_start_time_from_kalshi.
        "occurrence_datetime": m.get("occurrence_datetime"),
    }
    row.update(extra)
    return row


def get_moneyline_markets() -> list[dict]:
    """Also the source of truth for each fight's real fighter names (via
    yes_sub_title) -- other series' clients resolve names by cross-
    referencing this series' fight_suffix, see market_matcher_mma.py."""
    events = get_open_events(MONEYLINE_SERIES)
    rows = []
    for ev in events:
        fight_suffix = kalshi_fight_suffix(ev["event_ticker"])
        if not fight_suffix:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(_market_row(
                ev["event_ticker"], fight_suffix, m,
                event_title=ev.get("title", ""),
                fighter_name=m.get("yes_sub_title", ""),
            ))
    return rows


def _fighter_names_by_suffix(moneyline_rows: list[dict]) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for row in moneyline_rows:
        names.setdefault(row["fight_suffix"], []).append(row["fighter_name"])
    return names


def get_distance_markets() -> list[dict]:
    events = get_open_events(DISTANCE_SERIES)
    rows = []
    for ev in events:
        fight_suffix = kalshi_fight_suffix(ev["event_ticker"])
        if not fight_suffix:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(_market_row(ev["event_ticker"], fight_suffix, m, event_title=ev.get("title", "")))
    return rows


def get_method_of_victory_markets() -> list[dict]:
    """7-way per fight: {ticker suffix like "-USMDEC"/"-DUKOTKODQ"/"-DRAWDRAW"}.
    Parses the OUTCOME (fighter side + method bucket, or "draw") from
    yes_sub_title directly rather than the ticker's own ad-hoc suffix code."""
    events = get_open_events(METHOD_OF_VICTORY_SERIES)
    rows = []
    for ev in events:
        fight_suffix = kalshi_fight_suffix(ev["event_ticker"])
        if not fight_suffix:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            sub = m.get("yes_sub_title", "")
            is_draw = "draw" in sub.lower() or "no contest" in sub.lower()
            rows.append(_market_row(
                ev["event_ticker"], fight_suffix, m,
                event_title=ev.get("title", ""),
                is_draw_outcome=is_draw,
            ))
    return rows


def get_method_of_finish_markets() -> list[dict]:
    """4-way, fight-level (not per-fighter): KO/TKO/DQ, Submission, Decision,
    Draw/No Contest."""
    events = get_open_events(METHOD_OF_FINISH_SERIES)
    rows = []
    for ev in events:
        fight_suffix = kalshi_fight_suffix(ev["event_ticker"])
        if not fight_suffix:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(_market_row(ev["event_ticker"], fight_suffix, m, event_title=ev.get("title", "")))
    return rows


def get_rounds_markets() -> list[dict]:
    """Ladder of "ends before round N?" markets, N in 2..5 (fewer for a
    3-round fight). NOT independent thresholds -- treat as a monotonic
    ladder, same caveat as this app's spread/total ladders elsewhere, though
    the underlying probability shape here is a single round-of-finish
    distribution, not two paired sides."""
    events = get_open_events(ROUNDS_SERIES)
    rows = []
    for ev in events:
        fight_suffix = kalshi_fight_suffix(ev["event_ticker"])
        if not fight_suffix:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            before_round = int(suffix) if suffix.isdigit() else None
            if before_round is None:
                continue
            rows.append(_market_row(
                ev["event_ticker"], fight_suffix, m,
                event_title=ev.get("title", ""),
                before_round=before_round,
            ))
    return rows


def get_round_of_victory_markets() -> list[dict]:
    """Fighter x round-of-victory (1-5) grid plus a single "OTHER"
    (decision/draw/NC) bucket per fight."""
    events = get_open_events(ROUND_OF_VICTORY_SERIES)
    rows = []
    for ev in events:
        fight_suffix = kalshi_fight_suffix(ev["event_ticker"])
        if not fight_suffix:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            is_other = suffix == "OTHER"
            rows.append(_market_row(
                ev["event_ticker"], fight_suffix, m,
                event_title=ev.get("title", ""),
                is_other_outcome=is_other,
            ))
    return rows
