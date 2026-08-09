"""Live-polling Kalshi client for Call of Duty. Parallel to
kalshi_cs2_client.py, and deliberately much smaller because the real live
inventory is much smaller.

WHAT KALSHI ACTUALLY LISTS (confirmed live 2026-08-09, by sweeping the /series
index rather than guessing tickers):

    KXCODGAME               match winner. 200 events, 192 markets, real
                            active inventory. One event per match bundling
                            BOTH teams' own YES markets, exactly the
                            KXCS2GAME shape:
                              event  KXCODGAME-26AUG091040100TOG
                                     title "100 Thieves vs. OpTic Gaming"
                              market KXCODGAME-...-100T  yes_sub_title "100 Thieves"
                              market KXCODGAME-...-OG    yes_sub_title "OpTic Gaming"

    KXWCCODWARZONE          Warzone at the 2025 Esports World Cup -- 1 event,
    KXEWCCALLOFDUTYBLOPS6   0 markets each. Defined, never used. Not wired:
                            a series with no markets is not supply, and the
                            Rocket League check this session already showed
                            how a defined-but-empty ticker reads as coverage
                            when it is nothing of the kind.

    KXMCBLACKOPS6 /         Metacritic score markets. NOT esports results --
    KXMCCODBLOPS7           a different question entirely, deliberately out
                            of scope for a team-rating model.

NO spread, total-maps or per-map series exists for CoD (checked the same
sweep). So this client has exactly one getter, and the router prices exactly
one market type. If Kalshi opens more later, mirror the CS2 client's
get_total_maps_markets/get_map_winner_markets -- but note that per-map markets
must NOT be staked off this model (elo_cod.prob_map_n_win_a returns the same
number for every map, the flaw already gated in the other three titles).

TEAM NAMES COME FROM yes_sub_title, and for CoD they need no alias map at all:
Kalshi says "OpTic Gaming" and "Team Falcons", and so does breakingpoint.gg.
Verified live -- both open events priced straight off the event title with no
mapping layer. That is the whole reason breakingpoint was chosen over
Liquipedia, whose CoD shortcodes are "tx"/"mia".

PRICES ARE NOT ON THE NESTED-MARKETS RESPONSE. Fetching events with
with_nested_markets=true returns markets whose yes_bid/yes_ask/last_price are
all null; the real prices come from the bulk /markets call. That is what
_event_market_pairs does (shared with CS2), so this is handled -- but it is
worth stating, because a nested-only implementation looks like it works and
silently prices nothing.
"""
import logging

from app.clients.base import paginate
from app.clients.kalshi_client import get_open_markets_for_series
from app.ingestion.kalshi_ticker_time import start_from_ticker

log = logging.getLogger("kalshi_cod_client")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

SERIES_WINNER_SERIES = "KXCODGAME"


def get_open_events(series_ticker: str) -> list[dict]:
    def url_builder(cursor):
        url = f"{BASE}/events?series_ticker={series_ticker}&status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        return url

    return paginate(url_builder, list_key="events", cursor_style="cursor")


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _event_market_pairs(series_ticker: str):
    """(event, market) pairs via ONE bulk markets call rather than one call per
    event -- the per-event version is what hit Kalshi 429s and stalled CS2's
    whole price refresh. Only markets belonging to this series' OPEN events are
    yielded."""
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
        # THE TICKER'S CLOCK, NOT occurrence_datetime. Kalshi sets occurrence
        # once and never revises it when a match moves; the ticker carries an
        # Eastern-time stamp that measured within 15 min of the real start on
        # 25/28 LoL and 3/3 CS2 matches (vs 16/28 and 0/3 for occurrence).
        #
        # This is not a stale-data nicety. A start time later than reality is
        # exactly what let a LIVE soccer match be recommended on 2026-08-09
        # (Kalshi said 14:30Z against a real 11:30Z kickoff), because the
        # router's already-started guard had nothing to fire on. Using the
        # ticker clock here means CoD does not inherit that hole.
        "occurrence_datetime": start_from_ticker(m.get("ticker")) or m.get("occurrence_datetime"),
    }
    row.update(extra)
    return row


def get_series_winner_markets() -> list[dict]:
    """One event per match, two markets (one per team's own YES side), team
    name from yes_sub_title -- the same convention as KXCS2GAME."""
    rows = []
    for ev, m in _event_market_pairs(SERIES_WINNER_SERIES):
        rows.append(_base_row(
            ev["event_ticker"], ev.get("title", ""), m,
            team_name=m.get("yes_sub_title", ""),
        ))
    return rows
