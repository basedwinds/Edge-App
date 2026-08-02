"""Live-polling Polymarket client for NFL markets.

Unlike the historical backtest approach in
Downloads/ufc-kalshi-polymarket/polymarket_lib.py (which reconstructs price from
/trades because it needs a price *as of* a past timestamp), a live app can read
current price directly off each Gamma API market object (bestBid/bestAsk/
lastTradePrice/outcomePrices) -- confirmed present on real market objects
2026-07-14. No trades API needed for the live path.

Confirmed 2026-07-14: individual per-game NFL moneyline markets are NOT yet
listed on Polymarket for the upcoming season (only season-long futures/props
under tag_slug=nfl) -- unlike Kalshi's KXNFLGAME, which already lists games
months ahead. Per-game markets likely appear closer to each game week. The
poller must tolerate Polymarket-side games being absent even when the same
game already has a Kalshi market.
"""
import json
import re

from app.clients.base import get_json, paginate
from app.ingestion.market_matcher import POLYMARKET_MASCOT_TO_NFLVERSE_ABBR

_MASCOT_LOOKUP = list(POLYMARKET_MASCOT_TO_NFLVERSE_ABBR.keys())

GAMMA = "https://gamma-api.polymarket.com"

NFL_TAG_SLUG = "nfl"

# Confirmed live 2026-07-15 by paging every open NFL-tagged event (78 total)
# and reading titles/slugs directly -- the rest of that catalog is player
# props, awards, trade/retirement speculation, and narrative markets (Taylor
# Swift's wedding, Madden cover athlete, ...) this app's team-level Elo
# model has no way to price, same reasoning as the Kalshi side (see
# kalshi_client.py::FUTURES_SERIES). Hardcoded exact slugs rather than
# title/pattern matching -- more robust than fuzzy-matching against a
# catalog dominated by narrative markets that could coincidentally match a
# loose pattern. No Polymarket equivalent of Kalshi's 1-seed market was
# found in that catalog -- gracefully absent, same "poller tolerates one
# platform missing a market the other has" pattern as moneyline.
FUTURES_EVENT_SLUGS = {
    "division_winner": [
        "pro-football-afc-east-champion", "pro-football-afc-north-champion",
        "pro-football-afc-south-champion", "pro-football-afc-west-champion",
        "pro-football-nfc-east-champion", "pro-football-nfc-north-champion",
        "pro-football-nfc-south-champion", "pro-football-nfc-west-champion",
    ],
    "conference_champion": ["pro-football-2027-afc-champion", "pro-football-2027-nfc-champion"],
    "super_bowl_champion": ["big-game-champion-2027"],
    "playoff_qualifier": ["nfl-team-to-make-postseason"],
}


def get_open_nfl_events(limit: int = 100) -> list[dict]:
    def url_builder(offset):
        return f"{GAMMA}/events?tag_slug={NFL_TAG_SLUG}&closed=false&limit={limit}&offset={offset}"

    return paginate(url_builder, list_key=None, limit=limit, cursor_style="offset")


def is_game_market(event: dict) -> bool:
    """Heuristic: per-game markets are titled like 'Team A vs. Team B' or
    similar head-to-head phrasing, as opposed to season-long futures/props."""
    title = (event.get("title") or "").lower()
    return " vs" in title or " at " in title


def get_futures_markets() -> list[dict]:
    """Returns a flat list of dicts, one per team-side futures sub-market,
    tagged with market_kind (division_winner/conference_champion/
    super_bowl_champion/playoff_qualifier, matching kalshi_client.py's
    naming) and a human-readable group_label taken from the event's own
    title. Fetches each event by slug directly (not the paginated tag
    listing) since the exact slug set is already known."""
    rows = []
    for kind, slugs in FUTURES_EVENT_SLUGS.items():
        for slug in slugs:
            try:
                event = get_json(f"{GAMMA}/events/slug/{slug}")
            except Exception:
                continue
            group_label = event.get("title", "")
            for m in event.get("markets", []):
                team_name = m.get("groupItemTitle")
                if not team_name or team_name == "Other":
                    continue
                prices = extract_market_prices(m)
                outcomes = prices["outcomes"]
                outcome_prices = prices["outcome_prices"]
                if "Yes" not in outcomes or not outcome_prices:
                    continue
                yes_idx = outcomes.index("Yes")
                yes_price = outcome_prices[yes_idx] if yes_idx < len(outcome_prices) else None
                rows.append(
                    {
                        "market_kind": kind,
                        "event_slug": slug,
                        "group_label": group_label,
                        "team_full_name": team_name,
                        "yes_price": yes_price,
                        "condition_id": prices["condition_id"],
                        "slug": prices["slug"],
                        "question": prices["question"],
                        "volume": prices["volume"],
                    }
                )
    return rows


