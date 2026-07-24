"""Live-polling Kalshi client for NFL markets.

Adapted from the historical-batch-pull pattern in
Downloads/ufc-kalshi-polymarket/kalshi_pull.py (same base retry/backoff approach,
now via clients/base.py) but queries OPEN markets on an ongoing basis instead of
dumping settled history.

Series tickers confirmed live against the public API on 2026-07-14:
  KXNFLGAME   - moneyline (two markets per game, one "Will {team} win" per side)
  KXNFLSPREAD - spread (confirmed to exist, but no markets are open this far
                before the season -- spread/total series appear to open much
                closer to game week; ingestion for these lands in Phase 4 once
                real open-market data exists to verify structure against)
  KXNFLTOTAL  - totals (same caveat as spread)
"""
from app.clients.base import get_json, paginate
from app.ingestion.market_matcher import KALSHI_TEAM_ABBRS, parse_kalshi_event_ticker, split_teams_blob, to_nflverse_abbr

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = "KXNFLGAME"
SPREAD_SERIES = "KXNFLSPREAD"
TOTAL_SERIES = "KXNFLTOTAL"
TEAM_TOTAL_SERIES = "KXNFLTEAMTOTAL"
HALF_SPREAD_SERIES = {1: "KXNFL1HSPREAD", 2: "KXNFL2HSPREAD"}
HALF_TOTAL_SERIES = {1: "KXNFL1HTOTAL", 2: "KXNFL2HTOTAL"}

# Kalshi lists 240+ NFL series total (confirmed live 2026-07-15 via
# /series?category=Sports) -- the overwhelming majority are player props,
# awards, and draft/novelty markets this app's team-level Elo model has no
# way to price. These five are the ones that are actually team/season
# OUTCOME markets an Elo-based season Monte Carlo can meaningfully estimate
# (see app/models/season_sim.py), and are confirmed open with real
# liquidity (KXSB alone: $2-4M volume per team market) as of that date --
# unlike per-game spread/total (KXNFLSPREAD/KXNFLTOTAL above), which are
# still 0 open events this far before the season.
FUTURES_SERIES = {
    "division_winner": [
        "KXNFLAFCEAST", "KXNFLAFCNORTH", "KXNFLAFCSOUTH", "KXNFLAFCWEST",
        "KXNFLNFCEAST", "KXNFLNFCNORTH", "KXNFLNFCSOUTH", "KXNFLNFCWEST",
    ],
    "conference_champion": ["KXNFLAFCCHAMP", "KXNFLNFCCHAMP"],
    "one_seed": ["KXNFL1SEED"],
    "super_bowl_champion": ["KXSB"],
    "playoff_qualifier": ["KXNFLPLAYOFF"],
    # Found while auditing Kalshi's full series list for an undefeated-season
    # equivalent (2026-07-16) -- "best regular season record" is a genuine
    # per-team futures market season_sim.py can price (best_record_pct),
    # same team-ladder structure as everything else in this dict.
    "best_record": ["KXRECORDNFLBEST"],
    # Mirror of best_record, added 2026-07-16 -- same team-ladder structure,
    # confirmed live (KXRECORDNFLWORST-27, 32 real active markets).
    "worst_record": ["KXRECORDNFLWORST"],
}

# Season win-total markets, found while auditing Kalshi's win-total family
# for the "let's do the win-total markets" round (2026-07-16). Structurally
# different from FUTURES_SERIES above: each TEAM has its own series ticker
# (e.g. KXNFLWINS-KC), not one shared series with per-team markets, so these
# need per-team series iteration rather than a single get_open_events call.
# Confirmed live: the per-team win-total/exact-win-total series exist
# (KXNFLWINS-{team}/KXNFLEXACTWINS{team}) but have 0 open markets this far
# before the season -- same "not listed this far out" pattern spread/total
# were in before one surfaced mid-build, so this is built now, ready for
# when Kalshi opens the "-26" season events. KXNFLWINS-ANY (the league-wide
# "will ANY team hit N wins" ladder, team-less) IS open right now with real
# data (15+/16+/17+ wins), confirmed live -- used to validate the assumed
# floor_strike/ladder shape for the whole family before real per-team data
# exists.
WIN_TOTAL_SERIES_PREFIX = "KXNFLWINS-"
EXACT_WIN_TOTAL_SERIES_PREFIX = "KXNFLEXACTWINS"
WINS_ANY_SERIES = "KXNFLWINS-ANY"

