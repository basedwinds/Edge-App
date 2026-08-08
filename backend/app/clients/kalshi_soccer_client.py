"""Live-polling Kalshi client for Soccer moneyline (3-way Home/Draw/Away)
markets. Parallel to kalshi_tennis_client.py, but a genuinely different
market SHAPE: each match is THREE separate binary Yes/No markets sharing one
event_ticker (confirmed live 2026-07-19 via a real KXMLSGAME event: one
market per team + one "Tie" market, ticker suffixes "-{TEAM_CODE}"/"-TIE"),
not two markets naming each side the way NFL/Tennis moneyline does.

Home/away order: the shared event `title` ("San Jose vs Los Angeles G
Winner?" / "Liverpool vs Brentford Winner?") lists the HOME team FIRST --
confirmed live 2026-07-19 by cross-checking the real "Liverpool vs Brentford"
Kalshi title against football-data.co.uk's own HomeTeam="Liverpool" for that
exact real match (2026-05-24 E0 fixture). Relied on directly here rather than
re-derived per match.

Six GAME series confirmed live 2026-07-19 (see market_matcher_soccer.py's
_KALSHI_SOCCER_PREFIX_TO_DIVISION): KXEPLGAME/KXLALIGAGAME/KXSERIEAGAME/
KXBUNDESLIGAGAME/KXLIGUE1GAME/KXMLSGAME.

SPREAD and TOTAL series (KX{LEAGUE}SPREAD/KX{LEAGUE}TOTAL) confirmed live
2026-07-19 via a real KXMLSSPREAD/KXMLSTOTAL event -- both are real GOAL-count
LADDERS, same shape as this app's other sports' spread/total (see
kalshi_client.py's own get_spread_markets/get_total_markets docstrings):
  - SPREAD: one event per match ("San Jose vs Los Angeles G: Spread"), TWO
    markets per team per line (confirmed: 1.5 and 2.5 goals, 4 markets total
    per match), yes_sub_title is full text ("Los Angeles G wins by more than
    1.5 goals"), floor_strike gives the real threshold.
  - TOTAL: one event per match ("...: Total Goals"), one market per rung
    (confirmed: 0.5 through 5.5 goals, 6 rungs), yes_sub_title "Over X.5
    goals scored", team-less (game-level, not per-team)."""
import re
import time

from app.clients.base import get_json, paginate

BASE = "https://api.elections.kalshi.com/trade-api/v2"

MONEYLINE_SERIES = {
    "E0": "KXEPLGAME",
    "SP1": "KXLALIGAGAME",
    "I1": "KXSERIEAGAME",
    "D1": "KXBUNDESLIGAGAME",
    "F1": "KXLIGUE1GAME",
    "MLS": "KXMLSGAME",
    "E1": "KXEFLCHAMPIONSHIPGAME",
    "P1": "KXLIGAPORTUGALGAME",
    "N1": "KXEREDIVISIEGAME",
}

SPREAD_SERIES = {
    "E0": "KXEPLSPREAD",
    "SP1": "KXLALIGASPREAD",
    "I1": "KXSERIEASPREAD",
    "D1": "KXBUNDESLIGASPREAD",
    "F1": "KXLIGUE1SPREAD",
    "MLS": "KXMLSSPREAD",
    "E1": "KXEFLCHAMPIONSHIPSPREAD",
    "P1": "KXLIGAPORTUGALSPREAD",
    "N1": "KXEREDIVISIESPREAD",
}

LEAGUE_WINNER_SERIES = {
    "E0": ("KXPREMIERLEAGUE", "EPL Champion"),
    "SP1": ("KXLALIGA", "La Liga Champion"),
    "I1": ("KXSERIEA", "Serie A Champion"),
    "D1": ("KXBUNDESLIGA", "Bundesliga Champion"),
    "F1": ("KXLIGUE1", "Ligue 1 Champion"),
    # Added 2026-08-07. Both leagues gained GAME markets earlier the same day and
    # had no futures at all, which made them the two largest leagues in the app
    # with zero season-long coverage (P1 1,225 game markets, E1 735).
    #
    # INERT UNTIL KALSHI OPENS THE EVENTS: both series exist in the catalogue
    # (KXEFLCHAMPIONSHIP "EFL Championship League Winner", KXLIGAPORTUGAL "Liga
    # Portugal Winner") but returned 0 open events when this was wired, while
    # KXPREMIERLEAGUE already had its 2027 event open. Wired now anyway because
    # the change is one dict entry and the alternative is noticing weeks late --
    # same posture as the CFB spread ingestion, which sat inert by design until
    # Kalshi listed it.
    #
    # No new model needed: simulate_season is league-agnostic (it builds its own
    # double round-robin rather than reading a fixture list) and takes its team
    # list from the ingested markets themselves, so both price the moment rows
    # exist. The group_label below is OURS, not Kalshi's title -- the router's
    # _MARKET_TYPE_LABEL_TO_DIVISION is derived from this same dict, so the two
    # cannot disagree.
    "E1": ("KXEFLCHAMPIONSHIP", "EFL Championship Winner"),
    "P1": ("KXLIGAPORTUGAL", "Liga Portugal Champion"),
    "N1": ("KXEREDIVISIE", "Eredivisie Champion"),
}
# A dedicated "KXEPLTOP4"-style series per league (KXEPLTOP4, KXLALIGATOP4,
# etc) exists but had ZERO open events on Kalshi as of 2026-07-19 --
# confirmed real, just not this app's actual EPL Top-4 source (see
# TOP_N_SERIES below: EPL's REAL, live Top-4/Top-2/Top-Half futures live
# under a DIFFERENT series, "KXEPLTOP", found during a later 2026-07-19
# catalog_scan.py audit -- this empty per-league series was a real dead end,
# not the same market re-discovered). MLS has no league_winner-shaped market
# either -- KXMLSCUP is a PLAYOFF bracket, not a table finish, a genuinely
# different real structure the round-robin season model doesn't cover. That is
# still why MLS is absent from LEAGUE_WINNER_SERIES above; it is now modelled
# separately by playoff_sim_mls.py and ingested via MLS_PLAYOFF_SERIES below.

