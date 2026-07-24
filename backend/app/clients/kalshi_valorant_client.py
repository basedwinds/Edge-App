"""Live-polling Kalshi client for Valorant markets. Parallel to
kalshi_mma_client.py, but confirmed live 2026-07-19 to have a genuinely
different event structure: KXVALORANTMAP is ONE EVENT PER MAP (not one event
per match) -- e.g. event_ticker "KXVALORANTMAP-26JUL221400KCFNC-2" is
"Karmine Corp vs. Fnatic: Map 2", with a SEPARATE sibling event
"KXVALORANTMAP-26JUL221400KCFNC-1" for Map 1. The shared prefix before the
trailing "-{map_number}" (e.g. "26JUL221400KCFNC") is the real cross-map
join key for a single match, confirmed live via a real events listing --
parallel role to kalshi_mma_client.py's kalshi_fight_suffix, just with an
extra map-number segment to strip first.

Series confirmed live 2026-07-19:
  KXVALORANTMAP - map winner, ONE EVENT PER MAP (see above), 2 binary
                  markets per event (one per team's own YES side) -- 20
                  real open markets confirmed live across multiple regions
                  (Karmine Corp vs Fnatic, Trace Esports vs JD Gaming, Nova
                  Esports vs EDward Gaming, Pcific Esports vs FUT Esports).
  KXVALORANT    - tournament winner futures, team-less-of-a-single-match
                  shape (built in market_catalog_valorant.py, not this
                  client -- no per-match structure to parse here).

REAL BUG this fixes (found live 2026-07-20, via catalog_scan.py's newly-
added esports coverage -- user-reported: "keep pushing esports... covering
everything all markets"): a real, standalone whole-SERIES winner ticker DOES
exist now -- KXVALORANTGAME (confirmed live: 20 real open markets, one event
per match e.g. "KXVALORANTGAME-26JUL231700LEVEG" bundling both teams' own
YES markets, "Leviatán Esports vs. Evil Geniuses") -- the original
2026-07-19 catalog check only found KXVALORANTGAMETEAMVSMIBR (a genuine
one-off event-specific ticker, not a reusable series, and empty besides),
and concluded no generic series existed at all. Same shape as CS2's own
KXCS2GAME (one event per match, team name from yes_sub_title) -- Polymarket
ALSO has a real match-winner market type (see polymarket_valorant_client.py),
so Valorant's own series_winner market_type now has two real independent
price sources, same as every other market type here.
"""
import re

from app.clients.base import get_json, paginate

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MAP_WINNER_SERIES = "KXVALORANTMAP"
SERIES_WINNER_SERIES = "KXVALORANTGAME"

_MAP_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")


def match_code_and_map_number(event_ticker: str) -> tuple[str, int] | None:
    """"KXVALORANTMAP-26JUL221400KCFNC-2" -> ("26JUL221400KCFNC", 2). None
    if the ticker doesn't end in a numeric map-number suffix."""
    prefix = f"{MAP_WINNER_SERIES}-"
    if not event_ticker.startswith(prefix):
        return None
    rest = event_ticker[len(prefix):]
    m = _MAP_SUFFIX_RE.match(rest)
    if not m:
        return None
    return m.group(1), int(m.group(2))


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


def get_map_winner_markets() -> list[dict]:
    """Real fighter-name-equivalent here is the team name, read from
    yes_sub_title (same "trust the market's own label, don't decode an
    ad-hoc ticker abbreviation" discipline as kalshi_mma_client.py)."""
    events = get_open_events(MAP_WINNER_SERIES)
    rows = []
    for ev in events:
        parsed = match_code_and_map_number(ev["event_ticker"])
        if parsed is None:
            continue
        match_code, map_number = parsed
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append({
                "event_ticker": ev["event_ticker"],
                "match_code": match_code,
                "map_number": map_number,
                "ticker": m["ticker"],
                "team_name": m.get("yes_sub_title", ""),
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "last_price": _to_float(m.get("last_price_dollars")),
                "volume": _to_float(m.get("volume_fp")),
                "status": m.get("status"),
                "occurrence_datetime": m.get("occurrence_datetime"),
                "event_title": ev.get("title", ""),
            })
    return rows


def get_series_winner_markets() -> list[dict]:
    """KXVALORANTGAME -- whole-match/series winner, one event per match, 2
    binary markets per event (one per team's own YES side) -- see module
    docstring's real-bug note. Same row shape as kalshi_cs2_client.py's own
    get_series_winner_markets()."""
    rows = []
    for ev in get_open_events(SERIES_WINNER_SERIES):
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append({
                "event_ticker": ev["event_ticker"],
                "ticker": m["ticker"],
                "team_name": m.get("yes_sub_title", ""),
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "last_price": _to_float(m.get("last_price_dollars")),
                "volume": _to_float(m.get("volume_fp")),
                "status": m.get("status"),
                "occurrence_datetime": m.get("occurrence_datetime"),
                "event_title": ev.get("title", ""),
            })
    return rows


def get_tournament_winner_markets() -> list[dict]:
    """KXVALORANT -- season/tournament-long futures, team-less-of-a-single-
    match shape (no match_code/map_number to parse). group_label taken from
    the event's own title, same convention as this app's other sports'
    league_winner-style futures."""
    events = get_open_events("KXVALORANT")
    rows = []
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append({
                "event_ticker": ev["event_ticker"],
                "group_label": ev.get("title", ""),
                "ticker": m["ticker"],
                "team_name": m.get("yes_sub_title", ""),
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "last_price": _to_float(m.get("last_price_dollars")),
                "volume": _to_float(m.get("volume_fp")),
                "status": m.get("status"),
            })
    return rows
