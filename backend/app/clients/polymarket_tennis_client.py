"""Live-polling Polymarket client for Tennis moneyline markets. Parallel to
polymarket_mma_client.py.

Confirmed live 2026-07-18: `series_slug=atp`, `wta`, `itf` each list open
per-match events (12/11/24 open the day of the live audit). Real structural
findings, different from Kalshi's split-by-tier series:
  - Polymarket's `atp`/`wta` slugs bundle BOTH tour-level AND Challenger-tier
    matches together (e.g. "Cordenons: Fule vs Bondioli" -- a real Challenger
    event -- appeared under plain `series_slug=atp`, no separate Challenger
    slug exists on this platform). tier is therefore NOT derivable from the
    slug alone here -- callers needing tier (e.g. cross-checking against
    Kalshi's split) would have to infer it from tournament naming
    conventions; the live matcher doesn't need tier at all (see
    market_matcher_tennis.py), so this is left unresolved rather than guessed.
  - `itf` combines men's AND women's matches into ONE series_slug (unlike
    Kalshi's KXITFMATCH/KXITFWMATCH gender split) -- confirmed live via real
    examples of both in the same catalog pull.
  - Each event bundles a rich set of markets (moneyline + per-set winner +
    set/match totals + set handicap, confirmed live: 16 markets for one real
    Challenger match) as sibling `markets` entries, same "one event, many
    markets" shape as this app's NFL/MLB per-game bundling. The moneyline
    market is identified by `groupItemTitle is None` (every other market
    type in the bundle has a real, non-null groupItemTitle) -- confirmed via
    a live event dump, not assumed.

Set winner, game total (match + per-set), set handicap (game spread), and
total sets are all also built here (confirmed live via a real full bundle
dump, 2026-07-18): "Set N Winner" (outcomes are player names), "Match O/U
X.5" (match-level game total, same concept as Kalshi's KXATPGTOTAL), "Set N
O/U X.5" (PER-SET game total -- no Kalshi equivalent at all, Kalshi's total
is match-level only), "Set Handicap +/-1.5" (confirmed via a real resolved
example to be a GAMES differential for the whole match, same concept as
Kalshi's KXATPGSPREAD, despite the "Set" in its name -- always a single
+/-1.5 line, not a ladder of many lines like Kalshi's version), and "Total
Sets: O/U X.5" (whether the match goes the full distance in set count --
no Kalshi equivalent). "Completed Match" (Yes/No) is a resolution helper,
not a real tradeable proposition, and is skipped.
"""
import re

from app.clients.base import paginate
from app.clients.polymarket_client import extract_market_prices

GAMMA = "https://gamma-api.polymarket.com"

SERIES_SLUGS = ("atp", "wta", "itf")


def get_open_events(series_slug: str, limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?series_slug={series_slug}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def _market_status(m: dict) -> str:
    """REAL BUG this fixes (caught live 2026-07-19, user-reported near-0%
    prices on the Recommended tab): the EVENT-level `closed` flag (what
    get_open_events already filters on via closed=false) does NOT guarantee
    every market INSIDE that event bundle is still open -- a real confirmed
    example (event "Geneva Open: Norrie vs Navone", event closed=false,
    months after the real match ended) had every one of its OWN markets
    individually closed=true, with last_price frozen at $0.00/$1.00 forever.
    Ingesting that dead price as a live "market price" produced a nonsensical
    huge edge against the model's own live probability estimate. Kalshi
    already exposes this correctly per-market (`status`, e.g. "finalized" --
    see kalshi_tennis_client.py); this is Polymarket's equivalent signal,
    checked per-MARKET here rather than trusting the event's own flag."""
    if m.get("closed") or not m.get("active", True):
        return "closed"
    return "active"


def _normalize_start_time(raw) -> str | None:
    """Polymarket's per-market `gameStartTime` (confirmed live 2026-07-19,
    e.g. "2026-05-20 09:05:00+00") uses a space separator and a bare "+00"
    UTC offset -- not reliably parsed as an instant by every JS Date
    implementation. Normalized to a real ISO 8601 instant here rather than
    left for the frontend to guess at."""
    if not raw:
        return None
    text = str(raw).strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "Z"
    return text


def _resolve_full_name(partial: str, event: dict) -> str:
    """REAL BUG this guards against (caught live, same class of bug
    polymarket_mma_client.py::_resolve_full_fighter_name already found for
    MMA): set winner/set handicap markets only carry a partial name
    ("Navone", not "Mariano Navone" -- confirmed live 2026-07-18), which
    won't match TennisMatch's full-name rows. Resolves it against THIS SAME
    event's own moneyline outcomes (the two real full names) by suffix
    match -- cheap and reliable since an event only ever has 2 players."""
    for m in event.get("markets", []):
        if m.get("groupItemTitle") is not None:
            continue
        prices = extract_market_prices(m)
        outcomes = prices["outcomes"]
        if len(outcomes) != 2 or outcomes in (["Yes", "No"], ["Over", "Under"]):
            continue
        for full_name in outcomes:
            if full_name.lower().endswith(partial.lower()):
                return full_name
    return partial  # no moneyline match found -- fall back to the partial name rather than dropping the row


def get_moneyline_markets() -> list[dict]:
    """One row per (event, player) -- moneyline market identified by
    groupItemTitle being null (see module docstring)."""
    rows = []
    for series_slug in SERIES_SLUGS:
        for event in get_open_events(series_slug):
            for m in event.get("markets", []):
                if m.get("groupItemTitle") is not None:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if len(outcomes) != 2 or len(outcome_prices) != 2:
                    continue
                if outcomes in (["Yes", "No"], ["Over", "Under"]):
                    continue
                for player_name, price in zip(outcomes, outcome_prices):
                    rows.append({
                        "event_slug": event.get("slug", ""),
                        "event_title": event.get("title", ""),
                        "series_slug": series_slug,
                        "player_name": player_name,
                        "last_price": price,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                        "status": _market_status(m),
                        "estimated_start_time": _normalize_start_time(m.get("gameStartTime")),
                    })
    return rows


_SET_WINNER_RE = re.compile(r"^Set (\d) Winner:")
_SET_GAMES_RE = re.compile(r"^.+?:\s*Set (\d) Games O/U ([\d.]+)$")
_MATCH_TOTAL_RE = re.compile(r"^.+?:\s*Match O/U ([\d.]+)$")
_SET_HANDICAP_RE = re.compile(r"^Set Handicap:\s*(.+?)\s*\(([+-][\d.]+)\)\s*vs\s*(.+?)\s*\(([+-][\d.]+)\)$")
_TOTAL_SETS_RE = re.compile(r"^.+?:\s*Total Sets O/U ([\d.]+)$")


def get_set_winner_markets() -> list[dict]:
    """One row per (event, set_number, player) -- both sets' winner markets
    live in the SAME event bundle (unlike Kalshi's one-event-per-set
    structure), so no extra per-set event lookup is needed here."""
    rows = []
    for series_slug in SERIES_SLUGS:
        for event in get_open_events(series_slug):
            for m in event.get("markets", []):
                match = _SET_WINNER_RE.match(m.get("question", ""))
                if not match:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if len(outcomes) != 2 or len(outcome_prices) != 2:
                    continue
                for player_name, price in zip(outcomes, outcome_prices):
                    rows.append({
                        "event_slug": event.get("slug", ""),
                        "set_number": int(match.group(1)),
                        "player_name": _resolve_full_name(player_name, event),
                        "last_price": price,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                        "status": _market_status(m),
                    })
    return rows


def get_set_game_total_markets() -> list[dict]:
    """PER-SET game total (e.g. "Set 1 Games O/U 8.5") -- no Kalshi
    equivalent, Kalshi's game total is match-level only."""
    rows = []
    for series_slug in SERIES_SLUGS:
        for event in get_open_events(series_slug):
            for m in event.get("markets", []):
                match = _SET_GAMES_RE.match(m.get("question", ""))
                if not match:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Over", "Under"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""),
                    "set_number": int(match.group(1)),
                    "line": float(match.group(2)),
                    "over_price": outcome_prices[0],
                    "condition_id": prices["condition_id"],
                    "status": _market_status(m),
                })
    return rows


