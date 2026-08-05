"""Detects an esports match row whose start time was BORROWED from a different
fixture between the same two teams.

REAL BUG (user-reported 2026-08-05): "Team Phoenix vs Dark Passage" was
recommended, carrying two staked series_winner bets at +20pp and +23pp, for a
match played on AUGUST 2nd:

    id=333  Dark Passage vs Team Phoenix  match_date 2026-08-06  start 08-06T14:30Z
    id=294  Team Phoenix vs Dark Passage  match_date 2026-08-02  start 08-06T14:30Z

The teams play twice, the rows store them in opposite order so they never
dedupe, and the later fixture's start time was written onto the earlier row.
Everything downstream prefers the timestamp to the date, so a played match read
as tomorrow's.

WHY THE OBVIOUS TEST IS WRONG, measured. A first version simply distrusted any
row whose start time and match_date disagreed by >=2 days. That rewrote 71 LoL
rows when only 22 were corrupt: the other 49 were matches happening THAT DAY
whose match_date was merely stale (date 08-02, start 08-05 16:00, played 08-05).
It would have pushed 49 live fixtures into the past and off the board.

Tennis proves the same rule would be catastrophic there -- 596 of 3,139 rows
disagree by >=2 days, 576 of them with the start LATER, and in tennis the start
time is the FRESH field while match_date is a stale draw date. The direction of
the disagreement carries no information: LoL is 71 start-later vs 3, tennis 576
vs 20. Identical shape, opposite meaning.

THE ACTUAL SIGNAL IS THE COLLISION, not the gap. A start time that ANOTHER row
for the SAME team pair also claims, where that other row has a LATER match_date,
did not originate here. That is specific, evidence-based, and leaves a
stale-but-uncorrupted match_date alone.
"""
import datetime


def _parse_date(value) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _minute(value) -> str | None:
    return str(value)[:16] if value else None


def borrowed_start_times(matches) -> dict:
    """{match id: the date its own row claims} for rows whose start time belongs
    to a LATER fixture between the same two teams.

    `matches` needs .id, .team_a, .team_b, .match_date and
    .estimated_start_time. Returns only the rows to correct, so a caller can
    treat an absent id as "trust this row as-is".
    """
    by_pair: dict[frozenset, list] = {}
    for m in matches:
        if not m.estimated_start_time or not m.match_date:
            continue
        by_pair.setdefault(frozenset((m.team_a, m.team_b)), []).append(m)

    out: dict = {}
    for rows in by_pair.values():
        if len(rows) < 2:
            continue
        for row in rows:
            row_date = _parse_date(row.match_date)
            if row_date is None:
                continue
            # Another row for this same pairing claiming the same instant, whose
            # own fixture is later, is where this timestamp really belongs.
            for other in rows:
                if other is row or _minute(other.estimated_start_time) != _minute(row.estimated_start_time):
                    continue
                other_date = _parse_date(other.match_date)
                if other_date is not None and other_date > row_date:
                    out[row.id] = row.match_date
                    break
    return out


def corrected_start_time(match, borrowed: dict):
    """The start instant to publish for one match row.

    Unchanged unless this row is in `borrowed`, in which case its own fixture
    date is published at midnight UTC so a match that has already happened
    compares as past rather than as the rematch's kickoff.
    """
    if match is None:
        return None
    own_date = borrowed.get(match.id)
    if own_date is None:
        return match.estimated_start_time
    return f"{str(own_date)[:10]}T00:00:00Z"