UNDEFEATED_SEASON_SLUG = "pro-football-undefeated-regular-season"


def get_undefeated_market() -> dict | None:
    """Single LEAGUE-WIDE binary market ("will any team finish undefeated"),
    not team-keyed like every other futures market above (its one market's
    `groupItemTitle` is None, so it's deliberately NOT folded into
    get_futures_markets -- that function skips team_name-less markets by
    design). Found while auditing Kalshi for an undefeated-season
    equivalent (2026-07-16); no Kalshi equivalent exists."""
    try:
        event = get_json(f"{GAMMA}/events/slug/{UNDEFEATED_SEASON_SLUG}")
    except Exception:
        return None
    markets = event.get("markets", [])
    if not markets:
        return None
    prices = extract_market_prices(markets[0])
    outcomes = prices["outcomes"]
    outcome_prices = prices["outcome_prices"]
    if "Yes" not in outcomes or not outcome_prices:
        return None
    yes_idx = outcomes.index("Yes")
    return {
        "event_slug": UNDEFEATED_SEASON_SLUG,
        "group_label": event.get("title", ""),
        "yes_price": outcome_prices[yes_idx],
        "condition_id": prices["condition_id"],
        "slug": prices["slug"],
        "question": prices["question"],
        "volume": prices["volume"],
    }


_ANON_CANDIDATE_RE = re.compile(r"^(Person|Player|QB)\s+[A-Z]{1,2}$")


def is_anonymous_candidate(name: str) -> bool:
    """Polymarket pre-allocates anonymous placeholder outcomes ("Person B",
    "QB A", "Player C", ...) on multi-outcome categorical markets like
    Week-1-starting-QB, for candidates it hasn't named yet. No signal exists
    to associate these with anything real -- roster_change_rules.py-style
    "unknown = no adjustment" convention, not a guess."""
    return bool(_ANON_CANDIDATE_RE.match(name.strip())) or name.strip() == "Other"


def get_week1_qb_markets() -> list[dict]:
    """Team-specific 'Week 1 Starting QB' categorical markets -- discovered
    dynamically from the open NFL event list (title-matched, not hardcoded
    slugs) rather than FUTURES_EVENT_SLUGS's fixed-list pattern, since
    Polymarket only opens this market for teams with genuine QB competition
    (confirmed live 2026-07-16: 5 of 32 teams -- KC, LV, CLE, NYJ, MIN -- not
    all 32), so the team set isn't stable enough to hardcode."""
    events = get_open_nfl_events()
    qb_events = [e for e in events if "starting qb" in (e.get("title") or "").lower()]

    rows = []
    for event in qb_events:
        team_full_name = None
        title = event.get("title") or ""
        for mascot in _MASCOT_LOOKUP:
            if mascot.lower() in title.lower():
                team_full_name = mascot
                break
        if team_full_name is None:
            continue
        for m in event.get("markets", []):
            candidate_name = (m.get("groupItemTitle") or "").strip()
            if not candidate_name or is_anonymous_candidate(candidate_name):
                continue
            prices = extract_market_prices(m)
            outcomes = prices["outcomes"]
            outcome_prices = prices["outcome_prices"]
            if "Yes" not in outcomes or not outcome_prices:
                continue
            yes_idx = outcomes.index("Yes")
            rows.append(
                {
                    "event_slug": event.get("slug", ""),
                    "group_label": title,
                    "team_full_name": team_full_name,
                    "candidate_name": candidate_name,
                    "yes_price": outcome_prices[yes_idx] if yes_idx < len(outcome_prices) else None,
                    "condition_id": prices["condition_id"],
                    "slug": prices["slug"],
                    "question": prices["question"],
                    "volume": prices["volume"],
                }
            )
    return rows


