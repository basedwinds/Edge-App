"""Live-polling Kalshi client for NBA markets. Parallel to kalshi_client.py
(NFL), same architecture-decision reasoning as market_matcher_nba.py.

Series tickers confirmed live against the public API 2026-07-16:
  KXNBAGAME        - regular-season moneyline (0 open right now -- season
                      starts October, same "not listed this far out" pattern
                      NFL spread/total were in before one surfaced mid-build)
  KXNBASPREAD/-TOTAL - regular-season spread/total (also 0 open)
  KXNBASUMMERGAME/-SPREAD/-TOTAL - Summer League (LIVE right now, real
                      ladder data, same shape as NFL's per-game series)
  Futures (all confirmed open+liquid): KXNBA (championship), KXNBAEAST/
  KXNBAWEST (conference), the 6 division series, KXNBAPLAYOFF (playoff
  qualifier), KXNBAPLAYIN (play-in), KXNBAWINS-{team} (win totals, per-team
  series like NFL's), KXRECORDNBABEST (best record -- confirmed 0 open,
  unlike NFL's equivalent, included anyway so it activates automatically
  once Kalshi opens it).

Explicitly NOT built (same "different kind of model" scoping as NFL's
240-series Phase-4 audit): MVP/DPOY/ROY/6MOY/COY and every other player-level
award/prop series -- this app's team-level Elo has no way to price those.
"""
from app.clients.base import get_json, paginate
from app.ingestion.market_matcher_nba import KALSHI_TEAM_ABBRS, parse_kalshi_event_ticker, split_teams_blob, to_espn_abbr

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = "KXNBAGAME"
SPREAD_SERIES = "KXNBASPREAD"
TOTAL_SERIES = "KXNBATOTAL"
TEAM_TOTAL_SERIES = "KXNBATEAMTOTAL"
HALF_SPREAD_SERIES = {1: "KXNBA1HSPREAD", 2: "KXNBA2HSPREAD"}
HALF_TOTAL_SERIES = {1: "KXNBA1HTOTAL", 2: "KXNBA2HTOTAL"}
SUMMER_MONEYLINE_SERIES = "KXNBASUMMERGAME"
SUMMER_SPREAD_SERIES = "KXNBASUMMERSPREAD"
SUMMER_TOTAL_SERIES = "KXNBASUMMERTOTAL"

FUTURES_SERIES = {
    "conference_champion": ["KXNBAEAST", "KXNBAWEST"],
    "division_winner": [
        "KXNBAATLANTIC", "KXNBACENTRAL", "KXNBASOUTHEAST",
        "KXNBANORTHWEST", "KXNBAPACIFIC", "KXNBASOUTHWEST",
    ],
    "championship": ["KXNBA"],
    "playoff_qualifier": ["KXNBAPLAYOFF"],
    "play_in_qualifier": ["KXNBAPLAYIN"],
}

# REAL FIND during a "check if anything was missed" pass (2026-07-16, after
# Phase 2 was otherwise complete): "KXRECORDNBABEST" (the ticker guessed by
# analogy to NFL's KXRECORDNFLBEST/KXRECORDNFLWORST convention) doesn't
# exist at all -- confirmed 0 open events, repeatedly, across two separate
# checks. The REAL series is "KXNBARECORD", a single series covering BOTH
# best and worst via two events ("KXNBARECORD-27BEST"/"-27WORST", 30 real
# open markets each, ticker suffix = plain team abbreviation) -- a genuinely
# different structure than NFL's two-separate-series design, not just a
# naming difference. Handled by its own function below rather than forced
# into the generic FUTURES_SERIES loop (which assumes one whole series =
# one market_kind, true for everything else in this dict but not this one).
RECORD_SERIES = "KXNBARECORD"

WIN_TOTAL_SERIES_PREFIX = "KXNBAWINS-"


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


def get_moneyline_markets(series: str = MONEYLINE_SERIES) -> list[dict]:
    """Returns a flat list of dicts, one per team-side market. `series`
    defaults to the regular-season series but is overridable so
    get_summer_moneyline_markets can reuse this without duplicating the loop."""
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


def get_summer_moneyline_markets() -> list[dict]:
    return get_moneyline_markets(SUMMER_MONEYLINE_SERIES)