def get_match_total_markets() -> list[dict]:
    """MATCH-level game total (e.g. "Match O/U 21.5") -- same concept as
    Kalshi's KXATPGTOTAL."""
    rows = []
    for series_slug in SERIES_SLUGS:
        for event in get_open_events(series_slug):
            for m in event.get("markets", []):
                if _SET_GAMES_RE.match(m.get("question", "")):
                    continue  # avoid double-matching a per-set total as a match total
                match = _MATCH_TOTAL_RE.match(m.get("question", ""))
                if not match:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if outcomes != ["Over", "Under"] or len(outcome_prices) != 2:
                    continue
                rows.append({
                    "event_slug": event.get("slug", ""),
                    "line": float(match.group(1)),
                    "over_price": outcome_prices[0],
                    "condition_id": prices["condition_id"],
                    "status": _market_status(m),
                })
    return rows


def get_set_handicap_markets() -> list[dict]:
    """"Set Handicap +/-1.5" -- confirmed live to be a GAMES differential
    for the whole match (same concept as Kalshi's KXATPGSPREAD), always a
    single +/-1.5 line (not a ladder like Kalshi's version). Both rows
    (favorite's -1.5 perspective and underdog's +1.5 perspective) are
    returned -- the poller upserts both, same "one market per side" pattern
    as every other real spread market in this app."""
    rows = []
    for series_slug in SERIES_SLUGS:
        for event in get_open_events(series_slug):
            for m in event.get("markets", []):
                match = _SET_HANDICAP_RE.match(m.get("question", ""))
                if not match:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if len(outcomes) != 2 or len(outcome_prices) != 2:
                    continue
                name_a, line_a, name_b, line_b = match.groups()
                lines_by_name = {name_a: float(line_a), name_b: float(line_b)}
                for player_name, price in zip(outcomes, outcome_prices):
                    line = lines_by_name.get(player_name)
                    if line is None:
                        continue
                    rows.append({
                        "event_slug": event.get("slug", ""),
                        "player_name": _resolve_full_name(player_name, event),
                        "line": line,
                        "last_price": price,
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                        "status": _market_status(m),
                    })
    return rows


def get_total_sets_markets() -> list[dict]:
    """"Total Sets: O/U X.5" -- whether the match goes the full distance in
    SET count. No Kalshi equivalent."""
    rows = []
    for series_slug in SERIES_SLUGS:
        for event in get_open_events(series_slug):
            for m in event.get("markets", []):
                match = _TOTAL_SETS_RE.match(m.get("question", ""))
                if not match:
                    continue
                prices = extract_market_prices(m)
                outcomes, outcome_prices = prices["outcomes"], prices["outcome_prices"]
                if len(outcome_prices) != 2:
                    continue
                over_price = outcome_prices[0] if "Over" in outcomes[0] else outcome_prices[1]
                rows.append({
                    "event_slug": event.get("slug", ""),
                    "line": float(match.group(1)),
                    "over_price": over_price,
                    "condition_id": prices["condition_id"],
                    "status": _market_status(m),
                })
    return rows
