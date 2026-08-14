"""ESPN's free, unauthenticated public scoreboard API -- confirmed live
2026-07-19 for MLS ("usa.1" slug): `site.api.espn.com/apis/site/v2/sports/
soccer/usa.1/scoreboard?dates=YYYYMMDD-YYYYMMDD` returns real historical
match results (final score, date, home/away team) for a date range with no
API key. This is the only free source found for MLS during the live audit --
football-data.co.uk does not cover MLS at all (confirmed: no `mlsm.php` or
equivalent page). Results ONLY, no odds -- MLS rows built from this client
never get home_odds/draw_odds/away_odds populated (see SoccerMatch's
docstring in app/db/models.py), so MLS can never clear the backtest gate the
way the 5 football-data.co.uk-sourced leagues can.

A single `dates=YYYYMMDD` (no range) query was confirmed live to return ZERO
events even on a date with real MLS games recorded via the range query --
this endpoint apparently doesn't reliably serve single-day queries the same
way as a range, so this client always queries in ranges even when the caller
wants one day's worth of results.

The maximum date-range width this endpoint accepts is NOT documented and was
NOT stress-tested during the audit (only a 10-day window was confirmed to
work) -- fetch_season_range chunks conservatively in ~25-day windows rather
than assuming a wider range is safe, to avoid a silent truncation that would
look like "no games happened" instead of a real fetch limit."""
from __future__ import annotations

import datetime as dt
import re

import httpx

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard"
CHUNK_DAYS = 25

# Real live standings feed (confirmed 2026-07-19): one request per league
# returns the WHOLE table (rank/points/games played per team) -- 20 real
# rows for E0/SP1/I1, 18 for D1/F1, matching each league's own real team
# count exactly. Keyed by THIS app's own football-data.co.uk-style codes for
# a direct join against SoccerMatch.league, same convention as
# transfermarkt_client.py's COMPETITION_CODES. MLS deliberately excluded --
# confirmed live its own standings response is conference-split (15 teams in
# "children[0]", not the real 30-team single table), a genuinely different
# structure this app's motivation signal (see motivation_rules_soccer.py)
# isn't built to handle, same "MLS is structurally different, scope it out"
# precedent as season_sim_soccer.py's own relegation/top4 scope.
STANDINGS_LEAGUE_CODES = {
    "E0": "eng.1",
    "SP1": "esp.1",
    "I1": "ita.1",
    "D1": "ger.1",
    "F1": "fra.1",
}