MVP_SLUG = "pro-football-2026-mvp-winner"


def get_mvp_markets() -> list[dict]:
    """Single known-slug event, same fetch pattern as get_undefeated_market
    -- confirmed live 2026-07-16, real named candidates (mostly starting QBs,
    a few elite RBs) plus Polymarket's usual anonymous placeholder slots
    (filtered via is_anonymous_candidate, same as get_week1_qb_markets)."""
    try:
        event = get_json(f"{GAMMA}/events/slug/{MVP_SLUG}")
    except Exception:
        return []
    rows = []
    for m in event.get("markets", []):
        candidate_name = (m.get("groupItemTitle") or "").strip()
        if not candidate_name or is_anonymous_candidate(candidate_name):
            continue
        prices = extract_market_prices(m)
        outcomes = prices["outcomes"]
        outcome_prices = prices["outcome_prices"]
        if "Yes" not in outcomes or not outcome_prices:
            continue
        yes_idx = outcomes.index("Yes")
        rows.append(
            {
                "event_slug": MVP_SLUG,
                "group_label": event.get("title", ""),
                "candidate_name": candidate_name,
                "yes_price": outcome_prices[yes_idx] if yes_idx < len(outcome_prices) else None,
                "condition_id": prices["condition_id"],
                "slug": prices["slug"],
                "question": prices["question"],
                "volume": prices["volume"],
            }
        )
    return rows


_SIGNED_NUMBER_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)")


def _parse_signed_number(text: str) -> float | None:
    m = _SIGNED_NUMBER_RE.search(text or "")
    return float(m.group(1)) if m else None


