"""Keep a scraped series result pointing at the right team.

THE BUG THIS CLOSES, reported by the user 2026-08-09: a Valorant bet on GIANTX
GC was settled as a LOSS with the note "FALKE VENOM 2-0 GIANTX GC". vlr.gg says
GIANTX GC 2-0 FALKE VENOM. The app had the scoreline backwards and paid the
wrong side.

WHY. The esports catalogs reconcile a scraped row onto an existing fixture with
match_by_names_only(), which matches the two names in EITHER order -- it has to,
because different sources list the same fixture with the sides swapped. The
result fields were then written POSITIONALLY:

    found = match_by_names_only(row["team_a"], row["team_b"], upcoming)
    ...
    match.maps_won_a = row["maps_won_a"]   # row's A is GIANTX GC
    match.winner     = row["winner"]       # row says "team_a"

If the stored fixture is FALKE VENOM vs GIANTX GC and the scrape is GIANTX GC vs
FALKE VENOM, the winner's map count lands in the loser's column and "team_a"
silently changes meaning. Nothing errors; the fixture just describes a different
match than the one that was played.

This is the same defect class as the LoL lineup swap -- an ORDERED tuple stored
under an UNORDERED key -- and it was live in three titles at once (Valorant,
CS2, LoL), which is why the correction lives here once instead of being pasted
into each catalog.

THE FIX: before writing, ask whether the row is oriented the same way as the
stored fixture, and flip the ordered fields if not. Orientation is decided with
the SAME name comparator the matcher used, so the two cannot disagree.
"""
from __future__ import annotations

from typing import Callable


def row_is_flipped(row: dict, match, names_match: Callable[[str, str], bool]) -> bool:
    """True when the scraped row lists the fixture's teams the other way round.

    Deliberately conservative: it returns True only when the row's A is
    recognisably the fixture's B AND the row's B is recognisably the fixture's
    A. Anything ambiguous -- a name that matches both sides, or neither -- is
    reported as NOT flipped, so the caller keeps the existing orientation
    rather than acting on a guess.
    """
    ra, rb = row.get("team_a"), row.get("team_b")
    ma, mb = getattr(match, "team_a", None), getattr(match, "team_b", None)
    if not (ra and rb and ma and mb):
        return False
    same = names_match(ra, ma) and names_match(rb, mb)
    flipped = names_match(ra, mb) and names_match(rb, ma)
    return flipped and not same


def oriented_result(row: dict, match, names_match: Callable[[str, str], bool]) -> tuple:
    """(maps_won_a, maps_won_b, winner) in the STORED fixture's orientation.

    Returns the row's own values untouched when orientations agree. Any of the
    three may be None, meaning "the scrape did not say" -- callers must keep
    treating None as no-op rather than as a zero.
    """
    a, b, winner = row.get("maps_won_a"), row.get("maps_won_b"), row.get("winner")
    if not row_is_flipped(row, match, names_match):
        return a, b, winner
    winner = {"team_a": "team_b", "team_b": "team_a"}.get(winner, winner)
    return b, a, winner
