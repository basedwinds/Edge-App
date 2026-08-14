"""NASCAR's own public feed (cf.nascar.com) -- race calendar and starting grids.

WHY THIS EXISTS ALONGSIDE espn_racing_results. Two things ESPN cannot do:

1. GRIDS. ESPN publishes no starting field for many lower-series races. Measured
   2026-08-14 on the Truck race at Richmond, 90 minutes before the green flag:
   ESPN's scoreboard returned 0 competitors, its core event had a single
   competition with no type and no competitors, and fetch_race_grid returned
   None -- while NASCAR had the full 39-car qualifying result. Over the four
   preceding Truck races ESPN was missing the grid entirely on one and short by
   two on another. Since the grid is the racing model's strongest input, "ESPN
   has no grid" means the race is priced flat and never staked.

2. NAMES. ESPN labels races by VENUE ("NASCAR Cup Series at Richmond") while
   Kalshi and Polymarket name them by SPONSOR ("Black's Tire 250 presented by
   BTS Rewards"). NASCAR's own calendar uses the SPONSOR name, so it matches the
   market feeds directly. That is what fixes date resolution -- see match_race.

VALIDATED BEFORE USE, because a wrong grid is far worse than no grid. Across the
three 2026 Truck races where BOTH sources published a field, every starting
position agreed: 35/35, 33/33, 36/36 -- 104 of 104. Driver names resolve onto
the app's own ids via racing_ratings.resolve_driver_id, and on the Richmond race
the feed's 39 drivers and the market's 39 entrants were the same set exactly,
with no name on either side unaccounted for.

SERIES IDS, confirmed live 2026-08-14 by reading each calendar:
    1 = Cup (40 races)   2 = Xfinity (33)   3 = Truck (25)

All times in this feed are LOCAL TRACK TIME, US Eastern in practice, and carry no
offset ("2026-08-14T19:30:00"). They are converted to UTC here so callers never
have to think about it -- 19:30 ET is the 23:30Z that ESPN independently reports
for the same race.
"""
import datetime
import json
import logging
import re
import urllib.request

log = logging.getLogger(__name__)

_BASE = "https://cf.nascar.com/cacher"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; edge-app/1.0)"}

# Keyed by the SAME series strings the rest of the app uses -- espn_racing_results
# .NASCAR_RESULT_SERIES is ("nascar", "nascar_xfinity", "nascar_truck") -- so a
# caller can hand either client the same key and never have to translate.
SERIES_IDS = {"nascar": 1, "nascar_xfinity": 2, "nascar_truck": 3}

# Words that carry no identifying information in a market event title. Without
# this, "NASCAR"/"Series"/"winner" would match every race in every series.
_STOP = {
    "nascar", "series", "the", "at", "presented", "by", "powered", "race",
    "winner", "wins", "will", "be", "finish", "in", "top", "awarded", "pole",
    "position", "for", "2026", "cup", "xfinity", "truck", "o'reilly", "auto",
    "parts", "and", "a", "of",
}

# How far a date is allowed to move on the strength of a name match. The real
# defects measured 2026-08-14 across all 34 stored NASCAR events were 1 day
# (Truck at Richmond took the Cup race's slot) and 15 days (TSport 200, Pennzoil
# 250). Same-name collisions within a season sit ~139 days apart. 45 admits every
# observed defect and excludes every observed collision, with room either side.
_MAX_TIEBREAK_DAYS = 45

_calendar_cache: "dict[int, list[tuple]] | None" = None


def _get(url: str):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _tokens(s) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    return {t for t in s.split() if t and t not in _STOP and len(t) > 1}


