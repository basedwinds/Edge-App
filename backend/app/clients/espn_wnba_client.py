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


def fetch_scoreboard_events(start: datetime.date, end: datetime.date) -> list[dict]:
    """Raw ESPN event dicts across [start, end], one scoreboard call per day.
    WNBA plays ~mid-May through mid-Oct; callers pass a season-wide window."""
    out = []
    with httpx.Client(timeout=30.0, headers=_UA) as client:
        day = start
        while day <= end and day <= datetime.date.today():
            try:
                r = client.get(SCOREBOARD, params={"dates": day.strftime("%Y%m%d")})
                if r.status_code == 200:
                    out.extend(r.json().get("events", []))
            except httpx.HTTPError:
                pass
            day += datetime.timedelta(days=1)
    return out