def _get_team_ladder_markets(series: str) -> list[dict]:
    """Shared by spread/team-ladder series -- event ticker's teams blob
    resolves the two candidate teams, market ticker glues team+a rounded
    number with no separator, so the team is resolved by checking which
    known team prefixes the suffix (same approach as kalshi_client.py's NFL
    version)."""
    events = get_open_events(series)
    rows = []
    for ev in events:
        parsed = parse_kalshi_event_ticker(ev["event_ticker"])
        split = split_teams_blob(parsed["teams_blob"], KALSHI_TEAM_ABBRS) if parsed else None
        if not split:
            continue
        away_k, home_k = split
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            team_k = away_k if suffix.startswith(away_k) else (home_k if suffix.startswith(home_k) else None)
            if team_k is None or m.get("floor_strike") is None:
                continue
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "ticker": m["ticker"],
                    "team_abbr_kalshi": team_k,
                    "line": float(m["floor_strike"]),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def _get_game_ladder_markets(series: str) -> list[dict]:
    events = get_open_events(series)
    rows = []
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            if m.get("floor_strike") is None:
                continue
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "ticker": m["ticker"],
                    "line": float(m["floor_strike"]),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_spread_markets() -> list[dict]:
    return _get_team_ladder_markets(SPREAD_SERIES)


def get_total_markets() -> list[dict]:
    return _get_game_ladder_markets(TOTAL_SERIES)


def get_summer_spread_markets() -> list[dict]:
    return _get_team_ladder_markets(SUMMER_SPREAD_SERIES)


def get_summer_total_markets() -> list[dict]:
    return _get_game_ladder_markets(SUMMER_TOTAL_SERIES)


def get_team_total_markets() -> list[dict]:
    """KXNBATEAMTOTAL -- confirmed to exist in the live catalog (2026-07-17)
    but 0 open events so far (season starts October, same "not listed this
    far out" pattern as spread/total before one opened). Same team-ladder
    shape as get_spread_markets."""
    return _get_team_ladder_markets(TEAM_TOTAL_SERIES)


def get_half_spread_markets(half: int) -> list[dict]:
    """1st/2nd-half spread -- KXNBA1HSPREAD/KXNBA2HSPREAD, confirmed to
    exist in the live catalog but 0 open events so far. `half` is 1 or 2."""
    return _get_team_ladder_markets(HALF_SPREAD_SERIES[half])


def get_half_total_markets(half: int) -> list[dict]:
    """1st/2nd-half total -- see get_half_spread_markets."""
    return _get_game_ladder_markets(HALF_TOTAL_SERIES[half])


def get_futures_markets() -> list[dict]:
    """Returns a flat list of dicts, one per team-side futures market,
    tagged with market_kind and a group_label taken directly from Kalshi's
    own event title -- same shape as kalshi_client.py's NFL version."""
    rows = []
    for kind, series_list in FUTURES_SERIES.items():
        for series in series_list:
            try:
                events = get_open_events(series)
            except Exception:
                continue
            for ev in events:
                try:
                    markets = get_markets_for_event(ev["event_ticker"])
                except Exception:
                    continue
                for m in markets:
                    rows.append(
                        {
                            "market_kind": kind,
                            "event_ticker": ev["event_ticker"],
                            "group_label": ev.get("title", ""),
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


def get_record_markets() -> list[dict]:
    """KXNBARECORD-27BEST/-27WORST -- see RECORD_SERIES docstring above for
    why this needs its own function instead of fitting FUTURES_SERIES's
    one-series-one-kind assumption. market_kind is read off which event this
    row came from (title contains "Best"/"Worst"), not guessed."""
    rows = []
    try:
        events = get_open_events(RECORD_SERIES)
    except Exception:
        return rows
    for ev in events:
        title = ev.get("title", "")
        if "Worst" in title:
            kind = "worst_record"
        elif "Best" in title:
            kind = "best_record"
        else:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(
                {
                    "market_kind": kind,
                    "event_ticker": ev["event_ticker"],
                    "group_label": title,
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


def get_win_total_markets() -> list[dict]:
    """Per-team win-total ladder (KXNBAWINS-{team}), iterating all 30 teams'
    own series -- same shape/reasoning as kalshi_client.py's NFL version."""
    rows = []
    for team in sorted({to_espn_abbr(k) for k in KALSHI_TEAM_ABBRS}):
        from app.ingestion.market_matcher_nba import to_kalshi_abbr

        series = f"{WIN_TOTAL_SERIES_PREFIX}{to_kalshi_abbr(team)}"
        try:
            events = get_open_events(series)
        except Exception:
            continue
        for ev in events:
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                if m.get("floor_strike") is None:
                    continue
                rows.append(
                    {
                        "event_ticker": ev["event_ticker"],
                        "ticker": m["ticker"],
                        "team": team,
                        "line": float(m["floor_strike"]),
                        "yes_bid": _to_float(m.get("yes_bid_dollars")),
                        "yes_ask": _to_float(m.get("yes_ask_dollars")),
                        "last_price": _to_float(m.get("last_price_dollars")),
                        "volume": _to_float(m.get("volume_fp")),
                        "status": m.get("status"),
                    }
                )
    return rows