# Confirmed live 2026-07-16: unlike every other per-team Kalshi series in
# this client (which uses Kalshi's own JAC/LAR-style abbreviations,
# translated via KALSHI_TO_NFLVERSE_ABBR), the win-total family's own team
# suffix is "JAC" for Jacksonville (matches Kalshi's usual convention) but
# "LA" for the Rams (matches nflverse's convention, NOT "LAR") -- a genuine,
# confirmed inconsistency within this one series family, not a guess. Every
# other team's win-total suffix already matches its nflverse abbreviation
# directly (no override needed).
WIN_TOTAL_ABBR_OVERRIDES = {"JAX": "JAC"}


def _win_total_series_suffix(nflverse_abbr: str) -> str:
    return WIN_TOTAL_ABBR_OVERRIDES.get(nflverse_abbr, nflverse_abbr)


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


def get_moneyline_markets() -> list[dict]:
    """Returns a flat list of dicts, one per team-side market, each tagged
    with its parent event metadata (title/sub_title/event_ticker)."""
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
                    "event_sub_title": ev.get("sub_title", ""),
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


def get_futures_markets() -> list[dict]:
    """Returns a flat list of dicts, one per team-side futures market,
    tagged with market_kind (division_winner/conference_champion/one_seed/
    super_bowl_champion/playoff_qualifier) and a human-readable group_label
    taken directly from Kalshi's own event title (e.g. "NFC West Division
    Winner") -- no per-division ticker->label mapping needed since Kalshi's
    title is already exactly what should be shown."""
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


# Kalshi's stage-of-elimination ticker suffix -> this app's season_sim
# stage_exit_pct key. FL = Runner-Up (lost the Super Bowl), FW = Championship
# Winner (won it). The six are exhaustive and mutually exclusive.
STAGE_OF_ELIM_SUFFIX = {
    "REG": "reg", "WC": "wc", "DIV": "div", "CONF": "conf", "FL": "sb_loss", "FW": "sb_win",
}


def get_stage_of_elimination_markets() -> list[dict]:
    """KXNFLSTAGEOFELIM: for each team, six mutually-exclusive markets for the
    round they bow out in. Ticker is KXNFLSTAGEOFELIM-{YY}{TEAM}-{STAGE} (e.g.
    -27WAS-DIV), so team comes from the event ticker's last segment with the
    leading season-year digits stripped, and the stage from the market ticker's
    suffix (mapped to this app's season_sim stage_exit_pct keys)."""
    rows = []
    try:
        events = get_open_events("KXNFLSTAGEOFELIM")
    except Exception:
        return rows
    for ev in events:
        et = ev["event_ticker"]
        team_abbr = et.rsplit("-", 1)[-1].lstrip("0123456789")  # "27WAS" -> "WAS"
        try:
            markets = get_markets_for_event(et)
        except Exception:
            continue
        for m in markets:
            suffix = m["ticker"].rsplit("-", 1)[-1]
            stage = STAGE_OF_ELIM_SUFFIX.get(suffix)
            if stage is None:
                continue  # unknown rung, don't guess
            rows.append({
                "event_ticker": et,
                "group_label": ev.get("title", ""),
                "ticker": m["ticker"],
                "team_abbr_kalshi": team_abbr,
                "stage": stage,
                "stage_label": m.get("yes_sub_title", ""),
                "yes_bid": _to_float(m.get("yes_bid_dollars")),
                "yes_ask": _to_float(m.get("yes_ask_dollars")),
                "last_price": _to_float(m.get("last_price_dollars")),
                "volume": _to_float(m.get("volume_fp")),
                "status": m.get("status"),
            })
    return rows


def _get_team_win_ladder(ticker_builder) -> list[dict]:
    """Shared by get_win_total_markets/get_exact_win_total_markets. Unlike
    _get_team_ladder_markets below (per-GAME series where the team has to be
    parsed off the ticker suffix), the win-total family is per-TEAM (one
    series per team), so the team is already known from which series is
    being queried -- no ticker parsing needed. `ticker_builder` maps a
    win-total series team suffix -> full series ticker."""
    rows = []
    nflverse_abbrs = sorted({to_nflverse_abbr(k) for k in KALSHI_TEAM_ABBRS})
    for team in nflverse_abbrs:
        series = ticker_builder(_win_total_series_suffix(team))
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
                        "team": team,  # already nflverse-resolved, unlike team_abbr_kalshi elsewhere
                        "line": float(m["floor_strike"]),
                        "yes_bid": _to_float(m.get("yes_bid_dollars")),
                        "yes_ask": _to_float(m.get("yes_ask_dollars")),
                        "last_price": _to_float(m.get("last_price_dollars")),
                        "volume": _to_float(m.get("volume_fp")),
                        "status": m.get("status"),
                    }
                )
    return rows


