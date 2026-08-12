"""Live-polling Kalshi client for WNBA markets -- parallel to
kalshi_nba_client.py. Scope is moneyline only (KXWNBAGAME game-winner), the
one WNBA market with a real team-Elo baseline; spread/total/futures are
deferred (moneyline-only integration, see poller_wnba.py).

The All-Star game (e.g. KXWNBAGAME-26JUL25SPNCOO, "Team Coop"/"Team Spoon")
is left in the raw feed and naturally drops out downstream: its ticker blob
("SPNCOO") splits to non-real abbreviations, so the matcher returns no game id
and the Elo has no rating for those pseudo-teams -- no special-casing needed.
"""
import re

from app.clients.base import get_json, paginate, markets_for_event
from app.clients.kalshi_client import get_open_markets_for_series

BASE = "https://api.elections.kalshi.com/trade-api/v2"
MONEYLINE_SERIES = "KXWNBAGAME"
SPREAD_SERIES = "KXWNBASPREAD"
TOTAL_SERIES = "KXWNBATOTAL"
# PER-TEAM total ("Will New York score over 97.5 points?"), added 2026-08-11.
# 54 open markets with a real book -- 4,196 volume, a bid on every leg. Same
# per-team ladder SHAPE as the half spreads, so it reuses _ladder_rows: the team
# is parsed off the ticker suffix ("...-NY98" -> "NY"), NOT off yes_sub_title,
# which here is the whole sentence "New York over 97.5 points".
TEAM_TOTAL_SERIES = "KXWNBATEAMTOTAL"
# Half markets. All six are live with real settled history (528/528/176/282/
# 698/658 settled 2026-08-02), priced by game_lines_wnba's measured half
# constants. The winner series carry no floor_strike (they are "which team wins
# the half", not a threshold), so they use the moneyline fetch shape rather than
# the ladder one.
WIN_TOTAL_SERIES = "KXWNBAWINS"   # season win ladders (45 open, 15 teams)
# Season FINISHING-POSITION markets, one per team. Both resolve on the
# regular-season table, so neither needs a playoff bracket -- see
# season_sim_wnba.standings_probs.
STANDINGS_SERIES = {"one_seed": "KXWNBA1SEED", "playoff_qualifier": "KXWNBAPLAYOFF"}

# Season BRACKET markets, same one-per-team shape but resolving on the playoff
# bracket rather than the table -- priced from season_sim_wnba.bracket_probs,
# whose reseeding rule was recovered from the 2024/25 postseasons.
#
# A previous comment here claimed the championship markets "have 0 open markets
# (checked 2026-08-06)" and named them KXWNBACHAMP/KXWNBAFINALS. Both parts were
# wrong: the real series are KXWNBA / KXWNBAFINAL / KXWNBASEMIFINAL and all
# three are open. The 0-count came from probing series tickers that do not
# exist, which returns empty exactly like a real-but-unlisted series does --
# worth remembering, because "no markets" and "wrong ticker" look identical
# through this API.
BRACKET_SERIES = {
    "championship": "KXWNBA",
    "finals_qualifier": "KXWNBAFINAL",
    "semifinal_qualifier": "KXWNBASEMIFINAL",
}
HALF_WINNER_SERIES = {1: "KXWNBA1HWINNER", 2: "KXWNBA2HWINNER"}
HALF_SPREAD_SERIES = {1: "KXWNBA1HSPREAD", 2: "KXWNBA2HSPREAD"}
HALF_TOTAL_SERIES = {1: "KXWNBA1HTOTAL", 2: "KXWNBA2HTOTAL"}

# QUARTER markets, added 2026-08-11. Twelve series, same three shapes as the
# halves, all confirmed live: winner carries no floor_strike and DOES carry a
# TIE outcome, spread is a per-team ladder, total is a game-level ladder.
# Volume is concentrated in the third quarter (3Q total 62,144, 3Q winner
# 31,744, 3Q spread 24,909) rather than spread evenly.
QUARTER_WINNER_SERIES = {q: f"KXWNBA{q}QWINNER" for q in (1, 2, 3, 4)}
QUARTER_SPREAD_SERIES = {q: f"KXWNBA{q}QSPREAD" for q in (1, 2, 3, 4)}
QUARTER_TOTAL_SERIES = {q: f"KXWNBA{q}QTOTAL" for q in (1, 2, 3, 4)}