def get_spread_total_markets(game_like_events: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (spread_rows, total_rows). Polymarket bundles spread/total as
    EXTRA markets within the same per-game event object (index 1+, after
    the moneyline market at index 0) -- confirmed live via a currently-
    active MLB event (e.g. "mlb-tb-bos-2026-05-09": index 0 moneyline,
    index 2 "Spread: Boston Red Sox (-1.5)", indexes 3/4 two different O/U
    lines). NFL's own per-game events aren't open yet this far before the
    season, so this exact bundle shape couldn't be verified against a live
    NFL example -- worth a quick sanity check once real games open.
    Classified via each market's own `groupItemTitle` field rather than a
    fixed index, so this degrades gracefully if NFL's bundle differs (e.g.
    no MLB-specific "NRFI"-style market, or a different market count).

    Spread rows carry BOTH sides as separate rows (favorite: line = points
    that team must win by; underdog: negative line, i.e. "wins by more
    than -X" = doesn't lose by X or more) -- same "wins by more than line"
    convention app/models/game_lines.py already uses for Kalshi's ladder,
    so both platforms feed the same probability model with no special-casing.
    """
    if game_like_events is None:
        events = get_open_nfl_events()
        game_like_events = [e for e in events if is_game_market(e)]

    spread_rows: list[dict] = []
    total_rows: list[dict] = []
    for event in game_like_events:
        slug = event.get("slug", "")
        for m in event.get("markets", [])[1:]:  # index 0 is moneyline, handled separately
            group_item = (m.get("groupItemTitle") or "").strip()
            prices = extract_market_prices(m)
            outcomes = prices["outcomes"]
            outcome_prices = prices["outcome_prices"]
            if len(outcomes) != 2 or len(outcome_prices) != 2:
                continue

            if group_item.startswith("Spread"):
                line = _parse_signed_number(group_item) or _parse_signed_number(prices["question"])
                if line is None:
                    continue
                named_team, other_team = outcomes[0], outcomes[1]
                spread_rows.append(
                    {
                        "event_slug": slug,
                        "team_full_name": named_team,
                        "line": -line,
                        "last_price": outcome_prices[0],
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
                spread_rows.append(
                    {
                        "event_slug": slug,
                        "team_full_name": other_team,
                        "line": line,
                        "last_price": outcome_prices[1],
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
            elif group_item.startswith("O/U"):
                line = _parse_signed_number(group_item)
                if line is None or "Over" not in outcomes:
                    continue
                over_idx = outcomes.index("Over")
                under_idx = 1 - over_idx
                total_rows.append(
                    {
                        "event_slug": slug,
                        "side": "over",
                        "line": line,
                        "last_price": outcome_prices[over_idx],
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
                total_rows.append(
                    {
                        "event_slug": slug,
                        "side": "under",
                        "line": line,
                        "last_price": outcome_prices[under_idx],
                        "condition_id": prices["condition_id"],
                        "volume": prices["volume"],
                    }
                )
    return spread_rows, total_rows


def _extract_volume(market: dict) -> float | None:
    """REAL BUG this fixes (caught live 2026-07-19, tennis discrepancy audit):
    every Polymarket row in this app has always stored volume=None, on the
    documented assumption that "Polymarket's API never exposes real volume".
    That assumption was wrong -- confirmed live against the actual Gamma API
    response for a real event: the per-MARKET object (not just the event)
    carries a real `volumeNum` (float, e.g. 231112.10) and a redundant string
    `volume`. `extract_market_prices` simply never read it. Prefers
    `volumeNum` (already a float); falls back to parsing the `volume` string
    for older/partial responses."""
    if market.get("volumeNum") is not None:
        return market["volumeNum"]
    raw = market.get("volume")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def handicap_lines_in_outcome_order(handicap_match, outcomes: list[str]) -> list[float] | None:
    """Maps the two lines parsed out of an esports handicap title
    ("Map Handicap: A (-1.5) vs B (+1.5)", "Game Handicap: ...") onto that
    market's own outcome order. Shared by the CS2/Valorant/LoL Polymarket
    clients -- `handicap_match` is any regex match whose 4 groups are
    (team_1, line_1, team_2, line_2).

    REAL BUG this fixes (found live 2026-08-02 while building
    polymarket_cs2_client.py, then confirmed already live in the two older
    clients): the handicap TITLE names teams by ABBREVIATION while `outcomes`
    names them in full -- "Map Handicap: FNC (-1.5) vs Lilmix (+1.5)" against
    outcomes ["fnatic", "Lilmix"]; likewise TS/Spirit, BST/BESTIA,
    AG/"All Gamers", NS/"Nongshim RedForce", AL/"Anyone's Legend".  Both
    older clients looked the line up by exact team name and silently
    `continue`d on a miss, so they returned a small fraction of the real
    market instead of erroring.  Measured live on real open inventory:
    CS2 dropped 35/48 handicap markets (73%), Valorant 16/17 (94%), LoL
    97/103 (94%).

    Falls back to POSITION when names don't resolve, safe for two
    independently-verified reasons: across all three sports, title order
    matched outcome order on every market whose names DO resolve (20/20,
    zero disagreements), and the two lines are always exact negatives on
    real inventory (157/157 markets: -1.5/+1.5 or -2.5/+2.5).  The negation
    is enforced as a GUARD rather than assumed -- a pair that isn't an exact
    negation returns None so the caller skips that market rather than
    inventing a line for it."""
    team_1, raw_1, team_2, raw_2 = handicap_match.groups()
    line_1, line_2 = float(raw_1), float(raw_2)
    if team_1 in outcomes and team_2 in outcomes and team_1 != team_2:
        by_team = {team_1: line_1, team_2: line_2}
        return [by_team[o] for o in outcomes]
    if line_1 != -line_2:
        return None
    return [line_1, line_2]


def extract_market_prices(market: dict) -> dict:
    outcomes = json.loads(market.get("outcomes", "[]") or "[]")
    prices = json.loads(market.get("outcomePrices", "[]") or "[]")
    return {
        "outcomes": outcomes,
        "outcome_prices": [float(p) for p in prices] if prices else [],
        "best_bid": market.get("bestBid"),
        "best_ask": market.get("bestAsk"),
        "last_trade_price": market.get("lastTradePrice"),
        "volume": _extract_volume(market),
        "condition_id": market.get("conditionId"),
        "slug": market.get("slug"),
        "question": market.get("question"),
    }