MVP_SERIES = "KXNFLMVP"
COACH_OF_YEAR_SERIES = "KXNFLCOTY"
DPOY_SERIES = "KXNFLDPOTY"
OPOY_SERIES = "KXNFLOPOTY"


def _get_named_candidate_markets(series: str) -> list[dict]:
    """Shared by get_mvp_markets/get_coach_of_year_markets -- structurally
    identical to get_futures_markets (one event, many candidate markets,
    ticker suffix + yes_sub_title=candidate full name), but NOT folded into
    FUTURES_SERIES/get_futures_markets since those assume the ticker suffix
    resolves to a real NFL team abbreviation (upsert_kalshi_futures_market
    calls to_nflverse_abbr on it) -- here the suffix is a player/coach code
    (e.g. "PMAH" for Patrick Mahomes), not a team, so it needs its own
    upsert path that resolves the CANDIDATE NAME to a team via
    awards.py's reverse lookups instead."""
    rows = []
    try:
        events = get_open_events(series)
    except Exception:
        return rows
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            candidate_name = m.get("yes_sub_title") or ""
            if not candidate_name:
                continue
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "candidate_name": candidate_name,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_mvp_markets() -> list[dict]:
    return _get_named_candidate_markets(MVP_SERIES)


def get_coach_of_year_markets() -> list[dict]:
    return _get_named_candidate_markets(COACH_OF_YEAR_SERIES)


def get_dpoy_markets() -> list[dict]:
    return _get_named_candidate_markets(DPOY_SERIES)


def get_opoy_markets() -> list[dict]:
    return _get_named_candidate_markets(OPOY_SERIES)


DIVISION_WINS_SERIES = "KXNFLDIVISIONWINS"
DIVISION_ORDER_SERIES = "KXNFLDIVISIONORDER"
DIV_LEAST_WINS_SERIES = "KXNFLDIVLEASTWINS"
DIV_MOST_WINS_SERIES = "KXNFLDIVMOSTWINS"
WORST_TO_FIRST_SERIES = "KXNFLWORSTTOFIRST"
H2H_WINS_SERIES = "KXNFLH2HWINS"


def _division_code_from_ticker(event_ticker: str, series: str) -> str | None:
    """"KXNFLDIVISIONWINS-27NFCWEST" -> "NFCWEST" (Kalshi's compact,
    no-space division code) -- matched against our DIVISIONS dict's keys via
    key.replace(" ", "").upper() by the caller. "-27" (the award-year
    convention already seen on MVP/COTY/conference-champion) is hardcoded
    here, same precedent as those -- this app re-verifies against the live
    catalog each season rather than generalizing the year prefix."""
    prefix = f"{series}-27"
    if not event_ticker.startswith(prefix):
        return None
    return event_ticker[len(prefix):]


def get_division_wins_markets() -> list[dict]:
    """Ladder of 'division combines for N+ total wins' per division -- same
    shape as get_win_total_markets but keyed by DIVISION not team."""
    rows = []
    try:
        events = get_open_events(DIVISION_WINS_SERIES)
    except Exception:
        return rows
    for ev in events:
        division_code = _division_code_from_ticker(ev["event_ticker"], DIVISION_WINS_SERIES)
        if division_code is None:
            continue
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
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "division_code": division_code,
                    "line": float(m["floor_strike"]),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def _get_division_named_markets(series: str) -> list[dict]:
    """Shared by get_div_least_wins_markets/get_div_most_wins_markets --
    single event, one team-less market per division (ticker suffix =
    division code directly, e.g. "...-NFCNORTH"), same shape as the
    league-wide wins_any ladder but keyed by division instead of a win
    threshold."""
    rows = []
    try:
        events = get_open_events(series)
    except Exception:
        return rows
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            division_code = m["ticker"].rsplit("-", 1)[-1]
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "division_code": division_code,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_div_least_wins_markets() -> list[dict]:
    return _get_division_named_markets(DIV_LEAST_WINS_SERIES)


def get_div_most_wins_markets() -> list[dict]:
    return _get_division_named_markets(DIV_MOST_WINS_SERIES)