# Spread market tickers glue the team code to a rung index with no separator
# (confirmed live 2026-08-02: "KXWNBASPREAD-26AUG03PHXCHI-PHX7" = Phoenix, and
# "...-CHI7" = Chicago, both on the same event). Splitting on the letter/digit
# boundary resolves the team without needing a WNBA abbreviation table, unlike
# the NBA client's prefix-matching approach.
_SPREAD_SUFFIX_RE = re.compile(r"^([A-Z]+)\d+$")


def get_open_events(series_ticker: str = MONEYLINE_SERIES) -> list[dict]:
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


def get_moneyline_markets(series: str = MONEYLINE_SERIES) -> list[dict]:
    """Flat list of dicts, one per team-side market -- same row shape as
    kalshi_nba_client.get_moneyline_markets so the catalog upsert mirrors."""
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


def _ladder_rows(series: str, with_team: bool) -> list[dict]:
    """Shared spread/total ladder fetch. Uses the BULK per-series markets call
    (see kalshi_client.get_open_markets_for_series) rather than one request per
    event -- the per-event pattern is what stalled the cs2 refresh with Kalshi
    429s (fixed 2026-08-02), so WNBA is built without inheriting it.

    Row shape mirrors kalshi_nba_client's ladder rows so the catalog upsert can
    follow the NBA one. `line` comes from floor_strike, which Kalshi populates
    directly on both series (confirmed live: spread "greater/6.5", total
    "greater/186.5")."""
    events = {ev["event_ticker"]: ev for ev in get_open_events(series)}
    rows = []
    for m in get_open_markets_for_series(series):
        ev_ticker = m.get("event_ticker")
        if ev_ticker not in events or m.get("floor_strike") is None:
            continue
        row = {
            "event_ticker": ev_ticker,
            "event_title": events[ev_ticker].get("title", ""),
            "ticker": m["ticker"],
            "line": float(m["floor_strike"]),
            "yes_bid": _to_float(m.get("yes_bid_dollars")),
            "yes_ask": _to_float(m.get("yes_ask_dollars")),
            "last_price": _to_float(m.get("last_price_dollars")),
            "volume": _to_float(m.get("volume_fp")),
            "status": m.get("status"),
        }
        if with_team:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            match = _SPREAD_SUFFIX_RE.match(suffix)
            if not match:
                continue  # unparseable team code -> skip rather than guess
            row["team_abbr_kalshi"] = match.group(1)
        rows.append(row)
    return rows


def get_spread_markets() -> list[dict]:
    """Per-TEAM ladder: "<Team> wins the game by over X.5 points?"."""
    return _ladder_rows(SPREAD_SERIES, with_team=True)


def get_total_markets() -> list[dict]:
    """Game-level ladder: "Over X.5 points scored" (no team side)."""
    return _ladder_rows(TOTAL_SERIES, with_team=False)


def get_team_total_markets() -> list[dict]:
    """Per-TEAM ladder: "Will <Team> score over X.5 points?"."""
    return _ladder_rows(TEAM_TOTAL_SERIES, with_team=True)


def get_half_winner_markets(half: int) -> list[dict]:
    """Per-TEAM: "which team wins the Nth half". Same shape as the game
    moneyline, so it reuses that fetch."""
    return get_moneyline_markets(HALF_WINNER_SERIES[half])


def get_half_spread_markets(half: int) -> list[dict]:
    """Per-TEAM ladder: "<Team> wins the Nth half by over X.5 points?"."""
    return _ladder_rows(HALF_SPREAD_SERIES[half], with_team=True)