# All 6 leagues' ESPN slugs -- MLS repeats usa.1 (already hardcoded into
# SCOREBOARD_URL above for the historical-cache builder; kept as its own
# separate constant there rather than refactored to reuse this dict, to
# avoid touching that already-working pipeline). Used by
# refresh_soccer_results (poller_soccer.py) to backfill REAL final scores
# for live-tracked matches across every league, not just MLS -- confirmed
# live 2026-07-19 the same scoreboard endpoint pattern generalizes cleanly
# to every non-MLS league too (e.g. a real Crystal Palace 1-1 Fulham result
# for eng.1 on 2026-01-01).
LEAGUE_CODES = {
    **STANDINGS_LEAGUE_CODES,
    "MLS": "usa.1",
    # REAL BUG this fixes (2026-08-08). This map had only the original six
    # leagues, but refresh_soccer_results uses it to decide which leagues to
    # FETCH results for at all -- so every league added since was invisible to
    # it and its matches could never resolve, no matter how long they waited.
    # Measured: 202 unresolved live rows across 13 leagues, 134 of them in
    # leagues absent from this dict. That included P1, which is why a user's
    # 20 Maritimo bets sat pending on a match that had finished hours earlier:
    # Liga Portugal results were never requested. The duplicate-fixture row
    # for that match was real but not the cause.
    #
    # Every slug below was verified live against ESPN's scoreboard earlier the
    # same day while building the alias maps and the cup/UEFA pipelines --
    # these are not guesses at ESPN's naming.
    "P1": "por.1",       # Liga Portugal
    "N1": "ned.1",       # Eredivisie
    "E1": "eng.2",       # EFL Championship
    "B1": "bel.1",       # Belgian Pro League
    "T1": "tur.1",       # Turkish Super Lig
    "G1": "gre.1",       # Greek Super League
    "D2": "ger.2",       # 2. Bundesliga
    "I2": "ita.2",       # Serie B
    "SP2": "esp.2",      # Segunda Division
    "F2": "fra.2",       # Ligue 2
    "SC0": "sco.1",      # Scottish Premiership
    "E2": "eng.3",       # League One
    "E3": "eng.4",       # League Two
    # Extra-format leagues, added with the parser. Verified live:
    # bra.1 40 events, arg.1 78, mex.1 33, jpn.1 40 in August 2026.
    "BRA1": "bra.1",      # Brasileirao Serie A
    # 2026-08-08: verified live before wiring -- 37/28/28/39 events
    # respectively in a one-month window. Poland and Switzerland were REFUSED
    # at this step (no feed / zero events), which is why they are absent from
    # football_data_client.EXTRA_DIVISIONS too.
    "SWE1": "swe.1", "NOR1": "nor.1", "DNK1": "den.1", "CHN1": "chn.1",
    # Saudi Pro League, 2026-08-14. Its RATINGS have been in the pool since the
    # ESPN wave-2 build (scripts/build_espn_soccer_league_caches.py maps
    # KSA1 -> ksa.1), but that build only sourced history -- it never added a
    # settlement slug here, because those 15 leagues were admitted to support
    # UEFA/cup pricing rather than to trade in their own right. Wiring the
    # KXSAUDIPL* market series without this entry would produce bets that can
    # never settle, which is exactly why Poland and Switzerland were refused at
    # this same step. Verified live before adding: ksa.1 returned 10 events in
    # the Aug 4-24 window.
    "KSA1": "ksa.1",
    # Leagues Cup, 2026-08-08. Present so its bets can SETTLE -- this dict is
    # what refresh_soccer_results iterates, so a competition missing from it
    # produces markets that stay pending forever. The slug is "concacaf.
    # leagues.cup" (dots, not underscores); "concacaf.leagues_cup" and
    # "usa.leagues_cup" both 400.
    "LEAGUES_CUP": "concacaf.leagues.cup",
    # National teams (2026-08-09). Fixtures are stored under "INTL", so this is
    # the slug refresh_soccer_results uses to settle them. It is the ASEAN
    # Championship because that is the only national-team competition currently
    # listed; a second one would need its own entry, since this map is keyed by
    # the stored league code and INTL can only point at one slug.
    #
    # THAT IS A REAL LIMITATION, not a nicety: storing every national fixture
    # under one code means results can only be fetched from one competition at a
    # time. It is correct today and needs revisiting the moment a second
    # national-team competition is ingested.
    "INTL": "aff.championship",
    "ARG1": "arg.1",     # Liga Profesional
    "MEX1": "mex.1",     # Liga MX
    "JPN1": "jpn.1",     # J1 League
    # Cup and continental competitions are stored under their COMPETITION as
    # the league code (see market_catalog_soccer.cup_league_code /
    # uefa_league_code), so they key in here the same way a league does.
    "COPPA_ITALIA": "ita.coppa_italia",
    "DFB_POKAL": "ger.dfb_pokal",
    # Kalshi calls it EFL Cup, ESPN calls it eng.league_cup, the sponsor calls
    # it the Carabao Cup. Verified live before wiring -- eng.efl_cup and
    # eng.carabao_cup both 404.
    "EFL_CUP": "eng.league_cup",
    "UEFA_SUPER_CUP": "uefa.super_cup",
    "FRA_SUPER_CUP": "fra.super_cup",
    "UCL": "uefa.champions",
    "UEL": "uefa.europa",
    "UECL": "uefa.europa.conf",
}

