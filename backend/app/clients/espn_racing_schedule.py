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

_RACE_ORDINAL = re.compile("race[ ]*([0-9]+)", re.I)


def _race_ordinal(name: str) -> "int | None":
    """The 'Race 2' in a doubleheader name, or None.

    A DOUBLEHEADER IS TWO RACES UNDER ONE NAME, the same problem shape a sprint
    weekend has. IndyCar runs both on the same weekend at one venue, and
    `_tokens` strips digits, so "Snap-on Milwaukee Mile 250 Race 1" and
    "... Race 2" both reduce to {"milwaukee"} and are indistinguishable to the
    token matcher. Measured 2026-08-21: RaceEvents 89/90 ("Race 2") were stored
    as 2026-08-29 15:00 when ESPN has Race 1 at 08-29 18:30 and Race 2 at
    08-30 17:00 -- a full day early, so every start-time gate was wrong for them.

    The ordinal is deliberately read from the ORIGINAL name rather than from
    tokens, because tokenisation is exactly what destroys it."""
    m = _RACE_ORDINAL.search(name or "")
    return int(m.group(1)) if m else None
# The SPRINT race's own session. A sprint weekend holds two races -- the sprint
# on Saturday and the grand prix on Sunday -- inside ONE ESPN event whose name
# is the grand prix's ("Heineken Dutch Grand Prix"). Token matching therefore
# resolves a sprint to the GRAND PRIX date by construction, which is why every
# sprint market read Sunday while the sprint ran Saturday.
SPRINT_SESSION = "SR"

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
        return [], []
    out: list[tuple[set[str], datetime.datetime]] = []
    sprints: list[tuple[set[str], datetime.datetime]] = []
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
        # The sprint is a SEPARATE RACE sharing this event and this name, and
        # ESPN dates it properly on its own competition -- measured on the 2026
        # Dutch GP: SR 08-22T10:00Z against Race 08-23T13:00Z, a full day apart.
        # Collected into its own bucket rather than appended above, because a
        # sprint must never be returned to a caller asking for the grand prix
        # (it would win the token match just as often -- the tokens are
        # IDENTICAL -- and drag the GP onto Saturday).
        sprint = next((c for c in comps
                       if ((c.get("type") or {}).get("abbreviation") or "") == SPRINT_SESSION), None)
        sdt = _parse(sprint.get("date")) if sprint else None
        if sdt and toks:
            sprints.append((toks, sdt))
    return out, sprints


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
    return {series: v["main"] for series, v in fetch_race_dates_by_session().items()}


def fetch_race_dates_by_session() -> dict[str, dict[str, list[tuple[set[str], datetime.datetime]]]]:
    """{series: {"main": [(tokens, start)], "sprint": [(tokens, start)]}}.

    Same data as fetch_race_dates, split by which RACE the date belongs to. A
    sprint weekend is two races under one event name, so the name tokens are
    identical and only the session tells them apart.

    The season-calendar fallback feeds "main" ONLY. Its entries are weekend
    markers (and run ~3h late, see _fetch_event_starts), so they describe the
    grand prix; letting one answer a sprint lookup would hand the sprint the
    grand prix's date, which is the entire bug this split exists to fix. A
    sprint with no scoreboard entry therefore resolves to None and the caller
    keeps its existing fallback -- no worse than before, and never silently
    wrong.
    """
    out: dict[str, dict[str, list[tuple[set[str], datetime.datetime]]]] = {}
    for series, url in _ESPN.items():
        races, sprints = _fetch_event_starts(series, url)
        try:
            data = get_json(url)
        except Exception:
            log.exception("espn racing calendar fetch failed for %s", series)
            out[series] = {"main": races, "sprint": sprints, "ordinals": [], "scoreboard": list(races)}
            continue
        lg = (data.get("leagues") or [{}])[0]
        # ORDINAL-BEARING CALENDAR ENTRIES, kept apart from `races`.
        #
        # The scoreboard has the RIGHT TIMES but its event names carry no
        # ordinal at all (both Milwaukee entries tokenise to just {"milwaukee"}).
        # The calendar has the ordinal in its label ("Grand Prix of Milwaukee
        # Race 2") but its times run ~3h late. Neither source alone can date a
        # doubleheader leg, so pair them: the calendar answers WHICH DAY, then
        # the scoreboard entry on that day supplies the real start time.
        # Scoreboard-only snapshot, taken BEFORE the calendar entries are
        # appended to `races` below. The pairing needs to know which entries
        # carry REAL start times (scoreboard) versus weekend markers running ~3h
        # late (calendar).
        scoreboard = list(races)
        ordinals: list[tuple[set[str], int, datetime.datetime]] = []
        for c in lg.get("calendar") or []:
            if not isinstance(c, dict):
                continue
            label = c.get("label") or ""
            dt = _parse(c.get("endDate") or c.get("startDate"))
            toks = _tokens(label)
            if dt and toks:
                races.append((toks, dt))
                n = _race_ordinal(label)
                if n is not None:
                    ordinals.append((toks, n, dt))
        out[series] = {"main": races, "sprint": sprints, "ordinals": ordinals, "scoreboard": scoreboard}
    return out


def resolve_race_date_for_session(series: str, name: str,
                                  by_session: dict, is_sprint: bool) -> datetime.datetime | None:
    """resolve_race_date, but asking for the SPRINT's date when the event is a
    sprint. Returns None rather than falling back to the grand prix -- see
    fetch_race_dates_by_session."""
    entry = by_session.get(series) or {}
    bucket = entry.get("sprint" if is_sprint else "main", [])
    want = _tokens(name)
    if not want:
        return None

    # DOUBLEHEADER LEG: pair the two sources before falling through to plain
    # token matching. Neither can date a leg alone -- the SCOREBOARD has the real
    # start times but its names carry no ordinal (both Milwaukee entries tokenise
    # to just {"milwaukee"}), while the CALENDAR labels carry "Race 1"/"Race 2"
    # but their times run ~3h late. So: use the calendar to learn WHICH DAY this
    # leg runs, then take the scoreboard entry on that day for the real time.
    #
    # Falls through silently when there is no ordinal, no matching calendar
    # entry, or no scoreboard entry on that day -- a single-race weekend is
    # untouched, and a doubleheader we cannot pair is no worse than before.
    ordinal = _race_ordinal(name)
    if ordinal is not None and not is_sprint:
        day = None
        best_n = 0
        for toks, n, dt in entry.get("ordinals", []):
            if n != ordinal:
                continue
            overlap = len(want & toks)
            if overlap > best_n:
                day, best_n = dt.date(), overlap
        if day is not None:
            best, best_n2 = None, 0
            for toks, dt in entry.get("scoreboard", []):
                if dt.date() != day:
                    continue
                overlap = len(want & toks)
                if overlap > best_n2:
                    best, best_n2 = dt, overlap
            if best is not None:
                return best
    best: datetime.datetime | None = None
    best_n = 0
    for toks, dt in bucket:
        n = len(want & toks)
        if n > best_n:
            best, best_n = dt, n
    return best if best_n >= 1 else None


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
