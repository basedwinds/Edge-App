"""Run an `IN (...)` query over an id list too long for one SQLite statement.

REAL BUG this exists for (found 2026-08-04). SQLite caps host variables per
statement -- SQLITE_MAX_VARIABLE_NUMBER, 999 before 3.32 and 32766 after -- so
`col.in_(ids)` raises OperationalError("too many SQL variables") once the list
crosses that line. Nothing warns you as you approach it: the query works, and
works, and then one day the sport has grown by one market and the whole endpoint
500s. That is exactly how /tennis/markets broke -- tennis reached 34,617 markets
and the router's snapshot batch-load died, which the frontend's per-sport
guard() rendered as "tennis has no bets" rather than as an error.

markets.py already had this loop inline (added 2026-07-23 when the divergence
scanner hit the same wall); the per-sport routers each grew their own unchunked
copy of the pattern. This is that loop, once, so the next router to cross the
line does not have to rediscover it.

900 is deliberately far below even the old 999 cap: these lists are id integers,
the queries are indexed, and a handful of extra round-trips costs far less than
a threshold nobody can see coming.
"""
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")

CHUNK_SIZE = 900


def fetch_in_chunks(ids: Iterable[int], run: Callable[[list[int]], list[T]],
                    size: int = CHUNK_SIZE) -> list[T]:
    """Concatenate `run(chunk)` over `ids` split into `size`-long chunks.

    `run` takes a list of ids and returns that chunk's rows. Only safe for
    queries whose results simply UNION across chunks -- a positive `IN (...)`
    filter does, a negated `NOT IN (...)` does NOT (it needs the intersection,
    since every row is "not in" some other chunk). Callers with a NOT IN should
    pass a subquery instead, which costs no host variables at all.
    """
    ids = list(ids)
    if not ids:
        return []
    if len(ids) <= size:
        return run(ids)
    out: list[T] = []
    for i in range(0, len(ids), size):
        out.extend(run(ids[i:i + size]))
    return out