# ADDITIONAL scoreboards for a league code that ESPN splits across more than one
# competition. Fetched alongside the primary slug above and merged, deduped by
# event id.
#
# THE BUG THIS FIXES (2026-08-13), caught live by the user. Hearts v Benfica was
# in the cross-sport recommendations while it was being played: stored kickoff
# 21:45Z, real kickoff 18:45Z, ESPN showing "First Half, 42'". The +3h is the
# known Kalshi soccer signature (occurrence_datetime is the market EXPIRATION,
# not the start), and the precedence ladder that normally overrides it with a
# real ESPN kickoff had nothing to use -- because in August the UEFA competitions
# are in QUALIFYING, and uefa.champions / uefa.europa / uefa.europa.conf all
# return ZERO events. The qualifying rounds live on their own slugs.
#
# THE SETTLEMENT HALF IS THE BIGGER ONE. This dict's primary is also what
# refresh_soccer_results iterates, so a UEFA qualifying match could not be graded
# either -- exactly the failure the P1/Maritimo comment above describes, where 20
# bets sat pending because results were never requested for that competition.
# Being in LEAGUE_CODES was not enough; the SLUG has to be one that actually
# lists the match.
#
# Verified live before wiring, not guessed: uefa.europa_qual returned 11 events
# for 2026-08-13 including this exact match with its true 18:45Z kickoff and a
# live status, and uefa.europa.conf_qual returned 25. uefa.champions_qual
# returned 0 for that date and is kept anyway -- CL qualifying runs on different
# dates and an empty response is harmless. "uefa.europa.conf_q" and
# "uefa.champions.qual" both 400, so the naming is _qual on the full slug.
EXTRA_LEAGUE_CODES = {
    "UCL": ("uefa.champions_qual",),
    "UEL": ("uefa.europa_qual",),
    "UECL": ("uefa.europa.conf_qual",),
}


def _slugs_for(league: str) -> tuple[str, ...]:
    """Every ESPN competition slug that may carry this league's fixtures."""
    primary = LEAGUE_CODES.get(league)
    if primary is None:
        return ()
    return (primary,) + tuple(EXTRA_LEAGUE_CODES.get(league, ()))


# ESPN's scoreboard returns AT MOST 100 events per call and silently truncates
# from the START of the requested range -- there is no page cursor and no
# indication in the body that anything was dropped.
#
# REAL BUG this caused (found 2026-08-06): refresh_soccer_results asked for one
# window spanning every unresolved match (2026-03-07 -> today, five months). It
# got back exactly 100 events covering 2026-03-07 to 2026-04-25, so every MLS
# result after April was invisible and NOT ONE of 74 already-played matches
# could ever be backfilled. The truncation is silent, so this read as "ESPN has
# no result for these games" rather than "we never asked past April".
#
# 21 days is comfortably inside the cap for the densest league here (MLS peaks
# around 12 fixtures a week, so ~36 per chunk against a limit of 100).
_CHUNK_DAYS = 21


def fetch_scoreboard(league: str, start: dt.date, end: dt.date) -> list[dict]:
    """Real ESPN results for a league over a date range, fetched in chunks and
    de-duplicated by event id (chunk edges can repeat an event).

    Note the endpoint needs a RANGE: `dates=YYYYMMDD-YYYYMMDD` with start ==
    end returns 0 events even on a day that definitely had fixtures, so a
    single-day query is not a valid way to probe this API."""
    slugs = _slugs_for(league)
    if not slugs or start > end:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    # Several competitions are split across more than one ESPN slug (UEFA
    # qualifying vs league phase) -- see EXTRA_LEAGUE_CODES. The existing
    # event-id dedupe already covers the overlap, so merging is safe.
    for code in slugs:
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + dt.timedelta(days=_CHUNK_DAYS - 1), end)
            params = {"dates": f"{chunk_start.strftime('%Y%m%d')}-{chunk_end.strftime('%Y%m%d')}"}
            try:
                resp = httpx.get(
                    f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard",
                    params=params, timeout=30.0,
                )
                resp.raise_for_status()
                events = resp.json().get("events", [])
            except Exception:
                events = []
            for e in events:
                eid = str(e.get("id") or "")
                if eid and eid in seen:
                    continue
                if eid:
                    seen.add(eid)
                out.append(e)
            chunk_start = chunk_end + dt.timedelta(days=1)
    return out


