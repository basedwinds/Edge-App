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


def fetch_race_dates() -> dict[str, list[tuple[set[str], datetime.datetime]]]:
    """{series: [(name_tokens, race_date)]} from ESPN's full-season calendars.
    endDate is the race day (F1's Sunday); single-day NASCAR/IRL have start==end."""
    out: dict[str, list[tuple[set[str], datetime.datetime]]] = {}
    for series, url in _ESPN.items():
        try:
            data = get_json(url)
        except Exception:
            log.exception("espn racing calendar fetch failed for %s", series)
            out[series] = []
            continue
        lg = (data.get("leagues") or [{}])[0]
        races: list[tuple[set[str], datetime.datetime]] = []
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
