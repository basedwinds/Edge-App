"""Resolves the REAL race date for a RaceEvent from ESPN's season calendar.

Kalshi's market `close_time` is an unreliable settlement deadline, not the race
start -- it sat WEEKS after the actual race (the Brickyard 400 close_time was
Aug 24 for a Jul 26 race), which is exactly the kind of wrong date that misled a
coverage check. We match a Kalshi racing event to an ESPN calendar race by
normalized-name token overlap and use ESPN's real date; the caller falls back to
close_time only when there's no confident match (mostly NASCAR lower-series
races ESPN's Cup calendar doesn't carry).
"""
import datetime
import logging
import re

from app.clients.base import get_json

# The race session's ESPN abbreviation. Defined in espn_racing_results and
# re-stated here rather than imported, to keep the schedule client free of a
# dependency on the results client.
RACE_SESSION = "Race"

log = logging.getLogger("espn_racing_schedule")

_ESPN = {
    "f1": "https://site.api.espn.com/apis/site/v2/sports/racing/f1/scoreboard",
    "nascar": "https://site.api.espn.com/apis/site/v2/sports/racing/nascar-premier/scoreboard",
    "irl": "https://site.api.espn.com/apis/site/v2/sports/racing/irl/scoreboard",
    # Lower NASCAR series, added 2026-08-07 so their calendars are available to
    # fetch_race_dates (racing_championship also consumes this map).
    #
    # THESE DO NOT FIX LOWER-SERIES DATES, and I expected them to. The gap is
    # real -- Kalshi had the HyVee Perks 250 at 2026-08-23 for a race ESPN dates
    # 2026-08-08 -- but the cause is NAME SHAPE, not a missing calendar, and
    # adding the calendars does not address it. Measured directly:
    #
    #   "HyVee Perks 250"        tokens {hyvee, perks}            -> no match, any calendar
    #   "Pennzoil 250 presented" tokens {pennzoil, take, oil, ..} -> no match, any calendar
    #   "TSport 200 presented"   tokens {tsport, warn, ..}        -> no match, any calendar
    #   "Iowa Corn 350"          tokens {iowa, corn, ethanol}     -> Cup 08-09 (right)
    #
    # ESPN labels races by VENUE ("... at Iowa"); Kalshi names them by SPONSOR.
    # Sponsor-named races share no significant token with any venue label, so
    # they resolve to nothing whichever calendar is searched.
    #
    # resolve_race_date is therefore deliberately NOT extended to search across
    # the three NASCAR calendars: doing so would make Cup dates WORSE, not
    # better. "Iowa Corn 350" matches the Xfinity calendar as well as the Cup
    # one -- and at 08-09, the wrong day for the Xfinity race -- so a
    # cross-series search would let a lower-series entry outrank the correct
    # Cup one on a tie. Fixing lower-series dates needs a venue alias table or a
    # join on the ESPN event id resolved in espn_racing_results, not this.
    "nascar_xfinity": "https://site.api.espn.com/apis/site/v2/sports/racing/nascar-secondary/scoreboard",
    "nascar_truck": "https://site.api.espn.com/apis/site/v2/sports/racing/nascar-truck/scoreboard",
}
# Sponsor / filler words stripped before token-matching a race name, so
# "AWS Hungarian Grand Prix" and "Hungarian Grand Prix Winner" both key on
# {hungarian}, and NASCAR's sponsor-laden Kalshi titles reduce to the venue.
_STOP = {
    "grand", "prix", "the", "at", "nascar", "cup", "series", "presented", "by",
    "gp", "aws", "airways", "qatar", "heineken", "aramco", "crypto", "com", "gulf",
    "air", "stc", "of", "race", "winner", "pole", "ppg", "powered", "and",
}
# Kalshi names that share NO token with ESPN's label -> explicit aliases.
_ALIAS = {"brickyard": "indianapolis"}


def _tokens(name: str) -> set[str]:
    out: set[str] = set()
    for w in re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split():
        w = _ALIAS.get(w, w)
        if w and w not in _STOP and not w.isdigit():
            out.add(w)
    return out