# Clock strings on ESPN's scoring plays are football minutes, and they are NOT
# numerically sortable as text: a 90th-minute goal reads "90'", stoppage time
# reads "45+2'" or "90+4'", and "9'" sorts after "45'" lexically. So they are
# parsed to a comparable number, with stoppage added as a fraction so that
# 45+2 sits after 45 but before 46.
def _clock_minutes(v) -> float | None:
    m = re.match(r"^\s*(\d+)(?:\s*\+\s*(\d+))?", str(v or ""))
    if not m:
        return None
    base = int(m.group(1))
    extra = int(m.group(2)) if m.group(2) else 0
    return base + extra / 100.0


def _first_scorer(comp: dict, home_id, away_id) -> str | None:
    """'H' / 'A' for whichever side scored first, 'N' for a goalless match,
    None when ESPN gave us no usable detail.

    'N' and None are deliberately different: 'N' is a real answer (nobody
    scored, so a First-Team-To-Score bet on either side loses), while None
    means we do not know and the bet must stay pending rather than be guessed.
    """
    details = comp.get("details")
    if details is None:
        return None  # no play-by-play for this match -- unknown, not goalless
    scoring = []
    for d in details:
        if not d.get("scoringPlay"):
            continue
        minute = _clock_minutes((d.get("clock") or {}).get("displayValue"))
        team_id = str(((d.get("team") or {}).get("id")) or "")
        if minute is None or not team_id:
            continue
        scoring.append((minute, team_id))
    if not scoring:
        # An empty detail list on a finished match is a real 0-0; but if the
        # match had goals, the feed simply lacks them and we must not claim 0-0.
        return None
    scoring.sort(key=lambda x: x[0])
    first_team = scoring[0][1]
    if first_team == str(home_id):
        return "H"
    if first_team == str(away_id):
        return "A"
    return None


def parse_final_results(raw_events: list[dict]) -> list[dict]:
    """Simplified sibling of parse_final_events -- only the fields needed to
    MATCH a real ESPN result onto an already-existing, live-tracked
    SoccerMatch row (team names, date, final score), not to build a fresh
    historical-cache row (no source_match_id/season/league needed here,
    those are already known at the call site)."""
    out = []
    for e in raw_events:
        comps = e.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status_name = ((comp.get("status") or {}).get("type") or {}).get("name")
        if status_name != "STATUS_FULL_TIME":
            continue
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue
        try:
            home_goals = int(home.get("score"))
            away_goals = int(away.get("score"))
        except (TypeError, ValueError):
            continue
        home_name = (home.get("team") or {}).get("displayName")
        away_name = (away.get("team") or {}).get("displayName")
        if not home_name or not away_name:
            continue
        event_date = e.get("date")
        match_date = event_date[:10] if event_date else None
        if match_date is None:
            continue
        result_ft = "H" if home_goals > away_goals else ("A" if away_goals > home_goals else "D")
        first_scorer = _first_scorer(comp, (home.get("team") or {}).get("id"),
                                     (away.get("team") or {}).get("id"))
        if first_scorer is None and home_goals == 0 and away_goals == 0:
            first_scorer = "N"  # a real goalless match, corroborated by the score
        out.append({
            "home_team": home_name, "away_team": away_name, "match_date": match_date,
            "home_goals_ft": home_goals, "away_goals_ft": away_goals, "result_ft": result_ft,
            # Who scored FIRST -- ftts markets cannot be graded from a final
            # score. Carried from the same scoreboard payload this call already
            # fetches, so it costs no extra request.
            "first_scorer": first_scorer,
            # Needed to fetch half-time goals, which the scoreboard does not
            # carry -- see fetch_half_time_goals.
            "event_id": e.get("id"),
        })
    return out


