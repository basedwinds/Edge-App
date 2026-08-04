"""Flashscore esports feed -- a real in-play/finished flag for LoL, CS2, Valorant.

WHY THIS EXISTS. Same reason as flashscore_tennis_client: neither Kalshi nor
Polymarket can tell this app a match has started, and the esports start times are
demonstrably wrong. The case that prompted it (user-reported 2026-08-04): DRX vs
OKSavingsBank BRION was offered as an upcoming bet at its recorded 14:30Z start
while the match had actually begun at 10:30Z and was long over. The app had no
result either, because the LoL results source does not recognise that team's
sponsor name.

Flashscore publishes the missing signal on sport id 36, in exactly the same flat
`¬`/`÷` stream the tennis feed uses, with the same fields:

    AB = 1 scheduled, 2 LIVE, 3 finished

Confirmed live: it carries LCK, LEC, LCS, LPL, KeSPA Cup, LFL and Prime League
for LoL, plus Counter-Strike and Valorant tournaments, and its start times
disagree with ours by up to an hour on matches we hold (DN SOOPers vs Gen.G:
ours 09:15Z, really 08:15Z).

WHAT IT WILL AND WILL NOT COVER, measured before building rather than hoped for.
Inside Flashscore's own ~4-day window this app held 158 LoL matches and only 29
(18%) joined by team pair. The binding constraint is team IDENTITY, not the feed:
Flashscore says "Hanjin Brion" where Kalshi says "OKSavingsBank BRION", and
"Hanwha Life" where we store "Hanwha Life Esports". Sponsor renames are not
mechanically derivable (the same reason lol_team_aliases refuses to guess them),
so the join rate stays modest until team identity is solved properly.

That is still worth shipping because the gate is ONE-DIRECTIONAL: it can only
hide a match some real source positively reports as live or finished, and it has
no opinion on anything it cannot match. Of the 7 matches it recognised as
finished, 4 had NO result in this app at all -- precisely the ones nothing else
could catch. It will NOT catch the reported DRX match, whose name it spells
differently.

THE DATE ANCHOR matters more here than in tennis. Two esports teams meet
repeatedly, and this app already has rows where an old fixture and a genuine
rematch share one match row. A "finished" report from last week must never hide
next week's rematch, so `hides_match` requires the match's own scheduled day to
be no later than the day the feed says it actually started.

FAILS OPEN, like the tennis client: every function returns empty on any error, so
a feed outage degrades to exactly today's behaviour rather than blanking a board
or hiding something genuinely upcoming.
"""
import datetime
import logging

import httpx

from app.clients.flashscore_tennis_client import _HEADERS, _parse

log = logging.getLogger("flashscore_esports")

FEED = "https://local-global.flashscore.ninja/2/x/feed/f_36_{offset}_{kind}_en_1"

# Same offset/kind sweep the tennis client uses. Verified on the esports feed:
# offsets -1..+2 return the whole published window and kinds 2 and 3 agree.
_FEEDS = [(0, 3), (-1, 2), (0, 2), (1, 2), (2, 2)]

STATUS_SCHEDULED = "1"
STATUS_LIVE = "2"
STATUS_FINISHED = "3"

# Tournament-title keyword per app sport. Flashscore prefixes every esports
# tournament with its title, e.g. "LEAGUE OF LEGENDS: LCK (South Korea)".
TITLE_KEYWORDS = {
    "lol": "LEAGUE OF LEGENDS",
    "cs2": "COUNTER-STRIKE",
    "valorant": "VALORANT",
}


def _team_key(raw: str | None) -> str | None:
    """Normalized team key, shared with the LoL alias work so the two cannot
    drift apart on diacritics or punctuation."""
    from app.ingestion.lol_team_aliases import base_key

    return base_key(raw) or None