def get_worst_to_first_markets() -> list[dict]:
    """Single league-wide binary market, team-less -- same shape as
    get_wins_any_markets's family but no floor_strike (no threshold, it's a
    plain yes/no)."""
    rows = []
    try:
        events = get_open_events(WORST_TO_FIRST_SERIES)
    except Exception:
        return rows
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_h2h_wins_markets() -> list[dict]:
    """Per-pair 'will team A out-win team B' -- team resolved directly from
    the ticker suffix (already a real Kalshi team abbreviation, e.g.
    "...-PIT"), same convention as moneyline's team_abbr_kalshi field."""
    rows = []
    try:
        events = get_open_events(H2H_WINS_SERIES)
    except Exception:
        return rows
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            team_k = m["ticker"].rsplit("-", 1)[-1]
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "team_abbr_kalshi": team_k,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_division_order_markets() -> list[dict]:
    """24-permutation-per-division market -- ticker suffix is a concatenated
    blob of the 4 teams' Kalshi codes IN ORDER (e.g. "ARISEALARSF"), no
    separator. Resolved by the CALLER (market_catalog.py), which knows the
    division's real 4 teams and can just try all 24 permutations against the
    blob rather than parse it blindly."""
    rows = []
    try:
        events = get_open_events(DIVISION_ORDER_SERIES)
    except Exception:
        return rows
    for ev in events:
        division_code = _division_code_from_ticker(ev["event_ticker"], DIVISION_ORDER_SERIES)
        if division_code is None:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            blob = m["ticker"].rsplit("-", 1)[-1]
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "division_code": division_code,
                    "order_blob": blob,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


LEADER_SERIES = {
    "leader_pass_yds": "KXLEADERNFLPYDS",
    "leader_pass_tds": "KXLEADERNFLPTDS",
    "leader_pass_int": "KXLEADERNFLPINT",
    "leader_rush_yds": "KXLEADERNFLRUSHYDS",
    "leader_rush_tds": "KXLEADERNFLRUSHTDS",
    "leader_rec_yds": "KXLEADERNFLRYDS",
    "leader_rec_tds": "KXLEADERNFLRTDS",
    "leader_def_int": "KXLEADERNFLINT",
    "leader_sacks": "KXLEADERNFLSACKS",
}


def get_leader_markets(market_type: str) -> list[dict]:
    """League-leader categorical markets (KXLEADERNFL* family) -- same
    fetch shape as _get_named_candidate_markets (MVP/COTY/DPOY/OPOY), reused
    directly rather than duplicated."""
    series = LEADER_SERIES.get(market_type)
    if series is None:
        return []
    return _get_named_candidate_markets(series)


TEAM_POINTS_SERIES = {
    "team_pts_most": ("KXNFLTEAMPTS", "MOST"),
    "team_pts_least": ("KXNFLTEAMPTS", "LEAST"),
    "team_dpts_most": ("KXNFLTEAMDPTS", "MOST"),
    "team_dpts_least": ("KXNFLTEAMDPTS", "LEAST"),
}


def get_team_points_markets(market_type: str) -> list[dict]:
    """Who scores/allows the most/least points -- team ticker suffix is
    already a real team abbreviation (e.g. "...-MOST27-KC"), same shape as
    best_record/worst_record, just with a MOST/LEAST event-ticker qualifier
    to pick out (one of the 4 combinations may not be open yet -- confirmed
    live 2026-07-16 only team_pts_most/team_pts_least/team_dpts_least exist
    so far, team_dpts_most gracefully returns 0 rows until Kalshi opens it)."""
    spec = TEAM_POINTS_SERIES.get(market_type)
    if spec is None:
        return []
    series, qualifier = spec
    rows = []
    try:
        events = get_open_events(series)
    except Exception:
        return rows
    for ev in events:
        if qualifier not in ev["event_ticker"]:
            continue
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "team_abbr_kalshi": m["ticker"].rsplit("-", 1)[-1],
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


SEASON_STAT_SERIES = {
    "pass_yds": "KXNFLSEASONPASSYDS",
    "pass_tds": None,  # not listed as a season ladder on Kalshi, only KXLEADERNFLPTDS (league-leader, already built)
    "rush_yds": "KXNFLSEASONRSHYDS",
    "rush_tds": "KXNFLSEASONRSHTD",
    "rec_yds": "KXNFLSEASONRECYDS",
    "rec_tds": "KXNFLSEASONRECTD",
    "rec": "KXNFLSEASONREC",
}


def get_season_stat_markets(category: str) -> list[dict]:
    """Season-total threshold ladder for one stat category -- each EVENT is
    one threshold (e.g. event "KXNFLSEASONPASSYDS-27C4500" = "4500+ yards",
    floor_strike gives the exact number), with one market per named
    candidate inside. Multiple threshold events per category (confirmed
    live 2026-07-16: 3-6 per category) all need fetching and merging, unlike
    the single-event league-leader markets."""
    series = SEASON_STAT_SERIES.get(category)
    if series is None:
        return []
    rows = []
    try:
        events = get_open_events(series)
    except Exception:
        return rows
    for ev in events:
        try:
            markets = get_markets_for_event(ev["event_ticker"])
        except Exception:
            continue
        for m in markets:
            candidate_name = m.get("yes_sub_title") or ""
            if not candidate_name or m.get("floor_strike") is None:
                continue
            rows.append(
                {
                    "event_ticker": ev["event_ticker"],
                    "group_label": ev.get("title", ""),
                    "ticker": m["ticker"],
                    "candidate_name": candidate_name,
                    "line": float(m["floor_strike"]),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                }
            )
    return rows


