"""Live-polling Polymarket client for CS2. Parallel to
polymarket_valorant_client.py / polymarket_lol_client.py (same "one event,
many groupItemTitle-labeled markets" shape).

REAL GAP THIS CLOSES (found by catalog_scan.py's own "other" catch-all bucket,
confirmed live 2026-08-02): this app queried Polymarket CS2 under
`tag_slug=cs2`, which returns PROPS ONLY -- roster changes, Valve sticker
trade-ups / map-pool additions, "will ZywOo reach 36 HLTV MVP awards".  That
is where poller_cs2.py's old "no standard match-outcome market type at all --
an honest inventory gap, not a build gap" note came from.  It was wrong: it
was derived from the wrong tag.  The real head-to-head events are tagged
`counter-strike-2` -- 62 live, future-dated match events carrying ~$2.7M of
liquidity that this app could not see, in a sport it ALREADY models with Elo
and ALREADY prices on Kalshi (KXCS2GAME/KXCS2MAPWINNER/KXCS2TOTALMAPS).  So
these feed the existing cross-platform Kalshi-vs-Polymarket divergence scanner
(cross_platform_divergence.py) with no other change.

BOTH tags are queried and deduped by event slug (same multi-tag shape as
polymarket_nba_client's nba + nba-summer-league).  `counter-strike-2` is very
nearly a superset of `cs2` -- confirmed live, the only event under `cs2` that
is NOT also under `counter-strike-2` was a Valve sticker-trade-up prop this
app has no way to price anyway -- but `cs2` is kept rather than dropped so a
future re-tagging on Polymarket's side can't silently re-open the same
blind spot in the other direction.

Confirmed live 2026-08-02, a CS2 match event (title "Counter-Strike: OldBoys
vs Arch (BO1) - ESEA Advanced Europe Regular Season") bundles:
  - "Match Winner"                  -> series_winner (outcomes = the two real
                                       team names, NOT ["Yes","No"])
  - "Map N Winner"                  -> map_winner, same two-team-name shape
  - "O/U N.5 Games"                 -> series_total (maps played)
  - "Map Handicap: A (-1.5) vs B (+1.5)"
                                    -> series_handicap
Real live inventory across the 62 match events: 62 Match Winner, 110 Map N
Winner, 55 O/U N Games, 45 Map Handicap.

DELIBERATELY NOT INGESTED -- CS2's Polymarket events also carry two ROUND-level
market types that Valorant/LoL simply don't have, and that are by far the most
numerous of the lot (133 + 83 markets):
  - "Map N Total Rounds: Over/Under 21.5"
  - "Map N Rounds Handicap: A (-3.5) vs B (+3.5)"
This app's CS2 model (elo_cs2.py) is a team-level SERIES/MAP model -- it has no
round-level distribution at all, so there is nothing to price these against.
Ingesting them would create rows that can only ever show a null model_prob.
Both regexes below are anchored so they cannot catch these by accident
("^Map Handicap:" does not match "Map 1 Rounds Handicap:", and "^O/U N Games$"
does not match "Map 1 Total Rounds: Over/Under 21.5").

NO FUTURES PATH, deliberately -- and this is a real trap, not an omission.
Valorant/LoL both detect futures structurally as "an event whose title has no
'vs'".  Applying that heuristic here would be actively WRONG: every non-"vs"
event under `counter-strike-2` is a PROP, not a team-to-win-a-tournament
future (confirmed live: Valve map-pool/operation props, a fnatic
merger/acquisition prop, an HLTV-MVP-count prop, "will ArrowCS eat a bug on
stream").  Several of them carry DATE strings as groupItemTitle ("June 30",
"May 31", "June 1, 2026"), so the Valorant-style futures loop -- which reads
groupItemTitle as a TEAM NAME -- would happily upsert a tournament_winner
market for a team called "June 30".  Match events are therefore selected
POSITIVELY (title must contain " vs "), never by negation.  CS2's real
tournament-winner futures inventory is on the Kalshi side
(upsert_kalshi_cs2_tournament_winner_market).

STALENESS -- THE CRITICAL HAZARD HERE.  `closed=false` is NOT a liveness
signal on Gamma.  Confirmed live 2026-08-02: a bare
`tag_slug=counter-strike-2&closed=false` returns 100 events of which 98 are
`active=true` but ENDED MONTHS AGO (March 2026).  Naively swapping the slug
would have injected ~98 dead markets showing bogus settled prices -- precisely
the dead-market price bug this project has already had to fix three separate
times (Kalshi, MLB, Tennis; see dead_market_sanity_check.py's own docstring).
Three layered gates, in order of how much work each actually does:
  1. `end_date_min=<now>` -- a SERVER-side filter, and the one that does the
     real work: it cuts the listing from 100 stale-dominated events to 69, of
     which the most-stale survivor is only ~0.2 days past (i.e. genuinely
     today's matches).  Nothing months-old survives it.
  2. `_is_stale()` on the market's own gameStartTime (>2 days past), same
     belt-and-braces guard polymarket_lol_client.py already uses -- endDate is
     PADDED well past the real match time (confirmed live: an event whose slug
     and real start are 2026-08-08 carries endDate 2026-08-09), so end_date_min
     alone must not be trusted as a match-time gate.
  3. each market's own closed/active flags (`_market_status`), the same
     per-market-inside-an-open-event gap already fixed for Tennis/MMA.
Precise "has this specific match already started" gating is deliberately left
to cs2_markets.py's own `_match_already_started` router filter rather than
duplicated here -- that is this codebase's established ingest-broadly/gate-at-
the-router split, and it keeps a real price history for CLV on matches that
have since started.  That gate reads Cs2Match.estimated_start_time, which
poller_cs2.py populates from the `gameStartTime` this client extracts;
confirmed live that gameStartTime is present on 100% of CS2 match markets
(494/494), so it is a STRONGER start-time signal here than on the Kalshi side,
where it depends on a liquipedia.net scrape that lags real trading.
"""
import datetime
import re