def _parse(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


# How far either side of today to ask the scoreboard for real event times. Back
# far enough to still resolve a race that just ran, forward far enough to cover
# everything with live markets.
_EVENT_WINDOW_BACK = datetime.timedelta(days=30)
_EVENT_WINDOW_FWD = datetime.timedelta(days=120)


def _fetch_event_starts(series: str, url: str) -> list[tuple[set[str], datetime.datetime]]:
    """Real green-flag times from ESPN's scoreboard EVENTS, over a date range.

    THE CALENDAR IS NOT THE START TIME, which is what this exists to fix. A
    calendar entry is a race-weekend window marker and runs consistently LATE:

        Grand Prix of Portland   calendar 2026-08-09T23:00Z   event 2026-08-09T20:00Z
        NASCAR Cup at Iowa       calendar 2026-08-09T22:30Z   event 2026-08-09T19:30Z
        Grand Prix of Ontario    calendar 2026-08-16T19:00Z   event 2026-08-16T16:00Z

    A flat +3h on every race checked. Because the app treats start_time as the
    live-race cutoff, that made races look UPCOMING for three hours after the
    green flag and kept staking them -- the user reported exactly this.
    """
    today = datetime.datetime.utcnow().date()
    rng = f"{(today - _EVENT_WINDOW_BACK):%Y%m%d}-{(today + _EVENT_WINDOW_FWD):%Y%m%d}"
    try:
        data = get_json(f"{url}?dates={rng}")
    except Exception:
        log.exception("espn racing scoreboard fetch failed for %s", series)
        return []
    out: list[tuple[set[str], datetime.datetime]] = []
    for e in data.get("events") or []:
        if not isinstance(e, dict):
            continue
        comps = [c for c in (e.get("competitions") or []) if isinstance(c, dict)]
        # An F1 weekend is ONE event holding five competitions (FP1, FP2, FP3,
        # Qual, Race), so comps[0] is Friday practice -- taking it dated the
        # Italian GP to Sep 4 10:30 instead of Sep 6 13:00. Pick the race
        # session by name; NASCAR/IndyCar carry a single untyped competition
        # and fall through to it unchanged.
        race = next((c for c in comps
                     if ((c.get("type") or {}).get("abbreviation") or "") == RACE_SESSION), None)
        dt = _parse((race or (comps[0] if comps else {})).get("date") or e.get("date"))
        toks = _tokens(e.get("name") or e.get("shortName"))
        if dt and toks:
            out.append((toks, dt))
    return out


def fetch_race_dates() -> dict[str, list[tuple[set[str], datetime.datetime]]]:
    """{series: [(name_tokens, race_start)]}.

    Real scoreboard EVENT times first -- those are the green flag. The season
    calendar is only a fallback, for races outside the scoreboard window (a
    championship market months out, say), because its times are a race-weekend
    marker rather than a start and run ~3h late; see _fetch_event_starts.

    Event entries are listed FIRST so resolve_race_date's strict-improvement
    scan (`n > best_n`) keeps the real start when both sources match a name
    equally well.
    """
    out: dict[str, list[tuple[set[str], datetime.datetime]]] = {}
    for series, url in _ESPN.items():
        races = _fetch_event_starts(series, url)
        try:
            data = get_json(url)
        except Exception:
            log.exception("espn racing calendar fetch failed for %s", series)
            out[series] = races
            continue
        lg = (data.get("leagues") or [{}])[0]
        for c in lg.get("calendar") or []:
            if not isinstance(c, dict):
                continue
            dt = _parse(c.get("endDate") or c.get("startDate"))
            toks = _tokens(c.get("label"))
            if dt and toks:
                races.append((toks, dt))
        out[series] = races
    return out


def resolve_race_date(series: str, name: str,
                      dates: dict[str, list[tuple[set[str], datetime.datetime]]]) -> datetime.datetime | None:
    """Best token-overlap match for a Kalshi event name; None if no shared
    significant token (caller then keeps Kalshi's close_time)."""
    want = _tokens(name)
    if not want:
        return None
    best: datetime.datetime | None = None
    best_n = 0
    for toks, dt in dates.get(series, []):
        n = len(want & toks)
        if n > best_n:
            best, best_n = dt, n
    return best if best_n >= 1 else None
