"""ESPN WNBA schedule client (free public API, same host the NBA/MLB clients
use). Parallel to espn_nba_client.py but far simpler -- WNBA needs only the
schedule/scoreboard for the moneyline Elo build; no injuries/coach/standings
layer is wired for WNBA yet (moneyline-only scope, see poller_wnba.py).
"""
import datetime
import logging

import httpx

log = logging.getLogger("espn_wnba_client")

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
_UA = {"User-Agent": "Mozilla/5.0"}


FORWARD_DAYS = 14


def fetch_scoreboard_events(start: datetime.date, end: datetime.date,
                            respect_horizon: bool = True) -> list[dict]:
    """Raw ESPN event dicts across [start, end], one scoreboard call per day.
    WNBA plays ~mid-May through mid-Oct; callers pass a season-wide window.

    REAL BUG fixed 2026-08-02: the loop stopped at `datetime.date.today()`, so it
    only ever ingested games up to TODAY and the schedule could never contain an
    UPCOMING game. Kalshi lists a game's markets a day or more ahead, so those
    markets had no game row to link to -- they stayed unlinked, couldn't be priced
    or settled, and showed up as the standing health-check warning "N active WNBA
    Kalshi market(s) with no game/match link". Confirmed live: ESPN's 2026-08-03
    scoreboard returns LV@ATL, SEA@NY and PHX@CHI (exactly the pairings Kalshi was
    pricing) while our table had only TOR@GS that day -- and TOR@GS was there only
    because it's a late tip that falls on Aug 3 in UTC but appears on the Aug 2
    scoreboard.

    Bounded to FORWARD_DAYS ahead rather than the caller's full season `end`: the
    fetch is one HTTP call per day, so honouring a season-wide end would add ~100
    calls per refresh for schedule that barely changes. Two weeks comfortably
    covers the window in which markets get listed."""
    # respect_horizon=False is for the SEASON SIM, which needs the whole
    # remaining schedule. The clamp is right for the poller (one HTTP call per
    # day, and markets only list ~2 weeks out) but silently truncates anything
    # asking a season-wide question: measured 2026-08-02, the clamp left teams
    # with at most 38 of their 44 games, which understates every win total in a
    # way nothing surfaces as an error.
    horizon = (datetime.date.today() + datetime.timedelta(days=FORWARD_DAYS)
               if respect_horizon else end)
    out = []
    with httpx.Client(timeout=30.0, headers=_UA) as client:
        day = start
        while day <= end and day <= horizon:
            try:
                r = client.get(SCOREBOARD, params={"dates": day.strftime("%Y%m%d")})
                if r.status_code == 200:
                    out.extend(r.json().get("events", []))
            except httpx.HTTPError:
                pass
            day += datetime.timedelta(days=1)
    return out
