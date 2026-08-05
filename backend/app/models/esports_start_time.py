"""Guards an esports match's start time against its own match_date.

REAL BUG (user-reported 2026-08-05): "Team Phoenix vs Dark Passage" was being
recommended, with two staked series_winner bets at +20pp and +23pp, for a match
played on AUGUST 2nd. The row looked upcoming because it carried a start time
that belongs to a DIFFERENT fixture:

    id=333  Dark Passage vs Team Phoenix  match_date 2026-08-06  start 08-06T14:30Z
    id=294  Team Phoenix vs Dark Passage  match_date 2026-08-02  start 08-06T14:30Z

The same two teams play twice, the rows store them in opposite order so they
never dedupe, and the later fixture's start time was written onto the earlier
row. Everything downstream trusts the timestamp over the date (correctly, in
general -- match_date is often stale for esports), so the played match read as
tomorrow's.

Scale when this was written: LoL 74 of 401 dated rows disagree by >=2 days, with
30 fixtures appearing more than once while SHARING a start time. Valorant 22 of
306, CS2 2 of 493.

THE RULE: when the two disagree by two days or more, neither can be trusted, so
take the EARLIER of them. That is deliberately conservative in one direction --
if a match might already be over, it must not be offered as a bet. Losing a
genuinely-upcoming match costs one missed bet; recommending a finished one
stakes money on a known result.

Two days rather than one, because a real fixture legitimately crosses a UTC date
boundary (a 21:00 ET start is the next UTC day) and that must not be treated as
corruption.
"""
import datetime

# Below this, a mismatch is just a timezone/date-boundary artefact.
MAX_TRUSTED_DISAGREEMENT_DAYS = 2


def _parse_date(value) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def trusted_start_time(estimated_start_time, match_date):
    """The start instant to publish, or None when there is nothing usable.

    Returns the ORIGINAL estimated_start_time when it agrees with match_date (or
    when either is missing, which is the normal case for a fixture with no
    scheduled time). When they disagree by MAX_TRUSTED_DISAGREEMENT_DAYS or more,
    returns whichever is EARLIER, rendered as an instant so callers that compare
    against "now" keep working unchanged.
    """
    start_date = _parse_date(estimated_start_time)
    fixture_date = _parse_date(match_date)
    if start_date is None or fixture_date is None:
        return estimated_start_time
    if abs((start_date - fixture_date).days) < MAX_TRUSTED_DISAGREEMENT_DAYS:
        return estimated_start_time
    if fixture_date < start_date:
        # The fixture's own date is earlier -- publish that, at midnight UTC, so
        # a match that has already happened compares as past.
        return f"{fixture_date.isoformat()}T00:00:00Z"
    return estimated_start_time
