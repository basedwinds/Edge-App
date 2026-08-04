"""Live-polling Kalshi client for League of Legends markets. Parallel to
kalshi_valorant_client.py -- confirmed live 2026-07-19 the real inventory
shape matches Valorant's, not CS2's: KXLOLMAP is ONE EVENT PER MAP (24 real
open markets, e.g. event_ticker "KXLOLMAP-26JUL191900SDMKITS-2" = "SDM
Tigres vs. Kits Esports: Map 2", with a separate sibling event for Map 1),
same shared-match-code-prefix-plus-trailing-map-number structure as
kalshi_valorant_client.py's KXVALORANTMAP.

KXLOLTOTALMAPS (12 real open markets confirmed live) is genuinely
game-level/per-match, one market per event, team names + the real over-line
parsed from the market's own title text (same approach as
kalshi_cs2_client.py's KXCS2TOTALMAPS, which has an identical title-text
shape: "Will over N.N maps be played in the {team} vs. {team} League of
Legends match?").

KXLEAGUEWORLDS (Worlds tournament-winner futures) exists as a real series
but wasn't checked for CURRENT open markets at build time -- same "ready,
verify inventory live before relying on it" status as CS2's KXCS2 futures
client. No Polymarket LoL match-level markets exist -- checked live, real
Polymarket LoL inventory (tag_slug=lol) is the LCK/LPL/Worlds season-winner
futures only, no match-level market type at all.

REAL COVERAGE GAP this fixes (found live 2026-07-20, via catalog_scan.py's
newly-added esports coverage -- user-reported: "keep pushing esports...
covering everything all markets"): a real, standalone whole-SERIES winner
ticker exists too -- KXLOLGAME (confirmed live: 38 real open markets, one
event per match e.g. "KXLOLGAME-26JUL231700CPDBLUE" bundling both teams' own
YES markets, "Cupid Esports vs. Blue Otter") -- this app's own build docs
previously stated "no series (whole-match) winner Kalshi ticker exists for
LoL, unlike CS2's KXCS2GAME" (see poller_valorant.py's own docstring, which
made the same comparison for Valorant), which was true when checked
2026-07-19 but Kalshi has since added one. Same shape as CS2/Valorant's own
KXCS2GAME/KXVALORANTGAME (one event per match, team name from
yes_sub_title).
"""
import re
from app.ingestion.kalshi_ticker_time import start_from_ticker

from app.clients.base import get_json, paginate

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MAP_WINNER_SERIES = "KXLOLMAP"
SERIES_WINNER_SERIES = "KXLOLGAME"
TOTAL_MAPS_SERIES = "KXLOLTOTALMAPS"
TOURNAMENT_WINNER_SERIES = "KXLEAGUEWORLDS"

_MAP_SUFFIX_RE = re.compile(r"^(.*)-(\d+)$")
_TOTAL_MAPS_TITLE_RE = re.compile(r"Will over ([\d.]+) maps be played in the (.+?) vs\.? (.+?) League of Legends match\?", re.IGNORECASE)


def match_code_and_map_number(event_ticker: str) -> tuple[str, int] | None:
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
        # The TICKER's own clock, not occurrence_datetime, is the real scheduled
        # start: Kalshi sets occurrence once and never revises it when a match
        # moves, while the ticker carries an Eastern-time stamp that measured
        # within 15 min of Flashscore's real start on 25/28 LoL and 3/3 CS2
        # matches (vs 16/28 and 0/3 for occurrence). Falls back to occurrence
        # whenever the ticker has no clock. See ingestion/kalshi_ticker_time.py.
        "occurrence_datetime": start_from_ticker(m.get("ticker")) or m.get("occurrence_datetime"),
    }
    row.update(extra)
    return row


def get_map_winner_markets() -> list[dict]:
    rows = []
    for ev in get_open_events(MAP_WINNER_SERIES):
        parsed = match_code_and_map_number(ev["event_ticker"])
        if parsed is None:
            continue
        match_code, map_number = parsed
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(_base_row(
                ev["event_ticker"], ev.get("title", ""), m,
                match_code=match_code, map_number=map_number,
                team_name=m.get("yes_sub_title", ""),
            ))
    return rows


def get_series_winner_markets() -> list[dict]:
    """KXLOLGAME -- whole-match/series winner, one event per match, 2
    binary markets per event (one per team's own YES side) -- see module
    docstring's real-bug note. Same row shape as kalshi_cs2_client.py's own
    get_series_winner_markets()/kalshi_valorant_client.py's own new
    version."""
    rows = []
    for ev in get_open_events(SERIES_WINNER_SERIES):
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(_base_row(
                ev["event_ticker"], ev.get("title", ""), m,
                team_name=m.get("yes_sub_title", ""),
            ))
    return rows


def get_total_maps_markets() -> list[dict]:
    rows = []
    for ev in get_open_events(TOTAL_MAPS_SERIES):
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
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
    """KXLEAGUEWORLDS -- season/tournament-long futures. See module docstring
    -- current open-market count not verified live at build time."""
    rows = []
    for ev in get_open_events(TOURNAMENT_WINNER_SERIES):
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(_base_row(
                ev["event_ticker"], ev.get("title", ""), m,
                group_label=ev.get("title", ""),
                team_name=m.get("yes_sub_title", ""),
            ))
    return rows