from app.clients.base import paginate
from app.clients.polymarket_client import extract_market_prices, handicap_lines_in_outcome_order

GAMMA = "https://gamma-api.polymarket.com"

# See module docstring -- counter-strike-2 is the real head-to-head tag; cs2
# (props only) is kept alongside it purely so a future re-tag can't re-open
# the blind spot from the other direction.
CS2_TAG_SLUGS = ("counter-strike-2", "cs2")

_MAP_WINNER_RE = re.compile(r"^Map\s+(\d+)\s+Winner$", re.IGNORECASE)
_TOTAL_GAMES_RE = re.compile(r"^O/U\s+([\d.]+)\s+Games$", re.IGNORECASE)
# Anchored at "Map Handicap:" so it can NOT match "Map 1 Rounds Handicap: ..."
# (a round-level market this app has no model for -- see module docstring).
_HANDICAP_RE = re.compile(
    r"^Map Handicap:\s*(.+?)\s*\(([+-][\d.]+)\)\s*vs\s*(.+?)\s*\(([+-][\d.]+)\)$", re.IGNORECASE
)
# "Counter-Strike: OldBoys vs Arch (BO1) - ESEA Advanced Europe Regular Season"
_BEST_OF_RE = re.compile(r"\(BO(\d)\)", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_open_events(limit: int = 100) -> list[dict]:
    """Deduped union of both tags, with the server-side end_date_min liveness
    filter applied (see module docstring gate 1 -- without it this returns
    ~98 months-dead events marked active=true).

    Offset pagination (same as every sibling Polymarket client), NOT keyset,
    which is a real ceiling worth knowing about: Gamma's `/events?offset=`
    returns HTTP 422 past offset=500, so this tops out at ~500 live CS2
    events. That is ~7x current real inventory (69 events survive
    end_date_min today), and the end_date_min filter is what keeps it there --
    the same query without it already returns a full page of dead events.
    Deliberately not switching to /events/keyset for a listing this size;
    catalog_scan.py's own _fetch_polymarket_live_sports_events is the keyset
    example if this ever does need it. Note that on keyset the cursor param is
    `after_cursor`, not the `next_cursor` the response field is named --
    passing the wrong one silently re-returns page 1 forever."""
    end_date_min = _now_iso()
    out: dict[str, dict] = {}
    for tag_slug in CS2_TAG_SLUGS:
        def url_builder(offset, ts=tag_slug):
            return (
                f"{GAMMA}/events?tag_slug={ts}&closed=false"
                f"&end_date_min={end_date_min}&limit={limit}&offset={offset}"
            )

        for event in paginate(url_builder, list_key=None, limit=limit, cursor_style="offset"):
            slug = event.get("slug")
            if slug and slug not in out:
                out[slug] = event
    return list(out.values())


def _is_match_event(event: dict) -> bool:
    """POSITIVE selection, never "not a futures event" -- see the module
    docstring's futures trap. Every real CS2 match event's title is
    "Counter-Strike: A vs B (BOn) - Tournament"."""
    return " vs " in (event.get("title") or "")


def _market_status(m: dict) -> str:
    """Same real per-market-inside-an-open-event bug already fixed for
    Tennis/MMA -- an event's own closed=false doesn't guarantee every market
    inside it is still open."""
    if m.get("closed") or not m.get("active", True):
        return "closed"
    return "active"


def _normalize_start_time(raw) -> str | None:
    """Polymarket-wide `gameStartTime` format quirk (space separator, bare
    "+00" UTC offset) -- see polymarket_tennis_client.py's own version."""
    if not raw:
        return None
    text = str(raw).strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "Z"
    return text


def _is_stale(start_iso: str | None) -> bool:
    """Gate 2 (see module docstring). Same 2-day window as
    polymarket_lol_client.py -- endDate is padded past the real match time, so
    the server-side end_date_min filter must not be trusted on its own."""
    if not start_iso:
        return False  # no time -> can't tell; keep it, the router still gates it
    try:
        dt = datetime.datetime.fromisoformat(start_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return False
    return dt < datetime.datetime.utcnow() - datetime.timedelta(days=2)


def parse_best_of(event_title: str | None) -> int | None:
    """CS2's Polymarket titles state the series length outright ("(BO3)") --
    a signal Valorant/LoL's own Polymarket listings don't carry.

    Worth having beyond mirroring: best_of is a hard gate on CS2 pricing at
    all (_game_model_prob can't build a series distribution without it), and
    it is CS2's known weak spot -- 24/30 real open matches once had no
    model_prob for exactly this reason (see poller_cs2.py's own coverage-gap
    note). The two existing backfills INFER best_of indirectly, from a
    KXCS2TOTALMAPS O/U line or from Kalshi per-map ladder depth; this one
    reads it directly off the title. Only accepts real odd series lengths."""
    if not event_title:
        return None
    m = _BEST_OF_RE.search(event_title)
    if not m:
        return None
    best_of = int(m.group(1))
    return best_of if best_of in (1, 3, 5) else None


def _base_row(event: dict, m: dict, prices: dict, **extra) -> dict:
    row = {
        "event_slug": event.get("slug", ""),
        "event_title": event.get("title", ""),
        "group_item_title": m.get("groupItemTitle"),
        "outcomes": prices["outcomes"],
        "outcome_prices": prices["outcome_prices"],
        "condition_id": prices["condition_id"],
        "volume": prices["volume"],
        "status": _market_status(m),
        "estimated_start_time": _normalize_start_time(m.get("gameStartTime")),
        "best_of": parse_best_of(event.get("title")),
    }
    row.update(extra)
    return row


def _iter_match_markets(pattern_check, events: list[dict] | None = None):
    """Shared walk: live match events only, non-stale markets only. Yields
    (event, market, prices, pattern_check result).

    `events` is threaded through every getter so a caller can fetch the
    listing ONCE and slice all four market types out of it. Valorant/LoL's
    own clients re-fetch inside each getter; that costs CS2 8 full paginated
    fetches per refresh (4 market types x 2 tag slugs), which this sport
    specifically cannot afford -- run_full_refresh_cs2's own docstring records
    that its Kalshi step already fails to finish inside 2 minutes, and that a
    slow CS2 refresh is what starved the whole sport of price updates for ~8
    days. get_all_markets() below is the entrypoint that does the single
    fetch; the per-type getters keep working standalone for parity with the
    sibling clients and for tests."""
    for event in (get_open_events() if events is None else events):
        if not _is_match_event(event):
            continue
        for m in event.get("markets", []):
            git = (m.get("groupItemTitle") or "").strip()
            checked = pattern_check(git)
            if not checked:
                continue
            if _is_stale(_normalize_start_time(m.get("gameStartTime"))):
                continue
            yield event, m, extract_market_prices(m), checked


def _is_two_sided(prices: dict) -> bool:
    return len(prices["outcomes"]) == 2 and len(prices["outcome_prices"]) == 2


def get_match_winner_markets(events: list[dict] | None = None) -> list[dict]:
    """One row per (event, team) with that team's real name and price -- same
    shape kalshi_cs2_client's rows use for team identity, so
    market_matcher_cs2 can consume either uniformly."""
    rows = []
    for event, m, prices, _ in _iter_match_markets(
        lambda git: git.lower() == "match winner", events
    ):
        if not _is_two_sided(prices):
            continue
        for team_name, price in zip(prices["outcomes"], prices["outcome_prices"]):
            rows.append(_base_row(event, m, prices, team_name=team_name, last_price=price))
    return rows


def get_map_winner_markets(events: list[dict] | None = None) -> list[dict]:
    rows = []
    for event, m, prices, mt in _iter_match_markets(_MAP_WINNER_RE.match, events):
        if not _is_two_sided(prices):
            continue
        map_number = int(mt.group(1))
        for team_name, price in zip(prices["outcomes"], prices["outcome_prices"]):
            rows.append(_base_row(event, m, prices, map_number=map_number, team_name=team_name, last_price=price))
    return rows


def get_total_maps_markets(events: list[dict] | None = None) -> list[dict]:
    """"O/U N.5 Games" -- maps-played total (NOT "Map N Total Rounds", see
    module docstring). Team-less, same "side='over'" convention as every
    other sport's totals here."""
    rows = []
    for event, m, prices, mt in _iter_match_markets(_TOTAL_GAMES_RE.match, events):
        rows.append(_base_row(event, m, prices, line=float(mt.group(1))))
    return rows


def get_map_handicap_markets(events: list[dict] | None = None) -> list[dict]:
    """"Map Handicap: A (-1.5) vs B (+1.5)" -- one market covers BOTH sides of
    the same line (outcomes are the two team names, not Yes/No). Returns one
    row per (event, team) with that team's own handicap line."""
    rows = []
    for event, m, prices, hm in _iter_match_markets(_HANDICAP_RE.match, events):
        if not _is_two_sided(prices):
            continue
        lines = handicap_lines_in_outcome_order(hm, prices["outcomes"])
        if lines is None:
            continue
        for team_name, price, line in zip(prices["outcomes"], prices["outcome_prices"], lines):
            rows.append(_base_row(event, m, prices, team_name=team_name, line=line, last_price=price))
    return rows


def get_all_markets() -> dict[str, list[dict]]:
    """Single-fetch entrypoint the poller uses -- one paginated listing fetch,
    all four market types sliced out of it (see _iter_match_markets on why
    CS2 specifically can't afford the sibling clients' re-fetch-per-getter
    shape)."""
    events = get_open_events()
    return {
        "match_winner": get_match_winner_markets(events),
        "map_winner": get_map_winner_markets(events),
        "total_maps": get_total_maps_markets(events),
        "map_handicap": get_map_handicap_markets(events),
    }
