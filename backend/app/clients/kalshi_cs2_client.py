"""Live-polling Kalshi client for CS2 markets. Parallel to
kalshi_valorant_client.py, but the real inventory shape here is genuinely
different: KXCS2GAME is a whole-MATCH/series winner market (confirmed live
2026-07-19: 20 real open markets, one event per match e.g.
"KXCS2GAME-26JUL211200FOKAST" bundling both teams' own YES markets --
titled "Will FOKUS win the FOKUS vs. Astralis CS2 match?"), NOT per-map the
way Valorant's KXVALORANTMAP is. KXCS2TOTALMAPS (total maps played, 16 real
open markets confirmed live) is genuinely per-match too.

REAL BUG this fixes (found live 2026-07-20, via catalog_scan.py's newly-
added esports coverage -- user-reported: "keep pushing esports... covering
everything all markets"): the per-map series ticker was hardcoded here as
"KXCS2MAPWINNER", genuinely empty when originally checked (2026-07-19) --
but Kalshi's real live per-map ticker for CS2 is "KXCS2MAP" (confirmed live:
52 real open markets, e.g. "KXCS2MAP-26JUL221330100TFAL-2" bundling both
teams' own YES markets for "100 Thieves vs. Team Falcons: Map 2"), a
DIFFERENT ticker this app never queried at all -- not the same series
renamed, a parallel one. Same event-ticker shape as Valorant's own
KXVALORANTMAP (trailing "-N" map-number suffix, team name from
yes_sub_title), so the existing get_map_winner_markets() parsing below needed
no changes, just the corrected ticker constant. KXCS2 (tournament winner
futures) was ALSO checked live and found empty right now -- CS2's real
tournament calendar has no active futures contract at this exact moment,
unlike Valorant/LoL which did.

Team names are read from yes_sub_title (KXCS2GAME) / parsed from the
market's own title text (KXCS2TOTALMAPS, which has no per-team yes_sub_title
since it's a single game-level market, not per-team) -- same "trust the
market's own real label" discipline as kalshi_mma_client.py.
"""
import logging
import re

from app.clients.base import get_json, paginate
from app.clients.kalshi_client import get_open_markets_for_series

log = logging.getLogger("kalshi_cs2_client")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

SERIES_WINNER_SERIES = "KXCS2GAME"
MAP_WINNER_SERIES = "KXCS2MAP"
TOTAL_MAPS_SERIES = "KXCS2TOTALMAPS"
TOURNAMENT_WINNER_SERIES = "KXCS2"

_TOTAL_MAPS_TITLE_RE = re.compile(r"Will over ([\d.]+) maps be played in the (.+?) vs\.? (.+?) CS2 match\?", re.IGNORECASE)


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



def _event_market_pairs(series_ticker: str):
    """(event, market) pairs for a series using ONE bulk markets call instead of
    one call per event -- see kalshi_client.get_open_markets_for_series for the
    measured reason (cs2's ~300 per-event calls hit Kalshi 429s and stalled the
    whole sport's price refresh). Semantics are preserved: only markets whose
    event_ticker is in this series' OPEN events list are yielded, exactly as the
    old get_open_events() -> get_markets_for_event() loop did."""
    events = get_open_events(series_ticker)
    by_event: dict[str, list[dict]] = {}
    try:
        for m in get_open_markets_for_series(series_ticker):
            by_event.setdefault(m.get("event_ticker"), []).append(m)
    except Exception:
        log.exception("bulk market fetch failed for %s", series_ticker)
        return
    for ev in events:
        for m in by_event.get(ev["event_ticker"], []):
            yield ev, m


def _base_row(event_ticker: str, event_title: str, m: dict, **extra) -> dict:
    row = {
        "event_ticker": event_ticker,
        "event_title": event_title,
        "ticker": m["ticker"],
        "yes_bid": _to_float(m.get("yes_bid_dollars")),
        "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")),
        "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
        "occurrence_datetime": m.get("occurrence_datetime"),
    }
    row.update(extra)
    return row


def get_series_winner_markets() -> list[dict]:
    """One event per match, 2 markets (one per team's own YES side) --
    team name from yes_sub_title, same convention as every fighter-name
    market in kalshi_mma_client.py."""
    rows = []
    for ev, m in _event_market_pairs(SERIES_WINNER_SERIES):
        if True:
            rows.append(_base_row(
                ev["event_ticker"], ev.get("title", ""), m,
                team_name=m.get("yes_sub_title", ""),
            ))
    return rows


def get_map_winner_markets() -> list[dict]:
    """See module docstring's real-bug note -- KXCS2MAP (not KXCS2MAPWINNER)
    is the real live per-map ticker, confirmed live with 52 real open
    markets, same "one event per map, map number as a trailing
    event_ticker segment" shape kalshi_valorant_client.py's KXVALORANTMAP
    already uses."""
    rows = []
    for ev, m in _event_market_pairs(MAP_WINNER_SERIES):
        m_num = re.search(r"-(\d+)$", ev["event_ticker"])
        if not m_num:
            continue
        match_code = ev["event_ticker"][: m_num.start()]
        map_number = int(m_num.group(1))
        if True:
            rows.append(_base_row(
                ev["event_ticker"], ev.get("title", ""), m,
                match_code=match_code, map_number=map_number,
                team_name=m.get("yes_sub_title", ""),
            ))
    return rows


def get_total_maps_markets() -> list[dict]:
    """Game-level (not per-team) -- one market per event, real team names
    and the real over-line parsed from the market's own title text (no
    yes_sub_title carries a team name here, confirmed live)."""
    rows = []
    for ev, m in _event_market_pairs(TOTAL_MAPS_SERIES):
        if True:
            title_match = _TOTAL_MAPS_TITLE_RE.search(m.get("title", ""))
            if not title_match:
                continue
            line, team_a, team_b = title_match.groups()
            rows.append(_base_row(
                ev["event_ticker"], ev.get("title", ""), m,
                line=float(line), team_a=team_a.strip(), team_b=team_b.strip(),
            ))
    return rows


def get_tournament_winner_markets() -> list[dict]:
    """KXCS2 -- season/tournament-long futures. Currently empty live
    inventory (checked 2026-07-19), kept for when a real tournament-winner
    contract opens."""
    rows = []
    for ev, m in _event_market_pairs(TOURNAMENT_WINNER_SERIES):
        if True:
            rows.append(_base_row(
                ev["event_ticker"], ev.get("title", ""), m,
                group_label=ev.get("title", ""),
                team_name=m.get("yes_sub_title", ""),
            ))
    return rows
