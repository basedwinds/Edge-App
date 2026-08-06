"""Live-polling Kalshi client for MLB markets. Parallel to
kalshi_nba_client.py, same architecture-decision reasoning as
market_matcher_mlb.py.

Series tickers confirmed live against the public API 2026-07-17 (mid-season,
so nearly everything below is open right now, unlike NFL/NBA which had to
wait for their seasons to start):
  KXMLBGAME    - moneyline (45 open events)
  KXMLBSPREAD  - run-line, ladder-structured like NFL/NBA spread (15 open)
  KXMLBTOTAL   - total runs, ladder (15 open)
  KXMLBTEAMTOTAL - per-team total runs, ladder (15 open)
  Futures (all confirmed open now): KXMLB (World Series champion, NOT
  KXMLBWS -- that ticker exists but has 0 open events, a dead/unused
  ticker), KXMLBAL/KXMLBNL (league champion), 6 division-winner series,
  KXMLBWINS-{team} (30 per-team win-total series), KXMLBBESTRECORD/
  KXMLBWORSTRECORD, KXMLBPLAYOFFS.

F5 (KXMLBF5, 3-way incl. TIE) and RFI (KXMLBRFI, binary) built 2026-07-17 --
see get_f5_markets/get_rfi_markets below and game_lines_mlb.py's F5/RFI
probability functions for the real derived constants. Still explicitly NOT
built: KXMLBF3/KXMLBF7 (first-3/7-innings winner -- same 3-way shape as F5,
just a different inning cut, not built since F5 is the one both platforms
actually price), KXMLBEXTRAS (extra innings), half-line-equivalents.
Player-level props/awards/leaderboards stay out of scope for the same
"different kind of model" reason as NFL/NBA.
"""
from app.clients.base import get_json, paginate
from app.ingestion.market_matcher_mlb import parse_kalshi_event_ticker, split_teams_blob
from app.ingestion.mlb_data import team_abbreviations

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = "KXMLBGAME"
SPREAD_SERIES = "KXMLBSPREAD"
TOTAL_SERIES = "KXMLBTOTAL"
TEAM_TOTAL_SERIES = "KXMLBTEAMTOTAL"
F5_SERIES = "KXMLBF5"
RFI_SERIES = "KXMLBRFI"

FUTURES_SERIES = {
    "conference_champion": ["KXMLBAL", "KXMLBNL"],  # "conference" naming kept for API/schema parity with NFL/NBA; these are the AL/NL pennant winners
    "division_winner": [
        "KXMLBALEAST", "KXMLBALCENT", "KXMLBALWEST",
        "KXMLBNLEAST", "KXMLBNLCENT", "KXMLBNLWEST",
    ],
    "championship": ["KXMLB"],  # World Series -- KXMLBWS exists but is a dead ticker, confirmed 0 open events live
    "playoff_qualifier": ["KXMLBPLAYOFFS"],
    "best_record": ["KXMLBBESTRECORD"],
    "worst_record": ["KXMLBWORSTRECORD"],
}

WIN_TOTAL_SERIES_PREFIX = "KXMLBWINS-"
# World Series MATCHUP -- one market per possible AL/NL pairing (225 open =
# 15 x 15). Priced from the joint (AL champ, NL champ) counts the season sim
# tallies per trial, NOT from multiplying two pennant probabilities.
WS_MATCHUP_SERIES = "KXTEAMSINWS"


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


def get_moneyline_markets() -> list[dict]:
    events = get_open_events(MONEYLINE_SERIES)
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
                    "team_abbr": m["ticker"].rsplit("-", 1)[-1],
                    "team_full_name": m.get("yes_sub_title", ""),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def _get_team_ladder_markets(series: str) -> list[dict]:
    """Shared by spread/team-total -- event ticker's teams blob resolves the
    two candidate teams, market ticker glues team+a rounded number with no
    separator, so the team is resolved by checking which known team prefixes
    the suffix (same approach as kalshi_nba_client.py's NBA version)."""
    events = get_open_events(series)
    rows = []
    known_abbrs = team_abbreviations()
    for ev in events:
        parsed = parse_kalshi_event_ticker(ev["event_ticker"])
        split = split_teams_blob(parsed["teams_blob"], known_abbrs) if parsed else None
        if not split:
            continue
        away, home = split
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            team = away if suffix.startswith(away) else (home if suffix.startswith(home) else None)
            if team is None or m.get("floor_strike") is None:
                continue
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "ticker": m["ticker"],
                    "team_abbr": team,
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


def get_team_total_markets() -> list[dict]:
    return _get_team_ladder_markets(TEAM_TOTAL_SERIES)