def get_win_total_markets() -> list[dict]:
    """Season win total, over/under ladder per team ("N+ wins?", floor_strike
    = N) -- see WIN_TOTAL_SERIES_PREFIX above for why this iterates 32
    per-team series instead of one shared series."""
    return _get_team_win_ladder(lambda suffix: f"{WIN_TOTAL_SERIES_PREFIX}{suffix}")


def get_exact_win_total_markets() -> list[dict]:
    """Exact season win count per team (floor_strike = the exact win count,
    one binary market per possible value) -- same per-team-series shape as
    get_win_total_markets."""
    return _get_team_win_ladder(lambda suffix: f"{EXACT_WIN_TOTAL_SERIES_PREFIX}{suffix}")


def get_wins_any_markets() -> list[dict]:
    """League-wide 'will ANY team hit N wins' -- team-less ladder, same shape
    as get_total_markets. Confirmed live 2026-07-16 with real open markets
    (15+/16+/17+ wins), unlike the per-team win-total family above which
    isn't open yet this far before the season -- used to validate the
    floor_strike/ladder shape assumed for that family."""
    return _get_game_ladder_markets(WINS_ANY_SERIES)


def _team_for_market(ticker: str, away_k: str, home_k: str) -> str | None:
    """Spread/total market tickers glue team+a rounded number onto the event
    ticker with no separator (e.g. "...-WPG4"), unlike moneyline's clean
    "...-KC" suffix -- so the team can't be split off directly. Since the
    event ticker already tells us the two candidate teams, just check which
    one prefixes the suffix."""
    suffix = ticker.rsplit("-", 1)[-1]
    if suffix.startswith(away_k):
        return away_k
    if suffix.startswith(home_k):
        return home_k
    return None


def _get_team_ladder_markets(series: str) -> list[dict]:
    """Shared by every Kalshi ladder series that's per-TEAM (spread,
    team-total, half-spread): event ticker follows the KXNFL{KIND}-
    {date}{teams} convention, market ticker glues team+a rounded number
    with no separator (e.g. "...-WPG4"), so the team can't be split off
    directly -- resolved by checking which of the event's two known teams
    prefixes the suffix. `floor_strike` is the real threshold; the ticker's
    own trailing number is NOT reliably that threshold."""
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
            team_k = _team_for_market(m["ticker"], away_k, home_k)
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
    """Shared by every Kalshi ladder series that's game-level, not per-team
    (total, half-total): one-sided per rung ("Over X points scored?"), no
    team association."""
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
    """Kalshi structures spread as a LADDER of "Team wins by more than X
    points?" threshold markets (5-10+ rungs per team per game), not a
    single line -- confirmed via KXCFLSPREAD (same platform, same sport
    family; KXNFLSPREAD itself has zero open events this far before the
    season, so this couldn't be verified against a live NFL example
    directly -- worth a quick sanity check once real markets open)."""
    return _get_team_ladder_markets(SPREAD_SERIES)


def get_total_markets() -> list[dict]:
    """Same ladder structure as spread, one-sided per rung -- see
    get_spread_markets."""
    return _get_game_ladder_markets(TOTAL_SERIES)


def get_team_total_markets() -> list[dict]:
    """Same ladder structure as spread but one-sided per rung like total
    ("{Team} over X points scored?") -- confirmed via a currently-open
    World Cup team-total market (KXNFLTEAMTOTAL itself has zero open events
    this far before the season)."""
    return _get_team_ladder_markets(TEAM_TOTAL_SERIES)


def get_half_spread_markets(half: int) -> list[dict]:
    """1st/2nd-half spread -- same ladder structure as full-game spread,
    confirmed via a currently-open World Cup 1st-half total market (same
    platform, same event-ticker/floor_strike convention; KXNFL1HSPREAD/
    KXNFL2HSPREAD themselves have zero open events this far before the
    season). `half` is 1 or 2."""
    return _get_team_ladder_markets(HALF_SPREAD_SERIES[half])


def get_half_total_markets(half: int) -> list[dict]:
    """1st/2nd-half total -- see get_half_spread_markets."""
    return _get_game_ladder_markets(HALF_TOTAL_SERIES[half])


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