def _to_utc(local_iso: str) -> "datetime.datetime | None":
    """Feed times are naive US/Eastern. Convert to naive UTC, which is what
    RaceEvent.start_time stores everywhere else in this app."""
    if not local_iso:
        return None
    try:
        naive = datetime.datetime.strptime(str(local_iso)[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    try:
        from zoneinfo import ZoneInfo
        aware = naive.replace(tzinfo=ZoneInfo("America/New_York"))
        return aware.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        # Without tz data, refuse rather than guess an offset: a 4-vs-5 hour
        # error is exactly the kind of near-miss that survives review.
        return None


def race_calendars(season: int = 2026) -> "dict[int, list[tuple]]":
    """{series_id: [(race_id, race_name, start_utc, name_tokens), ...]}"""
    global _calendar_cache
    if _calendar_cache is not None:
        return _calendar_cache
    out: dict[int, list[tuple]] = {}
    for sid in SERIES_IDS.values():
        try:
            d = _get(f"{_BASE}/{season}/{sid}/race_list_basic.json")
        except Exception:
            log.warning("nascar feed: calendar fetch failed for series %s", sid)
            continue
        rows = d if isinstance(d, list) else d.get(f"series_{sid}") or []
        races = []
        for r in rows:
            name = r.get("race_name")
            start = _to_utc(r.get("race_date"))
            if not name or start is None:
                continue
            races.append((r.get("race_id"), name, start, _tokens(name)))
        out[sid] = races
    _calendar_cache = out
    return out


def match_race(event_name: str, near: "datetime.datetime | None" = None, season: int = 2026):
    """(series_key, race_id, race_name, start_utc) for a market event title, or None.

    `near` is the date the caller ALREADY has (RaceEvent.start_time). It is only
    a tiebreak, never the answer -- but it is a required one, because a sponsor
    name is NOT unique within a season. Confirmed live 2026-08-14: the 2026 Cup
    calendar carries "Cook Out 400" TWICE, at Martinsville on 03-29 and at
    Richmond on 08-15, and the market titles ("NASCAR: Cook Out 400 Winner")
    name no track at all. Without a tiebreak this returned the March race for an
    August event -- a five-month error, far worse than the one-day bug it was
    written to fix.

    Using the stored date is sound precisely because the bug being corrected is
    SMALL: the wrong dates measured across all 34 stored events were off by 1 day
    (Truck at Richmond) to 15 days (TSport 200, Pennzoil 250), while same-name
    collisions sit months apart. So the nearest candidate is the right one, and
    _MAX_TIEBREAK_DAYS refuses anything that would be a big jump.

    THE MATCHING RULE, and why it is this strict. Every significant token of the
    NASCAR race name must appear in the event title. Requiring the RACE's tokens
    (not the event's) means a long market title full of driver names and prop
    wording cannot dilute the match, while a race whose sponsor name is only
    partly present is refused rather than guessed.

    Contrast the rule this replaces: espn_racing_schedule.resolve_race_date
    accepted a match on ONE shared token and searched only the Cup calendar. Both
    the Truck race and the Cup race at Richmond share the token "richmond", so
    the Truck event took the CUP race's date -- 2026-08-15 23:00 against a real
    2026-08-14 23:30. That single wrong date then steered the grid lookup at the
    Cup calendar too, because the grid matcher picks the nearest date across
    calendars and the Cup race was an exact-day hit.

    CROSS-SERIES TIES ARE REFUSED, NOT RESOLVED. If the best match is equally
    good in two different series the answer is None, so the caller keeps whatever
    it already had. Guessing here is the precise failure this module exists to
    stop. Measured over all 34 stored NASCAR events: 31 matched uniquely, 0 were
    ambiguous, and the 3 non-matches are two season-championship futures (not
    races at all) and the Brickyard 400, whose feed name carries a sponsor the
    market title omits -- all three keep their existing behaviour.
    """
    want = _tokens(event_name)
    if not want:
        return None
    by_id = {v: k for k, v in SERIES_IDS.items()}
    hits = []
    for sid, races in race_calendars(season).items():
        for rid, rname, start, rtok in races:
            if rtok and rtok <= want:
                gap = abs((start - near).days) if near else 0
                hits.append((-len(rtok), gap, sid, rid, rname, start))
    if not hits:
        return None
    # Most specific name first, then nearest to the date we already hold.
    hits.sort()
    best = hits[0]
    if len(hits) > 1:
        second = hits[1]
        same_name_rank = best[0] == second[0]
        if same_name_rank and best[2] != second[2]:
            log.info("nascar feed: %r matches %s and %s equally -- refusing to guess",
                     event_name, by_id.get(best[2]), by_id.get(second[2]))
            return None
        # Same name, same series, two dates (e.g. "Cook Out 400" runs at
        # Martinsville in March and Richmond in August). Only accept when the
        # stored date picks one CLEARLY -- an equal gap is a coin flip.
        if same_name_rank and best[1] == second[1]:
            log.info("nascar feed: %r matches %d races equally far from %s -- refusing",
                     event_name, 2, near)
            return None
    _rank, gap, sid, rid, rname, start = best
    if near is not None and gap > _MAX_TIEBREAK_DAYS:
        # The name matched but the date moves by more than any observed defect.
        # Refusing keeps the caller's existing value rather than betting that a
        # months-long jump is a correction and not a mismatch.
        log.info("nascar feed: %r -> %s is %d days from stored %s -- refusing",
                 event_name, rname, gap, near)
        return None
    return by_id.get(sid), rid, rname, start


def fetch_grid(series_key: str, race_id, season: int = 2026) -> "dict[str, int] | None":
    """{driver_name: starting position} from qualifying, or None if not yet run.

    run_type 2 is qualifying (run_type 1 is practice). Returns None rather than
    an empty dict when qualifying has not happened, so callers can treat "no
    grid" exactly as they already do for ESPN.
    """
    sid = SERIES_IDS.get(series_key)
    if sid is None or race_id is None:
        return None
    try:
        d = _get(f"{_BASE}/{season}/{sid}/{race_id}/weekend-feed.json")
    except Exception:
        return None
    runs = [r for r in (d.get("weekend_runs") or []) if r.get("run_type") == 2]
    if not runs:
        return None
    grid = {}
    for row in runs[0].get("results") or []:
        name, pos = row.get("driver_name"), row.get("finishing_position")
        if name and isinstance(pos, int):
            grid[name] = pos
    return grid or None