# ESPN's status.type.state, which is the coarse three-way we actually want.
# "in" covers every in-play variant (STATUS_IN_PROGRESS, STATUS_FIRST_HALF,
# STATUS_HALFTIME, ...) without this having to enumerate them, and enumerating
# them is precisely how a new one gets missed.
_LIVE_STATE = "in"
_DONE_STATE = "post"


def parse_kickoffs(raw_events: list[dict]) -> list[dict]:
    """Real kickoff time and in-play state for EVERY event on the scoreboard,
    not just finished ones.

    REAL BUG THIS EXISTS FOR (user-reported 2026-08-09, a live match offered as
    a bet). Nuremberg vs Dresden was recommended -- $10 on Dresden away at 0.15
    -- while the match was live at 45'+2' with Dresden already 1-0 down. The
    "edge" of +15.5pp was an artifact of comparing a PRE-MATCH model
    probability against a LIVE price.

    Kalshi was the source of the error and it was not stale data on our side:
    Kalshi's own occurrence_datetime for that event said 2026-08-09T14:30:00Z
    against a real 11:30Z kickoff, and its expected_expiration_time said 14:30Z
    too -- so the usual "prefer expected_expiration_time" workaround could not
    have caught it either. Every D2 fixture that day carried the same wrong
    time.

    Both live guards then failed, for different reasons:
      * the start-time guard could not fire, because the time it was handed was
        three hours in the future;
      * the trading-based guard (looks_already_live_by_trading) only fires at an
        EXTREME price -- it is built to catch a DECIDED match (0.02/0.98), and a
        1-0 game at 0.15 is merely in progress. Correct by design, silent here.

    ESPN is the authority on when a match actually kicked off, this app already
    trusts it for settlement, and the poller ALREADY fetches this exact payload
    for results -- so correcting the start time costs no extra request. That is
    why the fix belongs here rather than in another platform workaround.

    Returns the same team-name/date shape parse_final_results does, so the
    caller can reuse the alias-joining it already has.
    """
    out = []
    for e in raw_events:
        comps = e.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue
        home_name = (home.get("team") or {}).get("displayName")
        away_name = (away.get("team") or {}).get("displayName")
        if not home_name or not away_name:
            continue
        kickoff = e.get("date")  # ISO-8601 UTC, e.g. "2026-08-09T11:30Z"
        if not kickoff:
            continue
        state = (((comp.get("status") or {}).get("type") or {}).get("state") or "").lower()
        out.append({
            "home_team": home_name,
            "away_team": away_name,
            "match_date": kickoff[:10],
            "kickoff": kickoff,
            "state": state,
            "is_live": state == _LIVE_STATE,
            "is_done": state == _DONE_STATE,
            "event_id": e.get("id"),
        })
    return out


def fetch_half_time_goals(league: str, event_id: str) -> tuple[int, int] | None:
    """(home_goals_ht, away_goals_ht) for one finished match, or None.

    Half-time goals are NOT on the scoreboard endpoint -- every competitor
    there has `linescores: null` (checked 2026-08-06: 0 of 37 finished MLS
    events carried them). They ARE on the per-event SUMMARY endpoint, under
    header.competitions[].competitors[].linescores, one entry per period with
    index 0 = first half.

    Validated on 16 finished MLS matches: the two halves summed to the final
    score on 16 of 16, none missing (e.g. FC Cincinnati 4 = [3,1] vs Vancouver
    3 = [2,1]).

    Costs ONE request per match, unlike the chunked scoreboard, so callers
    should only ask for matches they actually need to grade.
    """
    code = LEAGUE_CODES.get(league)
    if code is None or not event_id:
        return None
    try:
        resp = httpx.get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/summary",
            params={"event": event_id}, timeout=30.0,
        )
        resp.raise_for_status()
        comps = ((resp.json().get("header") or {}).get("competitions")) or []
        if not comps:
            return None
        goals = {}
        for c in comps[0].get("competitors") or []:
            lines = c.get("linescores") or []
            if not lines:
                return None
            try:
                goals[c.get("homeAway")] = int(float(lines[0].get("displayValue")))
            except (TypeError, ValueError):
                return None
        if "home" not in goals or "away" not in goals:
            return None
        return goals["home"], goals["away"]
    except Exception:
        return None


