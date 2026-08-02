"""ESPN college-football schedule client (free public API, same host the
NBA/WNBA/MLB clients use). Parallel to espn_wnba_client.py.

Two CFB-specific things, both confirmed live 2026-08-02:

`groups=80` restricts to FBS. Without it the scoreboard also returns FCS,
Division II and Division III games, which have no Elo ratings here (the game
cache the constants were derived from is FBS-only) and would flood the table
with teams we can never price.

CFB is a WEEKLY sport, so this fetches by week-sized strides rather than the
day-at-a-time loop the WNBA client uses -- ESPN's scoreboard accepts a
YYYYMMDD-YYYYMMDD range and returns every game inside it, so a whole season
costs a handful of calls instead of ~150. Verified: the 2026-09-19 slate alone
returns 71 events.
"""
import datetime
import logging

import httpx

log = logging.getLogger("espn_cfb_client")

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
_UA = {"User-Agent": "Mozilla/5.0"}

# FBS only -- see module docstring.
FBS_GROUP = "80"
# ESPN caps the scoreboard response; 900 comfortably clears a full Saturday
# (the busiest CFB day tops out near 130 FBS games).
_LIMIT = 900
# How far ahead to pull. This MUST exceed Kalshi's listing horizon or markets
# arrive with no game row to link to -- the exact bug that left WNBA markets
# unlinked when its client stopped at today.
# Measured 2026-08-02: Kalshi had already listed KXNCAAFGAME markets closing
# 2026-09-21, i.e. 50 days out, while a 45-day window only reached Sept 13. 90
# gives real headroom over that observed horizon; the fetch is a handful of
# range calls, so a wide window is cheap.
FORWARD_DAYS = 90
_STRIDE_DAYS = 14


def fetch_scoreboard_events(start: datetime.date, end: datetime.date) -> list[dict]:
    """Raw ESPN event dicts across [start, end], fetched in date-range strides."""
    out: list[dict] = []
    with httpx.Client(timeout=30.0, headers=_UA) as client:
        day = start
        while day <= end:
            chunk_end = min(day + datetime.timedelta(days=_STRIDE_DAYS - 1), end)
            try:
                r = client.get(SCOREBOARD, params={
                    "dates": f"{day.strftime('%Y%m%d')}-{chunk_end.strftime('%Y%m%d')}",
                    "groups": FBS_GROUP,
                    "limit": _LIMIT,
                })
                if r.status_code == 200:
                    out.extend(r.json().get("events", []))
            except httpx.HTTPError:
                log.exception("cfb scoreboard fetch failed for %s..%s", day, chunk_end)
            day = chunk_end + datetime.timedelta(days=1)
    return out


def fetch_upcoming_and_recent(back_days: int = 7, forward_days: int = FORWARD_DAYS) -> list[dict]:
    """The window the poller wants: far enough back to pick up results that just
    settled, far enough forward to cover everything Kalshi has listed."""
    today = datetime.date.today()
    return fetch_scoreboard_events(today - datetime.timedelta(days=back_days),
                                   today + datetime.timedelta(days=forward_days))


def season_for(game_date: datetime.date) -> int:
    """A January game belongs to the PREVIOUS season (bowls/playoff final).
    Must agree with market_matcher_cfb._season_for and CfbGame.season."""
    return game_date.year - 1 if game_date.month == 1 else game_date.year


def parse_event(ev: dict) -> dict | None:
    """ESPN event -> the CfbGame fields. Returns None when the event is missing
    anything required, rather than inventing a placeholder row."""
    comps = ev.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors") or []
    if len(competitors) != 2:
        return None

    home = away = None
    for c in competitors:
        abbr = (c.get("team") or {}).get("abbreviation")
        if not abbr:
            return None
        score = c.get("score")
        try:
            score = int(score) if score not in (None, "") else None
        except (TypeError, ValueError):
            score = None
        team = c.get("team") or {}
        side = {
            "abbr": abbr,
            "score": score,
            # Display names are the matcher's fallback when a Kalshi abbreviation
            # isn't in its alias table. They must come from HERE: the historical
            # cache's "home"/"away" fields are numeric ESPN team IDs, not names,
            # so a name index built from the cache silently maps "158" -> "NEB"
            # and the fallback never fires.
            # BOTH forms, because they differ in exactly the way that matters:
            # displayName is "North Carolina Tar Heels" (school + mascot) while
            # Kalshi's yes_sub_title is "NC State" / "Notre Dame" -- school only.
            # Indexing displayName alone leaves the fallback unable to match any
            # real Kalshi label. `location` is ESPN's school-only form.
            "name": team.get("displayName"),
            "short_name": team.get("location") or team.get("shortDisplayName") or team.get("name"),
        }
        if c.get("homeAway") == "home":
            home = side
        elif c.get("homeAway") == "away":
            away = side
    if not home or not away:
        return None

    iso = ev.get("date") or ""
    try:
        # ESPN returns e.g. "2026-09-19T16:00Z" -- a UTC instant.
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    game_date = dt.date()

    status = ((comp.get("status") or {}).get("type") or {})
    completed = bool(status.get("completed"))
    # Only trust scores once ESPN says the game is final; an in-progress game
    # reports a partial score that would corrupt the Elo if fed in as a result.
    if not completed:
        home["score"] = away["score"] = None

    season_type = ((ev.get("season") or {}).get("type"))
    return {
        "id": str(ev.get("id")),
        "season": season_for(game_date),
        # ESPN season type 3 = postseason (bowls/playoff); 2 = regular.
        "game_type": "POST" if season_type == 3 else "REG",
        "gameday": game_date.isoformat(),
        "gametime": dt.strftime("%H:%M"),
        "away_team": away["abbr"],
        "home_team": home["abbr"],
        # Not persisted on CfbGame -- used only to build the matcher's
        # display-name fallback index from live data.
        "away_name": away.get("name"),
        "home_name": home.get("name"),
        "away_short": away.get("short_name"),
        "home_short": home.get("short_name"),
        "away_score": away["score"],
        "home_score": home["score"],
        "neutral": 1 if comp.get("neutralSite") else 0,
        "venue": ((comp.get("venue") or {}).get("fullName")),
    }