def get_f5_markets() -> list[dict]:
    """KXMLBF5 -- 3-way per game (confirmed live 2026-07-17: one event, 3
    markets, tickers ending "-{AWAY_ABBR}"/"-{HOME_ABBR}"/"-TIE"). Unlike
    _get_team_ladder_markets, the ticker's own trailing suffix IS the
    resolved outcome directly (a team abbreviation or the literal "TIE") --
    no floor_strike/team-blob-prefix matching needed, same simple
    resolution get_moneyline_markets() already uses."""
    events = get_open_events(F5_SERIES)
    rows = []
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            outcome = m["ticker"].rsplit("-", 1)[-1]  # team abbr, or "TIE"
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "ticker": m["ticker"],
                    "outcome": outcome,  # "TIE" or a team abbreviation
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_rfi_markets() -> list[dict]:
    """KXMLBRFI -- one binary market per game (confirmed live: "Yes" = a run
    scores in the 1st inning by either team, no polarity trap on Kalshi's
    side unlike Polymarket's NRFI-labeled equivalent -- see
    polymarket_mlb_client.py::get_rfi_markets)."""
    events = get_open_events(RFI_SERIES)
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
                    "ticker": m["ticker"],
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_futures_markets() -> list[dict]:
    rows = []
    for kind, series_list in FUTURES_SERIES.items():
        for series in series_list:
            for ev in get_open_events(series):
                try:
                    markets = get_markets_for_event(ev["event_ticker"])
                except Exception:
                    continue
                for m in markets:
                    rows.append(
                        {
                            "market_kind": kind,
                            "event_ticker": ev["event_ticker"],
                            "ticker": m["ticker"],
                            "group_label": ev.get("title", ""),
                            "team_abbr": m["ticker"].rsplit("-", 1)[-1],
                            "yes_bid": _to_float(m.get("yes_bid_dollars")),
                            "yes_ask": _to_float(m.get("yes_ask_dollars")),
                            "last_price": _to_float(m.get("last_price_dollars")),
                            "volume": _to_float(m.get("volume_fp")),
                            "status": m.get("status"),
                        }
                    )
    return rows


def get_win_total_markets() -> list[dict]:
    """KXMLBWINS-{team}, 30 separate series (one per team, ladder of win-
    count thresholds within each) -- same structure as NFL/NBA win-totals."""
    rows = []
    for team in sorted(team_abbreviations()):
        series = f"{WIN_TOTAL_SERIES_PREFIX}{team}"
        for ev in get_open_events(series):
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


def get_world_series_matchup_markets() -> list[dict]:
    """One row per possible World Series pairing (KXTEAMSINWS).

    THE TICKER IS TWO ABBREVIATIONS CONCATENATED -- "KXTEAMSINWS-26-TORWSH" is
    TOR + WSH -- with no separator, so it has to be split by trying every cut
    against the known team sets. Two facts make that unambiguous rather than a
    guess, both verified live 2026-08-06 across all 225 open markets:

      * Kalshi's MLB abbreviations are IDENTICAL to the StatsAPI ones this app
        already stores (all 30 match, no exceptions), so no mapping table is
        needed. Checking against ESPN_TO_STATSAPI_ABBR instead would be wrong:
        that dict holds only the two EXCEPTIONS (ARI->AZ, CHW->CWS), not the
        roster.
      * The pairing is always AL first, then NL. Every AL team appears only as
        a prefix and every NL team only as a suffix, so constraining each side
        to its own league leaves exactly one valid cut.

    Result: 225 of 225 split uniquely, 0 ambiguous, 0 failed. A row that does
    not split is skipped rather than guessed.

    yes_sub_title ("Toronto vs Washington") is deliberately NOT used as the
    key. It is human-facing and, while Kalshi does disambiguate the shared
    cities ("Chicago C"/"Chicago WS", "New York M"/"New York Y", "Los Angeles
    A"/"Los Angeles D"), a label is a weaker contract than a ticker.
    """
    from app.models.season_sim_mlb import TEAM_LEAGUE

    al = {t for t, lg in TEAM_LEAGUE.items() if lg == "AL"}
    nl = {t for t, lg in TEAM_LEAGUE.items() if lg == "NL"}
    rows = []
    for ev in get_open_events(WS_MATCHUP_SERIES):
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            cuts = [
                (suffix[:i], suffix[i:])
                for i in range(2, len(suffix) - 1)
                if suffix[:i] in al and suffix[i:] in nl
            ]
            if len(cuts) != 1:
                continue
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "ticker": m["ticker"],
                    "al_team": cuts[0][0],
                    "nl_team": cuts[0][1],
                    "group_label": ev.get("title", "") or "World Series Matchup",
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows
