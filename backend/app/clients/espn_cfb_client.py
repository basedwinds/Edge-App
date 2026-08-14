"""ESPN college-football schedule client (free public API, same host the
NBA/WNBA/MLB clients use). Parallel to espn_wnba_client.py.

Two CFB-specific things, both confirmed live 2026-08-02:

`groups=80` restricts to FBS. Without it the scoreboard also returns FCS,
Division II and Division III games, which have no Elo ratings here (the game
cache the constants were derived from is FBS-only) and would flood the table
with teams we can never price.

Fetches ONE DAY AT A TIME, like the WNBA client. A date-RANGE query
(YYYYMMDD-YYYYMMDD) looks like an obvious optimisation and is a trap -- ESPN
silently returns an arbitrary SUBSET rather than everything in the range.
Measured 2026-08-02:

    dates=20260919            -> 25 events, all 25 on 9/19
    dates=20260913-20260919   ->  4 events, only  2 on 9/19
    dates=20260906-20260919   -> 94 events, only  2 on 9/19

The range form is not merely capped (a cap would truncate consistently); it
drops most of each day. Building on it left three already-listed Kalshi games --
Michigan St @ Notre Dame, LSU @ Ole Miss, Clemson @ LSU -- with no schedule row
to link to, which is exactly the silent unlinked-market failure this client
exists to avoid.

Because a schedule changes slowly, the poller should refresh it on a long
interval rather than every market cycle; per-day fetching is only expensive if
run every few minutes.
"""
import datetime
import logging

import httpx

log = logging.getLogger("espn_cfb_client")

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
_UA = {"User-Agent": "Mozilla/5.0"}

# FBS only -- see module docstring.
FBS_GROUP = "80"
# DO NOT RAISE THIS. ESPN silently falls back to a tiny default page when `limit`
# is too large -- the failure is inverted from what you'd expect. Measured
# 2026-08-02 on dates=20260919&groups=80:
#
#     limit omitted -> 71 events
#     limit=50      -> 71 events
#     limit=100     -> 71 events
#     limit=1000    -> 25 events   <-- silently degraded
#
# The original value here was 900, which returned 25 of 71 games per day. That is
# what left three already-listed Kalshi fixtures (Michigan St @ Notre Dame, LSU @
# Ole Miss, Clemson @ LSU) with no schedule row -- it looked like "ESPN doesn't
# have those games" rather than "we asked wrongly", which is exactly why a too-
# large limit is more dangerous than no limit at all.
_LIMIT = 100
# How far ahead to pull. This MUST exceed Kalshi's listing horizon or markets
# arrive with no game row to link to -- the exact bug that left WNBA markets
# unlinked when its client stopped at today.
# Measured 2026-08-02: Kalshi had already listed KXNCAAFGAME markets closing
# 2026-09-21, i.e. 50 days out, while a 45-day window only reached Sept 13. 90
# gives real headroom over that observed horizon; the fetch is a handful of
# range calls, so a wide window is cheap.
#
# RAISED 90 -> 125 on 2026-08-14. The 90 was sized for GAME markets -- it only
# ever had to outrun Kalshi's listing horizon. The SEASON-LONG markets silently
# inherited it, and they need something completely different: the whole season,
# because a win-total ladder resolves on all 12 regular-season games.
#
# What that cost, measured on the live board: the schedule ran 2026-08-29 to
# 2026-11-13, and today+90 is 2026-11-12 -- it stopped exactly at the window
# edge. EVERY one of 227 FBS-connected teams had an incomplete schedule, median
# 9 games, and all 16 teams carrying a staked win-total bet had exactly 9. The
# season sim was therefore simulating a 9-game season and pricing markets that
# settle over 12, which makes every win_total, conference and playoff
# probability wrong -- CONN alone had five rungs on the board, all skewed the
# same way, which is the signature of a per-team schedule error rather than
# noise.
#
# 125 days reaches early December from mid-August, which covers conference
# championship weekend (2026-12-05). Verified live: a range query confirmed real
# ESPN events out to 2026-12-06.
#
# COST is ~30 extra per-day calls per refresh (Sun/Mon are skipped). That is
# real but bounded, and it is why this stays a per-day fetch: a single date-RANGE
# call is capped by _LIMIT and cannot page, so it returns FEWER events, not more
# (measured Sep 1-14 with groups=80&limit=100: per-day 173, range 100). The
# range query looks tempting and is a trap.
FORWARD_DAYS = 125
# Sunday(6) and Monday(0): FBS plays Tue-Sat.
_SKIP_WEEKDAYS = {6, 0}


def fetch_scoreboard_events(start: datetime.date, end: datetime.date) -> list[dict]:
    """Raw ESPN event dicts across [start, end], ONE CALL PER DAY.

    Sunday and Monday are skipped: FBS plays Tuesday through Saturday, so those
    two days are ~29% of calls for essentially no games. De-duped by event id
    because a late kickoff can appear on two adjacent days' scoreboards (the same
    UTC-boundary effect the WNBA client documents)."""
    out: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=30.0, headers=_UA) as client:
        day = start
        while day <= end:
            if day.weekday() in _SKIP_WEEKDAYS:
                day += datetime.timedelta(days=1)
                continue
            try:
                r = client.get(SCOREBOARD, params={
                    "dates": day.strftime("%Y%m%d"),
                    "groups": FBS_GROUP,
                    "limit": _LIMIT,
                })
                if r.status_code == 200:
                    for e in r.json().get("events", []):
                        eid = str(e.get("id"))
                        if eid and eid not in seen:
                            seen.add(eid)
                            out.append(e)
            except httpx.HTTPError:
                log.exception("cfb scoreboard fetch failed for %s", day)
            day += datetime.timedelta(days=1)
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