def get_match_states(sport: str) -> dict[frozenset, dict]:
    """{frozenset({team_key, team_key}): {"start": datetime, "status": str}}

    Empty on any failure. A partial result is kept when only SOME feeds fail --
    a missing day is better than no data, and callers treat absence as "no
    opinion" anyway.
    """
    keyword = TITLE_KEYWORDS.get(sport)
    if not keyword:
        return {}
    out: dict[frozenset, dict] = {}
    try:
        client = httpx.Client(timeout=25.0, headers=_HEADERS)
    except Exception:
        return {}
    try:
        for offset, kind in _FEEDS:
            try:
                resp = client.get(FEED.format(offset=offset, kind=kind))
                if resp.status_code != 200 or not resp.text:
                    continue
                rows = _parse(resp.text)
            except Exception:
                log.debug("flashscore esports feed %s/%s failed", offset, kind, exc_info=True)
                continue
            for row in rows:
                if keyword not in (row.get("tournament") or "").upper():
                    continue
                a, b = _team_key(row.get("AE")), _team_key(row.get("AF"))
                started = row.get("AD")
                if not a or not b or a == b or not started or not started.isdigit():
                    continue
                state = {
                    "start": datetime.datetime.utcfromtimestamp(int(started)),
                    "status": row.get("AB"),
                    "tournament": row.get("tournament"),
                }
                pair = frozenset((a, b))
                # A definite status must not be overwritten by a day list that
                # still calls the same fixture "scheduled".
                prior = out.get(pair)
                if prior is None or prior.get("status") == STATUS_SCHEDULED:
                    out[pair] = state
    finally:
        client.close()
    return out


# Cached off the request path. Short TTL because this is a SAFETY decision: a
# match going live is exactly the event to react to quickly, and the sweep is 5
# cheap requests. One cache per sport, since each filters the same feed.
_CACHE_TTL_SECONDS = 60
_cache: dict[str, dict] = {}


def cached_match_states(sport: str) -> dict[frozenset, dict]:
    """`get_match_states` with a short TTL, FAILING OPEN.

    On any error -- including the shared x-fsign token rotating and every request
    4xx-ing -- this returns the last good result, or {} before the first success.
    An empty mapping hides nothing, so a dead feed degrades to exactly today's
    behaviour rather than blanking a board.
    """
    import time

    now = time.monotonic()
    entry = _cache.get(sport) or {"at": None, "data": {}}
    at = entry.get("at")
    if isinstance(at, float) and now - at < _CACHE_TTL_SECONDS:
        return entry["data"]
    try:
        fresh = get_match_states(sport)
    except Exception:
        return entry["data"]
    # Only advance on a real answer: an empty result is indistinguishable from
    # "feed down", so it must not overwrite a good set and un-hide live matches.
    if fresh:
        _cache[sport] = {"at": now, "data": fresh}
        return fresh
    entry["at"] = now
    _cache[sport] = entry
    return entry["data"]


def hides_match(states: dict[frozenset, dict], team_a: str | None, team_b: str | None,
                scheduled_start: str | None) -> bool:
    """Does a real source say THIS match is already under way or over?

    `scheduled_start` is the app's own stored start (ISO). It is the date anchor:
    a live/finished report can only speak to a match scheduled no later than the
    day that reported fixture actually started, so an earlier meeting of the same
    two teams can never hide a genuine rematch.
    """
    a, b = _team_key(team_a), _team_key(team_b)
    if not a or not b:
        return False
    state = states.get(frozenset((a, b)))
    if state is None or state.get("status") not in (STATUS_LIVE, STATUS_FINISHED):
        return False
    start = state.get("start")
    if not isinstance(start, datetime.datetime):
        return False
    if not scheduled_start:
        # No stored start to anchor against: fall back to "the reported fixture
        # has actually begun", which is still a positive real-world signal.
        return start <= datetime.datetime.utcnow()
    try:
        ours = datetime.date.fromisoformat(str(scheduled_start)[:10])
    except ValueError:
        return False
    return ours <= start.date()