TOTAL_SERIES = {
    "E0": "KXEPLTOTAL",
    "SP1": "KXLALIGATOTAL",
    "I1": "KXSERIEATOTAL",
    "D1": "KXBUNDESLIGATOTAL",
    "F1": "KXLIGUE1TOTAL",
    "MLS": "KXMLSTOTAL",
    "E1": "KXEFLCHAMPIONSHIPTOTAL",
    "P1": "KXLIGAPORTUGALTOTAL",
    "N1": "KXEREDIVISIETOTAL",
}

# BTTS (Both Teams To Score) confirmed live 2026-07-19 with real open
# inventory for MLS ONLY (KXMLSBTTS, 30 open events, real per-match) --
# the 5 European leagues' own BTTS series (KXEPLBTTS etc) exist but had
# ZERO open events at the same check (off-season, same pattern as their own
# GAME/SPREAD/TOTAL series before the season starts) -- built for all 6 keys
# anyway so European coverage activates automatically once real events open,
# same "the code doesn't need to change, just the live inventory" precedent
# as every other market type here.
BTTS_SERIES = {
    "E0": "KXEPLBTTS",
    "SP1": "KXLALIGABTTS",
    "I1": "KXSERIEABTTS",
    "D1": "KXBUNDESLIGABTTS",
    "F1": "KXLIGUE1BTTS",
    "MLS": "KXMLSBTTS",
    "E1": "KXEFLCHAMPIONSHIPBTTS",
    "P1": "KXLIGAPORTUGALBTTS",
    "N1": "KXEREDIVISIEBTTS",
}

# Relegation confirmed live 2026-07-19 for all 5 European leagues
# (KXEPLRELEGATION-27 etc, real per-team "is this team relegated Y/N"
# markets, 20/18 real markets per league) -- see season_sim_soccer.py's own
# docstring on why Bundesliga/Ligue 1's real playoff mechanics make this
# app's own model a LOWER BOUND for the team right at that boundary. No MLS
# entry -- MLS has no relegation.
RELEGATION_SERIES = {
    "E0": ("KXEPLRELEGATION", "EPL Relegation"),
    "SP1": ("KXLALIGARELEGATION", "La Liga Relegation"),
    "I1": ("KXSERIEARELEGATION", "Serie A Relegation"),
    "D1": ("KXBUNDESLIGARELEGATION", "Bundesliga Relegation"),
    "F1": ("KXLIGUE1RELEGATION", "Ligue 1 Relegation"),
}


# EPL-only real inventory (confirmed live 2026-07-19): ONE series
# ("KXEPLTOP") holds THREE real event tickers at once -- "-27TOPHALF"
# (Top Half Finishers), "-27TOP4" (Top 4 -- the REAL Champions-League-
# qualification-style futures this app's earlier live audit had marked as
# "confirmed to exist, zero real open events", checked against the WRONG
# series ticker "KXEPLTOP4" -- the real market lives here instead, under
# "KXEPLTOP", a genuinely different real discovery, not the same finding
# re-confirmed), and "-27TOP2" (Top 2). Not found for the other 4 leagues
# during the same live scan -- EPL-only for now, same "ship what has real
# inventory" precedent as everything else in this app.
TOP_N_SERIES = {"E0": "KXEPLTOP"}

# Season points ladders: "Will <team> finish the 2026-27 season with N+ points?".
# Unlike every other futures series here, these are a LADDER -- one market per
# (team, threshold) rather than one per team -- so a row needs both the team and
# the number. Confirmed live 2026-08-02: 384 open markets across the five
# leagues, each under a SINGLE event per league, yes_sub_title of the form
# "Tottenham: 75+ Points", and the threshold carried in floor_strike as N-0.5
# (74.5 for a 75+ market). All five were unquoted at the time -- the 2026-27
# seasons had not kicked off -- which is expected, not a fault.
TEAM_POINTS_SERIES = {
    "E0": "KXEPLTEAMPOINTS",
    "SP1": "KXLALIGATEAMPOINTS",
    "I1": "KXSERIEATEAMPOINTS",
    "D1": "KXBUNDESLIGATEAMPOINTS",
    "F1": "KXLIGUE1TEAMPOINTS",
}
_TOP_N_EVENT_LABELS = {"TOPHALF": ("top_half", "EPL Top Half"), "TOP4": ("top4", "EPL Top 4"), "TOP2": ("top2", "EPL Top 2")}


def get_open_events(series_ticker: str) -> list[dict]:
    def url_builder(cursor):
        url = f"{BASE}/events?series_ticker={series_ticker}&status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        return url

    return paginate(url_builder, list_key="events", cursor_style="cursor")