def fetch_standings(league: str) -> dict[str, dict]:
    """{team_display_name: {rank, points, games_played}} for one league's
    CURRENT real table. Returns {} on any failure or for an unrecognized/
    unsupported league (e.g. MLS) -- a missing standings table degrades the
    motivation signal to "no adjustment" for that match, not a crash."""
    code = STANDINGS_LEAGUE_CODES.get(league)
    if code is None:
        return {}
    try:
        resp = httpx.get(f"https://site.api.espn.com/apis/v2/sports/soccer/{code}/standings", timeout=15.0)
        resp.raise_for_status()
        entries = resp.json()["children"][0]["standings"]["entries"]
    except Exception:
        return {}

    out = {}
    for entry in entries:
        team_name = (entry.get("team") or {}).get("displayName")
        if not team_name:
            continue
        stats = {s["name"]: s.get("value") for s in entry.get("stats", [])}
        rank, points, games_played = stats.get("rank"), stats.get("points"), stats.get("gamesPlayed")
        if rank is None or points is None or games_played is None:
            continue
        out[team_name] = {"rank": int(rank), "points": int(points), "games_played": int(games_played)}
    return out


def fetch_range(start: dt.date, end: dt.date) -> list[dict]:
    """Raw ESPN event dicts for [start, end] inclusive. Returns [] (not
    raises) on a non-200 or malformed response -- a gap in MLS history is
    less harmful than crashing the whole cache build over one bad window."""
    params = {"dates": f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"}
    try:
        resp = httpx.get(SCOREBOARD_URL, params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception:
        return []


def fetch_season_range(start: dt.date, end: dt.date) -> list[dict]:
    """Chunks [start, end] into CHUNK_DAYS-wide windows (see module
    docstring on why the real max width is unverified) and concatenates."""
    events: list[dict] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=CHUNK_DAYS - 1), end)
        events.extend(fetch_range(cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return events


# ---------------------------------------------------------------------------
# GENERALISED, PER-SLUG VARIANTS (2026-08-12)
#
# The three functions above are hardcoded to SCOREBOARD_URL (usa.1) and stamp
# league="MLS", because MLS was the only ESPN-sourced league when they were
# written. Everything about them generalises -- the scoreboard shape is
# identical for every league slug -- so these take the slug and the league code
# rather than duplicating the parser per league.
#
# WHY THIS MATTERS: ESPN turns out to serve real, deep history for many leagues
# football-data.co.uk does not cover. Verified 2026-08-12 over 2022-2025
# completed matches: Colombia col.1 1,787 · USL usa.usl.1 1,693 · Uruguay
# uru.1 1,195 · Romania rou.1 1,184 · Guatemala gua.1 1,039 · Ecuador ecu.1
# 1,038 · Costa Rica crc.1 1,036 · Venezuela ven.1 961 · Saudi ksa.1 952 ·
# South Africa rsa.1 906 · Austria aut.1 774 · Switzerland sui.1 768 ·
# A-League aus.1 700 · Ireland irl.1 697 · NWSL usa.nwsl 652.
#
# NOTE the ESPN core-API league index (sports.core.api.espn.com/v2/sports/
# soccer/leagues, 214 entries) is a LOWER BOUND, not the truth: irl.1, rou.1
# and sui.1 all serve scoreboard data without appearing in it. Probe the
# scoreboard to decide whether a league is usable; use the index only to
# discover candidate slugs.
_SCOREBOARD_TEMPLATE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"


def fetch_range_for(slug: str, start: dt.date, end: dt.date) -> list[dict]:
    """fetch_range for an arbitrary league slug. Same swallow-and-return-[]
    posture: one bad window must not abort a multi-year cache build."""
    params = {"dates": f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}", "limit": 1000}
    try:
        resp = httpx.get(_SCOREBOARD_TEMPLATE.format(slug=slug), params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception:
        return []


def fetch_season_range_for(slug: str, start: dt.date, end: dt.date) -> list[dict]:
    events: list[dict] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=CHUNK_DAYS - 1), end)
        events.extend(fetch_range_for(slug, cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return events


def parse_final_events_for(raw_events: list[dict], league: str,
                           season_label) -> list[dict]:
    """parse_final_events for an arbitrary league.

    `season_label` is a callable(match_date) -> str rather than a constant,
    because THE SEASON STRING IS NOT COSMETIC: elo_soccer's
    start_season_if_new() fires SEASON_REGRESSION (1/3 of the way back to
    league average) every time it changes. Label a split-year league by
    calendar year and every club gets regressed mid-season, every season.
    Callers derive the label from each league's own observed match-month
    distribution -- see build_espn_league_cache."""
    out = []
    for e in raw_events:
        comps = e.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        if (((comp.get("status") or {}).get("type") or {}).get("name")) != "STATUS_FULL_TIME":
            continue
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue
        try:
            home_goals = int(home.get("score"))
            away_goals = int(away.get("score"))
        except (TypeError, ValueError):
            continue
        home_name = (home.get("team") or {}).get("displayName")
        away_name = (away.get("team") or {}).get("displayName")
        if not home_name or not away_name:
            continue
        event_date = e.get("date")
        match_date = event_date[:10] if event_date else None
        if match_date is None:
            continue
        out.append({
            "source": "espn",
            "source_match_id": f"espn:{league}:{e.get('id')}",
            "league": league,
            "season": season_label(match_date),
            "match_date": match_date,
            "home_team": home_name,
            "away_team": away_name,
            "home_goals_ft": home_goals,
            "away_goals_ft": away_goals,
            "home_goals_ht": None,
            "away_goals_ht": None,
            "result_ft": "H" if home_goals > away_goals else ("A" if away_goals > home_goals else "D"),
            "home_odds": None,
            "draw_odds": None,
            "away_odds": None,
        })
    return out


def parse_final_events(raw_events: list[dict]) -> list[dict]:
    """Keeps only events ESPN marks fully completed ("STATUS_FULL_TIME") --
    in-progress/scheduled/postponed rows have no real final score to train
    on. Returns SoccerMatch-shaped dicts (source="espn", league="MLS", no
    odds fields -- see module docstring)."""
    out = []
    for e in raw_events:
        comps = e.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status_name = ((comp.get("status") or {}).get("type") or {}).get("name")
        if status_name != "STATUS_FULL_TIME":
            continue
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home is None or away is None:
            continue
        try:
            home_goals = int(home.get("score"))
            away_goals = int(away.get("score"))
        except (TypeError, ValueError):
            continue
        home_name = ((home.get("team") or {}).get("displayName"))
        away_name = ((away.get("team") or {}).get("displayName"))
        if not home_name or not away_name:
            continue
        event_date = e.get("date")  # ISO instant, e.g. "2026-04-05T23:00Z"
        match_date = event_date[:10] if event_date else None
        if match_date is None:
            continue
        result_ft = "H" if home_goals > away_goals else ("A" if away_goals > home_goals else "D")
        out.append({
            "source": "espn",
            "source_match_id": f"espn:{e.get('id')}",
            "league": "MLS",
            "season": _season_label(match_date),
            "match_date": match_date,
            "home_team": home_name,
            "away_team": away_name,
            "home_goals_ft": home_goals,
            "away_goals_ft": away_goals,
            "home_goals_ht": None,  # ESPN's scoreboard payload doesn't expose a half-time score
            "away_goals_ht": None,
            "result_ft": result_ft,
            "home_odds": None,  # see module docstring -- MLS has no free odds source
            "draw_odds": None,
            "away_odds": None,
        })
    return out


def _season_label(iso_date: str) -> str:
    """MLS runs within a single calendar year (Feb-Dec), unlike the
    Aug-May European season football-data.co.uk covers -- season label is
    just the match's own year, not a two-year span."""
    year = int(iso_date[:4])
    return str(year)
