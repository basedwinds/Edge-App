"""One rule shared by the esports pollers: never orphan a match that has played.

REAL BUG this exists for (user-reported 2026-08-03, cost a real $20 bet).
Invictus Gaming vs LNG Esports was played on 2026-08-02 and bet on. The result
was never scraped, so the row kept winner=None -- which every poller treats as
"still upcoming". When the 2026-08-09 REMATCH appeared, its markets bound to that
same row and moved estimated_start_time from 2026-08-02T09:00Z to
2026-08-09T07:00Z. The played match was then orphaned: it could never settle, and
no "past its start" check could see it, because its own row now claimed a future
date. The bet sat pending until it was deleted by hand.

market_catalog_*.find_or_create_upcoming_match already restricts rematch matching
to fixtures near the incoming date, and that closes the common case. It cannot
close this one: its window helper documents that "unknown dates fall back to
True, preserving the old name-only behaviour", so whenever the platform gives no
occurrence_datetime -- routine for esports -- a rematch can still land on the
older row.

So this guards the WRITE instead of the match, which is where the damage actually
happens and needs no date on the incoming row at all. Moving a start time from
the past into the future is never a legitimate correction: a match that should
already have begun does not get rescheduled days later under the same row. A
genuine postponement of a not-yet-started match still passes, as does any
correction to a match whose start is still ahead of us.
"""
import datetime

# How far past its start a match may be and still accept a jump into the future.
# Covers a match that is merely running long or whose start we had slightly early,
# without letting a days-later rematch claim the row.
_GRACE = datetime.timedelta(hours=6)


def _parse(stamp: str | None) -> datetime.datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def should_update_start(current: str | None, incoming: str | None,
                        match_date: str | None = None,
                        now: datetime.datetime | None = None) -> bool:
    """May `incoming` replace `current` as a match's estimated start?

    False only for the orphaning move: the match has already been played and the
    new value is in the future. Everything else -- an unparseable value, a
    correction to a match still ahead, a genuine postponement of something not
    yet started -- passes, so this cannot freeze legitimate reschedules.

    `match_date` is the fallback for "has it been played" when the row carries no
    start time. That matters for REPAIR as much as for prevention: the honest way
    to fix an already-orphaned row is to clear the bogus future start rather than
    invent a time of day nobody recorded, and without this argument a cleared row
    would simply be re-corrupted on the next poll, since "no current value" reads
    as "nothing to protect".
    """
    if not incoming:
        return False
    now = now or datetime.datetime.utcnow()
    incoming_dt = _parse(incoming)
    if incoming_dt is None:
        return True
    current_dt = _parse(current)
    if current_dt is None and match_date:
        try:
            day = datetime.date.fromisoformat(str(match_date)[:10])
        except ValueError:
            day = None
        if day is not None:
            # End of the played day, so a same-day start time is still accepted.
            current_dt = datetime.datetime.combine(day, datetime.time(23, 59))
    if current_dt is None:
        return True
    already_started = current_dt < (now - _GRACE)
    moves_to_future = incoming_dt > now
    return not (already_started and moves_to_future)


def apply_start(match, incoming: str | None, now: datetime.datetime | None = None) -> bool:
    """Set `match.estimated_start_time` if the rule above allows it, AND keep
    `match.match_date` in agreement with it.

    THE SECOND HALF IS THE POINT. `find_or_create_upcoming_match` defaults
    match_date to `datetime.date.today()` whenever the platform poller that
    creates the row has no date to give -- which is the normal case, since
    Kalshi/Polymarket supply a start TIME separately, after the row exists. So a
    "live:" row records the day it was SCRAPED, and nothing ever corrects it once
    the real start arrives.

    User-reported (2026-08-05): Leviatan vs MIBR showed as an August 3rd match in
    Recommended Bets. The match is on the 8th; the 3rd is simply when Polymarket
    first listed it. Measured at the time: 22 Valorant rows carried a match_date
    2-6 days before their own start time, every one of them a "live:" row, and
    the gap was always in the same direction.

    That stale date does real damage beyond the label. `_within_rematch_window`
    compares an incoming date against match_date with a +/-2 day window, so a
    row stamped 5 days early can no longer be recognised as the same fixture --
    which is why the vlr.gg scrape created a SECOND row (id 230) for a match the
    Polymarket poller had already created (id 275), instead of reconciling onto
    it. One wrong field produced both the wrong date and the duplicate.

    Only ever moves match_date to the day of a start time this module has just
    accepted, so the orphaning guard above still decides what is trustworthy;
    this never invents a date of its own.

    KNOWN LIMIT, deliberately not worked around. This is the SECOND line of
    defence -- the pollers now pass the real start date into
    find_or_create_upcoming_match, so the fallback stamp is not written in the
    first place. It has to be, because a stale match_date is self-sealing: the
    guard above treats an old match_date as evidence the match was PLAYED, so
    once the fake date is a day or more in the past it starts REFUSING the very
    start time that would correct it. Verified against the reported row.

    So a row can still get stuck if it is created at a moment when its platform
    supplies no start time at all (match_date falls back to today) and the start
    only appears days later. That row cannot be mispriced -- it has no start time
    to be wrong about -- and the date it shows is the day it was seen. Detecting
    it would mean telling a fallback stamp apart from a real date, and since the
    pollers now pass real dates those two cases look identical.
    """
    if not should_update_start(match.estimated_start_time, incoming, match.match_date, now):
        return False
    match.estimated_start_time = incoming
    day = str(incoming)[:10] if incoming else None
    if day and len(day) == 10 and match.match_date != day:
        try:
            datetime.date.fromisoformat(day)
        except ValueError:
            return True
        match.match_date = day
    return True