# --- BATCHED MARKET FETCH (2026-08-08) -------------------------------------
# MEASURED PROBLEM this solves. run_full_refresh_soccer was taking 784s against
# a 300s interval, with "kalshi markets" alone at 415s -- 1.4x the entire
# interval -- and that figure swung 174s -> 415s between consecutive passes,
# which is rate-limit backoff variance rather than workload. The app had logged
# 805 Kalshi 429s since startup; base.get_json sleeps 2*(attempt+1)s per 429 up
# to four retries, so a throttled call can burn 12s doing nothing, and every
# sport's poller competes for the same quota.
#
# THE CAUSE was one HTTP call PER EVENT. Each fetcher below walks
# get_open_events(series) and then calls get_markets_for_event() for every
# event it found -- hundreds of round trips per cycle across 9 leagues and a
# dozen market types. But Kalshi will return every market for a whole SERIES in
# a single request, which is how this session's probes read all 441 UEFA
# markets instantly. So the per-event call is replaced by one batched fetch per
# series, memoized for a short window and grouped by event_ticker.
#
# NO CALL SITE CHANGES. All 22 callers keep calling get_markets_for_event; it
# just answers from the batch now. The series is recoverable from the event
# ticker, which is always "{SERIES}-{EVENT SUFFIX}".
#
# FALLS BACK RATHER THAN GUESSING: if an event is absent from its series batch
# (an unexpected ticker shape, or a market the series query does not surface),
# the original per-event request is issued for that event alone. A miss costs
# one call, never a wrong or empty answer.
_MARKET_BATCH_TTL_SECONDS = 120  # shorter than the 300s poll interval, so each cycle refetches once
_market_batch_cache: dict[str, tuple[float, dict[str, list[dict]]]] = {}


def _series_of(event_ticker: str) -> str | None:
    head = (event_ticker or "").split("-", 1)[0].strip()
    return head or None


def _markets_by_event_for_series(series_ticker: str) -> dict[str, list[dict]]:
    cached = _market_batch_cache.get(series_ticker)
    if cached and (time.time() - cached[0]) < _MARKET_BATCH_TTL_SECONDS:
        return cached[1]

    def url_builder(cursor):
        url = f"{BASE}/markets?series_ticker={series_ticker}&status=open&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        return url

    try:
        markets = paginate(url_builder, list_key="markets", cursor_style="cursor")
    except Exception:
        # A failed batch must not poison the cache -- leave it unset so the
        # per-event fallback handles this cycle and the next pass retries.
        return {}
    grouped: dict[str, list[dict]] = {}
    for m in markets:
        ev = m.get("event_ticker")
        if ev:
            grouped.setdefault(ev, []).append(m)
    _market_batch_cache[series_ticker] = (time.time(), grouped)
    return grouped


def get_markets_for_event(event_ticker: str) -> list[dict]:
    series = _series_of(event_ticker)
    if series:
        grouped = _markets_by_event_for_series(series)
        hit = grouped.get(event_ticker)
        if hit is not None:
            return hit
    d = get_json(f"{BASE}/markets?event_ticker={event_ticker}")
    return d.get("markets", [])


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_TITLE_SUFFIX_RE = re.compile(
    r"\s+Winner\?$|:\s*Spread$|:\s*Total Goals$"
    r"|:\s*First Half Winner$|:\s*First Half Spread$|:\s*First Half Total$|:\s*First Half BTTS$"
    r"|:\s*Second Half Winner$|:\s*Second Half Spread$|:\s*Second Half Total$|:\s*Second Half BTTS$"
    r"|:\s*First Team to Score$|:\s*Correct Score$|:\s*Team Total$"
)


def _parse_title_teams(title: str) -> tuple[str, str] | None:
    """"Liverpool vs Brentford Winner?" -> ("Liverpool", "Brentford"), home
    first (see module docstring). Also handles the SPREAD/TOTAL series' own
    title suffixes ("...: Spread" / "...: Total Goals") -- same "home team
    listed first" convention confirmed for the GAME series applies here too
    (all three series share the same underlying match, same event-creation
    pipeline on Kalshi's side)."""
    if " vs " not in title:
        return None
    left, _, right = title.partition(" vs ")
    right = _TITLE_SUFFIX_RE.sub("", right).strip()
    left = left.strip()
    if not left or not right:
        return None
    return left, right


def get_moneyline_markets() -> list[dict]:
    """One row per (event, outcome) across all 6 leagues -- 3 rows per real
    match (home/away/draw), each a plain binary Yes/No market. `side` is
    "home"/"away"/"draw", resolved by comparing yes_sub_title against the
    event title's own home/away team names (ticker suffix alone isn't
    reliably a team abbreviation for every league -- "Tie" is always
    unambiguous, but team-side tickers use ad-hoc per-league codes, e.g.
    "-LAG"/"-SJ" -- text comparison against the title is more robust)."""
    rows = []
    for division, series_ticker in MONEYLINE_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = m.get("yes_sub_title", "")
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home_team:
                    side, team = "home", home_team
                elif label == away_team:
                    side, team = "away", away_team
                else:
                    continue  # label didn't match either known team or "Tie" -- skip rather than guess
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "event_title": title,
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "side": side,
                    "team": team,
                    "estimated_start_time": m.get("occurrence_datetime"),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_SPREAD_SUB_RE = re.compile(r"^(.+?) wins by more than [\d.]+ goals$")


def get_spread_markets() -> list[dict]:
    """One row per (event, team, line) -- 4 rows per real match (2 teams x
    2 lines, see module docstring). `team` is resolved by comparing the
    parsed name inside yes_sub_title against the event's known home/away
    teams, same "text comparison, not ticker-suffix" reasoning as moneyline."""
    rows = []
    for division, series_ticker in SPREAD_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _SPREAD_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                name = sub_match.group(1)
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "team": team,
                    "line": _to_float(m.get("floor_strike")),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_TOTAL_SUB_RE = re.compile(r"^Over ([\d.]+) goals scored$")


