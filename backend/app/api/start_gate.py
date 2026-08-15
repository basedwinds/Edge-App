"""Re-apply the started-gate to a cached payload at SERVE time.

WHY THIS EXISTS. Every sport's /markets router decides whether a market is
still bettable when it BUILDS the payload -- volume, already-final,
already-started -- and those decisions are then frozen for as long as the cache
serves that body. response_cache.py has carried this note since 2026-08-03:

    "the right fix is to re-evaluate the cheap time-based gates
     (already_started/already_decided) when a cached payload is SERVED, so
     cache age cannot produce a stale safety decision at all. Logged as the
     follow-up rather than bolted on here, since it touches every sport's
     payload shape, not just tennis."

That follow-up is this module, and it is what makes serving a payload PAST its
TTL safe. Without it the only options were a short TTL (which the warm pass can
no longer keep up with -- 290s of compute per 180s window is over 100% duty
cycle, so no scheduling fixes it) or a longer one that lets an already-started
match keep a live stake suggestion.

WIRED FOR ALL THIRTEEN SPORTS, DELIBERATELY. The recurring defect in this repo
is a guard wired to SOME of the app: the futures spread guard shipped to 3 of 13
routers, the duplicate-listing cap to 4 of 13 and double-staked $4,180. Seven
sports plus racing already serialised an absolute instant; NFL/NBA/WNBA/CFB/MLB
carried only `gameday` + a `gametime` clock string whose timezone lives in the
router, so a payload-level gate could not see them. Those five now serialise
`start_time_utc` from the SAME function their own started-gate calls, so the
field and the gate cannot drift apart. scripts/board_artifact_scan.py checks the
coverage so it cannot silently regress.

NULLS THE STAKE, DOES NOT DROP THE ROW. The board filters on
suggested_stake_dollars != null (see project_only_show_placeable_bets), so
clearing the stake is exactly "no longer recommended". Dropping rows would also
empty the browse tables, which is not what a started match should do -- it is
still worth looking at, just not worth staking.

CANNOT BE MORE AGGRESSIVE THAN THE ROUTER. It reads the very field the router's
own gate reasons from, and applies the same predicate (start <= now) that the
router applied at build time. Anything it removes, a fresh build would have
removed too. That matters because these instants are imperfect --
estimated_start_time is sometimes borrowed from a colliding fixture -- but a bad
instant already excludes the row upstream, so this adds no new failure mode.

FUTURES PAYLOADS ARE UNTOUCHED, and correctly so: a season-long future has no
single start instant, which is also why futures produce no CLV. Rows carrying
none of the fields below simply pass through.
"""
from __future__ import annotations

import datetime
import json
import logging

log = logging.getLogger(__name__)

# In priority order. The first one PRESENT on a row is the one used -- not the
# first one non-null, so a row that carries the field but has no known start is
# treated as unknown rather than silently falling through to a weaker field.
#   start_time_utc        NFL, NBA, WNBA, CFB, MLB  (added for this gate)
#   estimated_start_time  tennis, soccer, cs2, lol, valorant, cod, mma
#   close_time            racing -- the market's own close, which is the right
#                         instant for a race: entries lock at close, not at the
#                         green flag.
START_FIELDS = ("start_time_utc", "estimated_start_time", "close_time")

# Cleared together. Leaving stake_pool set on a zero-stake row would make it look
# like an allocation decision rather than an ineligible market.
STAKE_FIELDS = ("suggested_stake_dollars", "suggested_stake_units", "stake_pool")


def iso_z(instant: datetime.datetime | None) -> str | None:
    """UTC instant -> "...Z", matching what the sports that already carry an
    absolute start time emit. None passes through as None (unknown start)."""
    if instant is None:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=datetime.timezone.utc)
    return instant.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value) -> datetime.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def apply_start_gate(body: bytes, now: datetime.datetime | None = None) -> tuple[bytes, int]:
    """Return (body, rows_gated). Any parse problem returns the body unchanged.

    Failing OPEN rather than closed is deliberate: this runs on the serve path
    for every stale hit, and a payload shape it does not understand must degrade
    to today's behaviour, never to a blank board."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        rows = json.loads(body)
    except Exception:
        return body, 0
    if not isinstance(rows, list):
        return body, 0

    gated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("suggested_stake_dollars") is None:
            continue     # already not recommended -- nothing to clear
        field = next((f for f in START_FIELDS if f in row), None)
        if field is None:
            continue     # futures and anything else without a start instant
        start = _parse(row.get(field))
        if start is None or start > now:
            continue
        for key in STAKE_FIELDS:
            if key in row:
                row[key] = None
        gated += 1

    if not gated:
        return body, 0
    return json.dumps(rows).encode(), gated


def start_field_coverage(rows: list) -> tuple[int, int]:
    """(rows carrying a start field, total rows) -- used by the board scan to
    prove the gate is wired for every sport rather than assumed to be."""
    if not rows:
        return 0, 0
    have = sum(1 for r in rows if isinstance(r, dict) and any(f in r for f in START_FIELDS))
    return have, len(rows)