def get_half_total_markets(half: int) -> list[dict]:
    """Game-level ladder: "Over X.5 points in the Nth half"."""
    return _ladder_rows(HALF_TOTAL_SERIES[half], with_team=False)


def get_quarter_winner_markets(quarter: int) -> list[dict]:
    """Per-TEAM: which team wins the Nth quarter. Same shape as the game
    moneyline (and the half winner), so it reuses that fetch. The TIE leg comes
    back as its own row and MUST be refused downstream -- a quarter ends level
    far more often than a half does."""
    return get_moneyline_markets(QUARTER_WINNER_SERIES[quarter])


def get_quarter_spread_markets(quarter: int) -> list[dict]:
    """Per-TEAM ladder: "<Team> wins the Nth quarter by over X.5 points?"."""
    return _ladder_rows(QUARTER_SPREAD_SERIES[quarter], with_team=True)


def get_quarter_total_markets(quarter: int) -> list[dict]:
    """Game-level ladder: "Over X.5 points in the Nth quarter"."""
    return _ladder_rows(QUARTER_TOTAL_SERIES[quarter], with_team=False)


def get_win_total_markets() -> list[dict]:
    """Season win ladders. Team comes from the EVENT ticker suffix
    ("KXWNBAWINS-26CONN" -> "CONN", after stripping the two-digit season) --
    yes_sub_title here is "20+ wins", not a team, so there is no name to fall
    back on. The suffix is a KALSHI abbreviation, so callers must map it through
    to_espn_abbr (CONN -> CON, PDX -> POR) before matching a team.

    floor_strike is an INTEGER here (20 means 20+), matching CFB's win ladders
    rather than the soccer points ladders' N-0.5."""
    out = []
    for m in get_open_markets_for_series(WIN_TOTAL_SERIES):
        ev = m.get("event_ticker") or ""
        floor = m.get("floor_strike")
        if not ev or not m.get("ticker") or floor is None:
            continue
        abbr = re.sub(r"^\d+", "", ev.rsplit("-", 1)[-1])
        if not abbr:
            continue
        out.append({
            "event_ticker": ev,
            "ticker": m["ticker"],
            "team_abbr_kalshi": abbr,
            "line": float(floor),
            "yes_bid": _to_float(m.get("yes_bid_dollars")),
            "yes_ask": _to_float(m.get("yes_ask_dollars")),
            "last_price": _to_float(m.get("last_price_dollars")),
            "volume": _to_float(m.get("volume_fp")),
            "status": m.get("status"),
        })
    return out


def get_standings_markets() -> list[dict]:
    """Season team markets, one row per team: #1 seed, playoff qualifier, and
    the three bracket outcomes (championship / finals / semifinals).

    All five share one shape, which is why they share one fetcher -- the team
    lives in the MARKET ticker suffix ("KXWNBA1SEED-26-WSH" -> "WSH"), not the
    event ticker, and there is no floor_strike; they are plain yes/no
    propositions. What differs is only what PRICES them: the first two resolve
    on the regular-season table, the last three on the playoff bracket. The
    suffix is a KALSHI abbreviation, so callers map it through to_espn_abbr
    before matching a team.

    KXWNBA is a bare series ticker, so its markets read "KXWNBA-26-WSH" -- the
    same rsplit still yields the team, and the digit guard below still rejects
    a suffix that is a year rather than a team.
    """
    out = []
    for market_kind, series in {**STANDINGS_SERIES, **BRACKET_SERIES}.items():
        for m in get_open_markets_for_series(series):
            ticker = m.get("ticker")
            if not ticker:
                continue
            abbr = ticker.rsplit("-", 1)[-1]
            if not abbr or abbr.isdigit():
                continue
            out.append({
                "event_ticker": m.get("event_ticker") or series,
                "ticker": ticker,
                "team_abbr_kalshi": abbr,
                "market_kind": market_kind,
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "last_price": _to_float(m.get("last_price_dollars")),
                "volume": _to_float(m.get("volume_fp")),
                "status": m.get("status"),
            })
    return out