def get_total_markets() -> list[dict]:
    """One row per (event, line) -- game-level, team-less ladder (6 rungs
    confirmed live, see module docstring)."""
    rows = []
    for division, series_ticker in TOTAL_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _TOTAL_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "line": float(sub_match.group(1)),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_BTTS_TITLE_SUFFIX_RE = re.compile(r":\s*BTTS$")


def get_btts_markets() -> list[dict]:
    """One row per real match -- BTTS is a single binary Yes/No market
    (event-level, no per-team/per-line split like moneyline/spread), so
    unlike get_moneyline_markets there's no label-matching loop: whichever
    single market exists under the event IS the BTTS market (confirmed live
    2026-07-19 against a real KXMLSBTTS event -- exactly one market per
    event, yes_sub_title empty/generic, the event title itself carries the
    match). Title suffix is "...: BTTS" (confirmed live -- NOT the spelled-
    out "Both Teams To Score" every other series' title suffix pattern here
    would suggest), stripped the same way SPREAD/TOTAL strip their own
    suffix before team-name parsing."""
    rows = []
    for division, series_ticker in BTTS_SERIES.items():
        events = get_open_events(series_ticker)
        for ev in events:
            raw_title = ev.get("title", "")
            title = _BTTS_TITLE_SUFFIX_RE.sub("", raw_title).strip()
            teams = _parse_title_teams(title if " vs " in title else raw_title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "home_team": home_team,
                    "away_team": away_team,
                    "ticker": m["ticker"],
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_relegation_markets() -> list[dict]:
    """One row per (league, team-in-the-field) -- same real shape as
    get_league_winner_markets (single event per league/season, one binary
    market per team, no title-parsing needed), confirmed live 2026-07-19
    against a real KXEPLRELEGATION-27 event (20 real per-team markets,
    ticker suffix a team code, yes_sub_title the real full team name --
    "Yes" resolves to THAT team being relegated)."""
    rows = []
    for division, (series_ticker, group_label) in RELEGATION_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_team_points_markets() -> list[dict]:
    """One row per (league, team, points threshold) for the KX*TEAMPOINTS ladders.

    Two things differ from the other per-team futures fetchers above. The team
    name has to be split off yes_sub_title ("Tottenham: 75+ Points"), since the
    same team appears on several rungs. And the threshold is taken from
    floor_strike rather than parsed out of the title -- Kalshi already states it
    numerically, and reading "75+" out of prose would break the moment a market
    is worded differently. A row with no usable team or no floor_strike is
    SKIPPED rather than guessed at: an unpriceable rung is better than a rung
    priced against the wrong number."""
    rows = []
    for division, series_ticker in TEAM_POINTS_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub = m.get("yes_sub_title") or ""
                team = sub.split(":", 1)[0].strip()
                floor = m.get("floor_strike")
                if not team or floor is None:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "ticker": m["ticker"],
                    "team": team,
                    "line": _to_float(floor),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_league_winner_markets() -> list[dict]:
    """One row per (league, team-in-the-field) -- confirmed live 2026-07-19
    via a real KXPREMIERLEAGUE-27 event: 20 real per-team markets (real
    volume, e.g. Arsenal $8.1k, Man City $11.2k), ticker suffix a team code
    (e.g. "-ARS"), yes_sub_title the real full team name. Single event per
    league (one season's worth of teams), not per-match like GAME/SPREAD/
    TOTAL -- no team-name title-parsing needed here."""
    rows = []
    for division, (series_ticker, group_label) in LEAGUE_WINNER_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": division,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


# MLS Cup Playoffs (added 2026-08-07). Priced by playoff_sim_mls.py, NOT by the
# round-robin season model that handles the European league_winner markets --
# see that module's docstring for why MLS needs its own.
#
# ONE simulation prices all three of these series, which is why they are grouped
# in a single dict: KXMLSEAST/KXMLSWEST resolve on winning the CONFERENCE
# BRACKET, not on topping the regular-season conference table. That was checked
# against Kalshi's own rules_primary rather than inferred from the series name
# -- KXMLSEAST-26-TOR reads "...is the 2026 MLS Eastern Conference champion",
# and the East/West bracket winners are exactly the two teams the sim already
# has to produce on the way to an MLS Cup winner. Pricing them off the
# regular-season table instead would answer a different question (Vancouver
# leading the West in August is not the same proposition as Vancouver winning
# the Western bracket in December).
#
# Live inventory confirmed 2026-08-07: KXMLSCUP 30 open, KXMLSEAST 15,
# KXMLSWEST 15 -- one market per team, the same one-event-per-series shape as
# LEAGUE_WINNER_SERIES, so no title parsing.
#
# NOT included: KXMLSLEADER (17 open). That is the golden boot ("Will <player>
# lead MLS in goals"), a PLAYER season-stat market -- the family this app
# already measured and put in PLAYER_STAT_TRACKING_ONLY. Different question,
# different model, deliberately out of scope here.
MLS_PLAYOFF_SERIES = {
    "KXMLSCUP": ("mls_cup_winner", "MLS Cup"),
    "KXMLSEAST": ("mls_conference_winner", "MLS Eastern Conference"),
    "KXMLSWEST": ("mls_conference_winner", "MLS Western Conference"),
}


def get_mls_playoff_markets() -> list[dict]:
    """One row per (series, team). Same per-team shape as
    get_league_winner_markets -- yes_sub_title is the full team name."""
    rows = []
    for series_ticker, (market_type, group_label) in MLS_PLAYOFF_SERIES.items():
        for ev in get_open_events(series_ticker):
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"],
                    "division": "MLS",
                    "market_type": market_type,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


# ---------------------------------------------------------------------------
# Second batch (added 2026-07-19, same day, after a full catalog_scan.py
# audit surfaced real, live inventory this app hadn't covered yet): First
# Half / Second Half / First Team To Score / Correct Score / Team Total.
# Confirmed live via real KXMLS* events (MLS is the only currently in-season
# league -- the 5 European leagues' own equivalents return the identical
# empty pattern every other per-match series shows right now, see module
# docstring). Built for all leagues with a real confirmed series (not just
# MLS) so European coverage activates automatically once their season
# starts, same "ship the code, thin/empty until in-season" precedent as the
# original GAME/SPREAD/TOTAL series.
# ---------------------------------------------------------------------------

FIRST_HALF_SERIES = {
    "E0": "KXEPL1H", "SP1": "KXLALIGA1H", "I1": "KXSERIEA1H",
    "D1": "KXBUNDESLIGA1H", "F1": "KXLIGUE11H", "MLS": "KXMLS1H",
    "E1": "KXEFLCHAMPIONSHIP1H",
    "P1": "KXLIGAPORTUGAL1H",
    "N1": "KXEREDIVISIE1H",
}
FIRST_HALF_SPREAD_SERIES = {
    "E0": "KXEPL1HSPREAD", "SP1": "KXLALIGA1HSPREAD", "I1": "KXSERIEA1HSPREAD",
    "D1": "KXBUNDESLIGA1HSPREAD", "F1": "KXLIGUE11HSPREAD", "MLS": "KXMLS1HSPREAD",
    "E1": "KXEFLCHAMPIONSHIP1HSPREAD",
    "P1": "KXLIGAPORTUGAL1HSPREAD",
    "N1": "KXEREDIVISIE1HSPREAD",
}
FIRST_HALF_TOTAL_SERIES = {
    "E0": "KXEPL1HTOTAL", "SP1": "KXLALIGA1HTOTAL", "I1": "KXSERIEA1HTOTAL",
    "D1": "KXBUNDESLIGA1HTOTAL", "F1": "KXLIGUE11HTOTAL", "MLS": "KXMLS1HTOTAL",
    "E1": "KXEFLCHAMPIONSHIP1HTOTAL",
    "P1": "KXLIGAPORTUGAL1HTOTAL",
    "N1": "KXEREDIVISIE1HTOTAL",
}
FIRST_HALF_BTTS_SERIES = {
    "E0": "KXEPL1HBTTS", "SP1": "KXLALIGA1HBTTS", "I1": "KXSERIEA1HBTTS",
    "D1": "KXBUNDESLIGA1HBTTS", "F1": "KXLIGUE11HBTTS", "MLS": "KXMLS1HBTTS",
    "E1": "KXEFLCHAMPIONSHIP1HBTTS",
    "P1": "KXLIGAPORTUGAL1HBTTS",
    "N1": "KXEREDIVISIE1HBTTS",
}

# Second Half confirmed live ONLY for EPL/La Liga on Kalshi (catalog_scan.py
# found no KX{LEAGUE}2H* series for Bundesliga/Ligue1/Serie A/MLS at all --
# a real, confirmed platform gap, not an oversight here) -- Polymarket DOES
# have a real "second-half-result" market for MLS (see
# polymarket_soccer_client.py), so Second Half coverage genuinely differs by
# platform, not just by season.
SECOND_HALF_SERIES = {"E0": "KXEPL2H", "SP1": "KXLALIGA2H"}
SECOND_HALF_SPREAD_SERIES = {"E0": "KXEPL2HSPREAD", "SP1": "KXLALIGA2HSPREAD"}
SECOND_HALF_TOTAL_SERIES = {"E0": "KXEPL2HTOTAL", "SP1": "KXLALIGA2HTOTAL"}
SECOND_HALF_BTTS_SERIES = {"E0": "KXEPL2HBTTS", "SP1": "KXLALIGA2HBTTS"}

FTTS_SERIES = {
    "E0": "KXEPLFTTS", "SP1": "KXLALIGAFTTS", "I1": "KXSERIEAFTTS",
    "D1": "KXBUNDESLIGAFTTS", "F1": "KXLIGUE1FTTS", "MLS": "KXMLSFTTS",
}
SCORE_SERIES = {
    "E0": "KXEPLSCORE", "SP1": "KXLALIGASCORE", "I1": "KXSERIEASCORE",
    "D1": "KXBUNDESLIGASCORE", "F1": "KXLIGUE1SCORE", "MLS": "KXMLSSCORE",
}
TEAMTOTAL_SERIES = {
    "E0": "KXEPLTEAMTOTAL", "SP1": "KXLALIGATEAMTOTAL", "I1": "KXSERIEATEAMTOTAL",
    "D1": "KXBUNDESLIGATEAMTOTAL", "F1": "KXLIGUE1TEAMTOTAL", "MLS": "KXMLSTEAMTOTAL",
    "E1": "KXEFLCHAMPIONSHIPTEAMTOTAL",
}


def _get_half_winner_markets(series: dict, half: int) -> list[dict]:
    """Shared by First/Second Half Winner -- same real 3-way shape as
    get_moneyline_markets, but the tie-side label is "Tie 1st Half"/
    "Tie 2nd Half" (confirmed live for 1st Half via a real KXMLS1H event),
    not the bare "Tie" moneyline uses, and the team-side label is
    "{team} wins 1st/2nd Half", not the bare team name -- genuinely
    different label conventions, not reusable via get_moneyline_markets
    with a parameter swap alone."""
    half_word = "1st Half" if half == 1 else "2nd Half"
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = m.get("yes_sub_title", "")
                if label == f"Tie {half_word}":
                    side, team = "draw", None
                elif label == f"{home_team} wins {half_word}":
                    side, team = "home", home_team
                elif label == f"{away_team} wins {half_word}":
                    side, team = "away", away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "side": side, "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_markets() -> list[dict]:
    return _get_half_winner_markets(FIRST_HALF_SERIES, 1)


def get_second_half_markets() -> list[dict]:
    return _get_half_winner_markets(SECOND_HALF_SERIES, 2)


_HALF_SPREAD_SUB_RE = re.compile(r"^(.+?) wins the (?:1H|2H) by more than [\d.]+ goals$")


def _get_half_spread_markets(series: dict) -> list[dict]:
    """Same shape as get_spread_markets, sub_title says "wins the 1H/2H by
    more than X goals" (confirmed live for 1H via a real KXMLS1HSPREAD
    event) instead of the full-match "wins by more than X goals"."""
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _HALF_SPREAD_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                name = sub_match.group(1)
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "team": team, "line": _to_float(m.get("floor_strike")),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_spread_markets() -> list[dict]:
    return _get_half_spread_markets(FIRST_HALF_SPREAD_SERIES)


def get_second_half_spread_markets() -> list[dict]:
    return _get_half_spread_markets(SECOND_HALF_SPREAD_SERIES)


_HALF_TOTAL_SUB_RE = re.compile(r"^Over ([\d.]+) (?:1H|2H) goals scored$")


def _get_half_total_markets(series: dict) -> list[dict]:
    """Same shape as get_total_markets, sub_title says "Over X.5 1H/2H
    goals scored" (confirmed live for 1H via a real KXMLS1HTOTAL event)."""
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _HALF_TOTAL_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "line": float(sub_match.group(1)),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_total_markets() -> list[dict]:
    return _get_half_total_markets(FIRST_HALF_TOTAL_SERIES)


def get_second_half_total_markets() -> list[dict]:
    return _get_half_total_markets(SECOND_HALF_TOTAL_SERIES)


def _get_half_btts_markets(series: dict) -> list[dict]:
    """Same single-binary-market-per-event shape as get_btts_markets --
    reused directly (title suffix already added to _TITLE_SUFFIX_RE above,
    same "strip the known suffix, then parse teams" pattern)."""
    rows = []
    for division, series_ticker in series.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team, "ticker": m["ticker"],
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


def get_first_half_btts_markets() -> list[dict]:
    return _get_half_btts_markets(FIRST_HALF_BTTS_SERIES)


def get_second_half_btts_markets() -> list[dict]:
    return _get_half_btts_markets(SECOND_HALF_BTTS_SERIES)


def get_ftts_markets() -> list[dict]:
    """First Team To Score -- real 3-way shape confirmed live via KXMLSFTTS
    (home team / away team / "No Goal"), genuinely different tie-analogue
    label ("No Goal", not "Tie") from every other 3-way market here."""
    rows = []
    for division, series_ticker in FTTS_SERIES.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = m.get("yes_sub_title", "")
                if label.lower() == "no goal":
                    side, team = "none", None
                elif label == home_team:
                    side, team = "home", home_team
                elif label == away_team:
                    side, team = "away", away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "side": side, "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_SCORE_SUB_RE = re.compile(r"^(.+?) (\d+) - (\d+) (.+?)$")


def get_correct_score_markets() -> list[dict]:
    """Real ladder confirmed live via KXMLSSCORE (30 real rungs per match,
    e.g. "San Jose Earthquakes wins 2-1" / "Draw 1-1"). yes_sub_title format
    differs for a draw ("Draw H-H") vs a decisive score ("{winner} wins
    H-A") -- home_score/away_score are derived from the TICKER suffix
    instead (e.g. "-SJ1LAG2"), which encodes both sides' goal counts in a
    fixed, unambiguous "{HOME_CODE}{h}{AWAY_CODE}{a}" shape regardless of
    which side won, rather than parsing two different real sub_title
    sentence shapes."""
    rows = []
    for division, series_ticker in SCORE_SERIES.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                ticker = m.get("ticker", "")
                suffix_match = re.search(r"-[A-Z]+(\d+)[A-Z]+(\d+)$", ticker)
                if not suffix_match:
                    continue
                home_score, away_score = int(suffix_match.group(1)), int(suffix_match.group(2))
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": ticker, "home_score": home_score, "away_score": away_score,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


_TEAMTOTAL_SUB_RE = re.compile(r"^(.+?) over ([\d.]+) goals$")


def get_team_total_markets() -> list[dict]:
    """Real ladder confirmed live via KXMLSTEAMTOTAL (one side's OWN goal
    total, e.g. "San Jose over 1.5 goals" -- 3 lines x 2 teams per match)."""
    rows = []
    for division, series_ticker in TEAMTOTAL_SERIES.items():
        for ev in get_open_events(series_ticker):
            title = ev.get("title", "")
            teams = _parse_title_teams(title)
            if teams is None:
                continue
            home_team, away_team = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub_match = _TEAMTOTAL_SUB_RE.match(m.get("yes_sub_title", ""))
                if not sub_match:
                    continue
                name, line = sub_match.group(1), float(sub_match.group(2))
                if name == home_team:
                    team = home_team
                elif name == away_team:
                    team = away_team
                else:
                    continue
                rows.append({
                    "event_ticker": ev["event_ticker"], "division": division,
                    "home_team": home_team, "away_team": away_team,
                    "ticker": m["ticker"], "team": team, "line": line,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows



def get_top_n_markets() -> list[dict]:
    """One row per (threshold, team-in-the-field) -- see TOP_N_SERIES/
    _TOP_N_EVENT_LABELS above for the real event-ticker-suffix -> threshold
    mapping this dispatches on. Same real per-team-market shape as
    get_league_winner_markets/get_relegation_markets (single event per
    threshold, one binary market per team, no title-parsing needed)."""
    rows = []
    for division, series_ticker in TOP_N_SERIES.items():
        for ev in get_open_events(series_ticker):
            event_ticker = ev.get("event_ticker", "")
            suffix = event_ticker.rsplit("-", 1)[-1].replace(str(division), "").lstrip("0123456789")
            label_info = None
            for key, info in _TOP_N_EVENT_LABELS.items():
                if event_ticker.endswith(key):
                    label_info = info
                    break
            if label_info is None:
                continue
            threshold, group_label = label_info
            try:
                markets = get_markets_for_event(event_ticker)
            except Exception:
                continue
            for m in markets:
                team = m.get("yes_sub_title", "")
                if not team:
                    continue
                rows.append({
                    "event_ticker": event_ticker,
                    "division": division,
                    "threshold": threshold,
                    "group_label": group_label,
                    "ticker": m["ticker"],
                    "team": team,
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "volume": _to_float(m.get("volume_fp")),
                    "status": m.get("status"),
                })
    return rows


# ---------------------------------------------------------------------------
# DOMESTIC CUPS (2026-08-08). Different from every league series above in one
# structural way: a cup tie can pair clubs from DIFFERENT divisions, so the
# division is a property of each CLUB, not of the series. Callers get the
# competition and both tiers, and resolve each club's own division themselves
# (app/models/cup_match.py does the rating conversion).
#
# SCOPE IS DELIBERATE. check_cup_market_coverage.py measured the live inventory:
# Coppa Italia is 81% priceable because it starts at Serie A/B, but the DFB
# Pokal is only 40% and structurally capped -- its first round pairs Bundesliga
# clubs with REGIONALLIGA sides (Grossaspach, Hemelingen, Viktoria Cologne,
# Luneburg, St. Tonis), third and fourth tier, two divisions below anything
# football-data publishes for Germany. Both are ingested anyway: unrateable ties
# simply price as None, exactly like an unrated league club, and the Pokal's
# coverage improves in later rounds as the minnows are eliminated. Ingesting
# them cannot produce a bad bet -- the rating gate decides that.
CUP_COMPETITIONS = {
    "coppa_italia": {
        "name": "Coppa Italia",
        "top": "I1", "second": "I2",
        "moneyline": "KXCOPPAITALIAGAME",
        "advance": "KXCOPPAITALIAADVANCE",
        "total": "KXCOPPAITALIATOTAL",
    },
    "dfb_pokal": {
        "name": "DFB Pokal",
        "top": "D1", "second": "D2",
        "moneyline": "KXDFBPOKALGAME",
        "advance": "KXDFBPOKALADVANCE",
        "total": None,  # no live total series for the Pokal as of 2026-08-08
    },
    # EFL Cup (the Carabao Cup -- Kalshi files it under EFL, not the sponsor
    # name, which is why a KXCARABAO* probe returns nothing). Added 2026-08-08
    # after a user asked whether it was covered; it was not, and all four of its
    # series sat dispositioned not_relevant.
    #
    # Same coverage caveat as the DFB Pokal, for the same structural reason:
    # the EFL Cup admits all four English professional tiers, and this app rates
    # only E0 and E1. The live first round is Plymouth (League One) vs Exeter
    # (League Two), neither of which is rateable. Coverage improves sharply from
    # round three, when Premier League clubs enter. Ingested anyway -- an
    # unrateable tie simply prices as None, exactly like an unrated league club,
    # and the alternative is noticing in October that nothing was collected.
    "efl_cup": {
        "name": "EFL Cup",
        "top": "E0", "second": "E1",
        "moneyline": "KXEFLCUPGAME",
        "advance": "KXEFLCUPADVANCE",
        "total": "KXEFLCUPTOTAL",
    },
}

# An ADVANCE event titles itself "Home vs Away: X To Advance", so the pair has
# to be taken from the segment BEFORE the colon -- running _parse_title_teams
# over the whole string would try to read the outcome clause as a team name.
_ADVANCE_SUB_RE = re.compile(r"^(.+?)\s+advances$", re.IGNORECASE)
_REG_TIME_PREFIX = re.compile(r"^Reg(?:ulation)?\s*Time:\s*", re.IGNORECASE)


def _cup_pair(title: str) -> tuple[str, str] | None:
    return _parse_title_teams(title.split(":", 1)[0].strip())


def _cup_row(cup: str, cfg: dict, ev: dict, m: dict, home: str, away: str, **extra) -> dict:
    row = {
        "event_ticker": ev["event_ticker"],
        "event_title": ev.get("title", ""),
        "competition": cup,
        "competition_name": cfg["name"],
        "top_division": cfg["top"],
        "second_division": cfg["second"],
        "home_team": home,
        "away_team": away,
        "ticker": m["ticker"],
        "estimated_start_time": m.get("occurrence_datetime"),
        "yes_bid": _to_float(m.get("yes_bid_dollars")),
        "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")),
        "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }
    row.update(extra)
    return row


def get_cup_moneyline_markets() -> list[dict]:
    """3-way cup moneyline -- home/away/draw at 90 minutes, same shape as
    get_moneyline_markets. NOTE these settle on REGULATION only; who actually
    progresses is the separate ADVANCE series below."""
    rows = []
    for cup, cfg in CUP_COMPETITIONS.items():
        for ev in get_open_events(cfg["moneyline"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                # Cup moneyline labels carry a "Reg Time: " prefix that league
                # ones do not ("Reg Time: Tie", "Reg Time: L.R. Vicenza"). That
                # prefix is also the settlement rule stated out loud: these
                # resolve on 90 minutes, NOT on who eventually progressed.
                label = _REG_TIME_PREFIX.sub("", (m.get("yes_sub_title") or "").strip())
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home:
                    side, team = "home", home
                elif label == away:
                    side, team = "away", away
                else:
                    continue  # never guess which club an unrecognised label means
                rows.append(_cup_row(cup, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_cup_advance_markets() -> list[dict]:
    """Who progresses -- INCLUDING extra time and penalties, which is why this
    cannot be priced off the moneyline (see cup_match._advance_probs)."""
    rows = []
    for cup, cfg in CUP_COMPETITIONS.items():
        for ev in get_open_events(cfg["advance"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                sub = _ADVANCE_SUB_RE.match((m.get("yes_sub_title") or "").strip())
                if not sub:
                    continue
                who = sub.group(1).strip()
                if who == home:
                    side, team = "home", home
                elif who == away:
                    side, team = "away", away
                else:
                    continue
                rows.append(_cup_row(cup, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_cup_total_markets() -> list[dict]:
    """Over/under total goals in REGULATION. The pair is not in the market
    title here ("Will over 4.5 goals be scored?"), so it comes from the EVENT
    title, and the line comes from yes_sub_title."""
    rows = []
    for cup, cfg in CUP_COMPETITIONS.items():
        series = cfg.get("total")
        if not series:
            continue
        for ev in get_open_events(series):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                mt = re.search(r"([\d.]+)", m.get("yes_sub_title") or m.get("title") or "")
                if not mt:
                    continue
                try:
                    line = float(mt.group(1))
                except ValueError:
                    continue
                rows.append(_cup_row(cup, cfg, ev, m, home, away, line=line, side="over"))
    return rows


# --- UEFA CLUB COMPETITIONS (2026-08-08) -----------------------------------
# Cross-COUNTRY, so unlike the domestic cups above there is no "top/second tier"
# pair -- each club's league is resolved individually and converted with the
# fitted strength offsets (app/models/uefa_match.py).
#
# ADVANCE IS DELIBERATELY NOT INGESTED. KXUCLADVANCE and friends exist and have
# live inventory, but UEFA knockout ties are decided over TWO LEGS plus extra
# time, so "to advance" depends on an aggregate score across two matches. The
# single-leg formula that prices domestic cup advancement would be wrong here
# (see uefa_match.py's own docstring), and pricing it off the single-match
# distribution would be worse than not pricing it. GAME and TOTAL settle on one
# match's regulation result and are fine.
#
# SPREAD is also skipped for now: its yes_sub_title uses a different shape from
# the league spread parser ("Goal Diff Reg Time: <team> ...") and there are only
# 16 live rows, so it is not worth a bespoke parser until the league phase.
UEFA_COMPETITIONS = {
    "ucl": {"name": "Champions League", "moneyline": "KXUCLGAME", "total": "KXUCLTOTAL"},
    "uel": {"name": "Europa League", "moneyline": "KXUELGAME", "total": "KXUELTOTAL"},
    "uecl": {"name": "Conference League", "moneyline": "KXUECLGAME", "total": "KXUECLTOTAL"},
}


def _uefa_row(comp: str, cfg: dict, ev: dict, m: dict, home: str, away: str, **extra) -> dict:
    row = {
        "event_ticker": ev["event_ticker"], "event_title": ev.get("title", ""),
        "competition": comp, "competition_name": cfg["name"],
        "home_team": home, "away_team": away,
        "ticker": m["ticker"], "estimated_start_time": m.get("occurrence_datetime"),
        "yes_bid": _to_float(m.get("yes_bid_dollars")), "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "last_price": _to_float(m.get("last_price_dollars")), "volume": _to_float(m.get("volume_fp")),
        "status": m.get("status"),
    }
    row.update(extra)
    return row


def get_uefa_moneyline_markets() -> list[dict]:
    """Regulation-time 3-way for a single UEFA match. Labels carry the same
    "Reg Time: " prefix the domestic cups use."""
    rows = []
    for comp, cfg in UEFA_COMPETITIONS.items():
        for ev in get_open_events(cfg["moneyline"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                label = _REG_TIME_PREFIX.sub("", (m.get("yes_sub_title") or "").strip())
                if label.lower() == "tie":
                    side, team = "draw", None
                elif label == home:
                    side, team = "home", home
                elif label == away:
                    side, team = "away", away
                else:
                    continue
                rows.append(_uefa_row(comp, cfg, ev, m, home, away, side=side, team=team))
    return rows


def get_uefa_total_markets() -> list[dict]:
    """Over/under total goals in regulation."""
    rows = []
    for comp, cfg in UEFA_COMPETITIONS.items():
        for ev in get_open_events(cfg["total"]):
            teams = _cup_pair(ev.get("title", ""))
            if teams is None:
                continue
            home, away = teams
            try:
                markets = get_markets_for_event(ev["event_ticker"])
            except Exception:
                continue
            for m in markets:
                mt = re.search(r"([\d.]+)", m.get("yes_sub_title") or m.get("title") or "")
                if not mt:
                    continue
                try:
                    line = float(mt.group(1))
                except ValueError:
                    continue
                rows.append(_uefa_row(comp, cfg, ev, m, home, away, line=line, side="over"))
    return rows
