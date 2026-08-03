"""Flashscore tennis feed -- real start times AND a real in-play flag.

WHY THIS EXISTS. Neither Kalshi nor Polymarket can tell this app that a match has
started. Every field was tested on 2026-08-03 and all three failed:
occurrence_datetime is set once and never revised (measured wrong by up to 20
HOURS, and wrong on 38/38 tour matches with a median error of -1581 minutes,
because a rescheduled match keeps its original stamp); expected_expiration_time
updates but is an expiry, not a start; target_datetime updates but means
different things in different series (+340..+372 min in the Challenger series,
+40..+1725 in the tour one), so a gate built on it flagged a genuinely UNPLAYED
match as started and was reverted. There is no live/status field at all --
product_metadata_derived.live_title is just the tournament name.

Flashscore publishes what the platforms do not:

    AB = 1 scheduled, 2 LIVE, 3 finished

That is a positive in-play flag, and it covers ITF (confirmed live against
"ITF MEN - SINGLES: M25+H Tauste (Spain)"), which matters because ITF is the
majority of this app's tennis inventory.

WHAT WAS VERIFIED BEFORE BUILDING (2026-08-03, all live):
  - Tier coverage today: 231 ITF / 81 challenger / 42 tour out of 355 matches.
  - Start times agree with tennisexplorer EXACTLY where both have the match
    (Berrettini/Navone 17:10, Kopriva/Galarneau 17:10, Mejia/Landaluce 17:15).
  - Against this app's own rows: roughly half of at-risk matches resolve by
    player pair.
  - Of the matches it DOES have, 46 of 118 disagreed with our stored start by
    more than an hour. Those are precisely the wrong platform times.

WHY THE OTHER HALF IS MISSING, measured after an initial wrong guess. The first
version of this docstring claimed the misses were "overwhelmingly Asian ITF
names ... a name-matching gap rather than absent fixtures, so 65% is a floor".
That was wrong. Of 126 missing at-risk matches, 76 had NEITHER player's surname
anywhere in the feed, and only 7 (6%) had both -- and several of those 7 were
different players who merely share a surname. The gap is absent fixtures, not
name forms: Flashscore publishes an order of play about two days out (offsets
+3 and beyond return nothing, and feed "kinds" 1-7 all return the same ~380
matches, so there is no more data to fetch), while Kalshi lists markets days
earlier. Coverage is therefore capped by what has actually been scheduled, and
it closes on its own as a match approaches. Name matching was worth about 3
matches; do not expect more from tuning it.

NOT THE HTML PAGE. flashscore.com/tennis/ is a JavaScript shell; the match data
only arrives over this feed. The response is not JSON -- it is a flat
delimiter-separated stream, `¬` between records and `÷` between key and value,
with `~ZA` opening a tournament block and `~AA` opening a match.

FRAGILITY, DELIBERATELY FAILING OPEN. The request needs a fixed `x-fsign` token.
It is a public constant, not a credential, but if it ever rotates every call
starts failing. Every function here returns EMPTY on any error rather than
raising or guessing, so the callers degrade to exactly today's behaviour: start
times keep their platform value and the live gate goes inert. A feed outage must
never blank the board or hide a match that is genuinely upcoming.
"""
import datetime
import logging

import httpx

log = logging.getLogger("flashscore_tennis")

FEED = "https://local-global.flashscore.ninja/2/x/feed/f_2_{offset}_{kind}_en_1"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    # Public constant the feed requires; see the module docstring on failing open.
    "x-fsign": "SW9D1eZo",
    "Referer": "https://www.flashscore.com/",
}

# kind 3 is the live/today view, kind 2 the per-day list. Offsets are days from
# today; -1..+2 covers everything this app can hold an open market on without
# fetching a week of fixtures nobody is pricing yet.
_FEEDS = [(0, 3), (-1, 2), (0, 2), (1, 2), (2, 2)]

STATUS_SCHEDULED = "1"
STATUS_LIVE = "2"
STATUS_FINISHED = "3"


def _parse(text: str) -> list[dict]:
    """Flat `¬`/`÷` stream -> one dict per match, tournament carried down."""
    out: list[dict] = []
    current: dict | None = None
    tournament: str | None = None
    for record in text.split("¬"):
        if "÷" not in record:
            continue
        key, _, value = record.partition("÷")
        if key == "~ZA":
            tournament = value
        if key == "~AA":
            if current:
                out.append(current)
            current = {"tournament": tournament}
        elif current is not None:
            current[key] = value
    if current:
        out.append(current)
    return out


def _name_key(raw: str | None) -> str | None:
    """Flashscore already publishes "Surname I." -- the SAME abbreviated space
    TennisMatch.player_a_key uses (see market_matcher_tennis), so this only has
    to lowercase it. Verified: "Berrettini M." -> "berrettini m." matches our
    stored key directly, no re-derivation and no second name convention to keep
    in sync."""
    if not raw:
        return None
    key = " ".join(raw.split()).lower()
    return key or None


def _reduced_key(key: str) -> str | None:
    """"galan d. e." -> "galan d." -- surname plus FIRST initial only.

    Flashscore sometimes carries a second initial where this app stores one
    (Daniel Elahi Galan is "galan d. e." there and "galan d." here; likewise
    "schwaerzler j. j.", "gonzalez daniele j. m."). Dropping trailing initials
    reconciles those.

    DELIBERATELY NOT SURNAME-ONLY. Matching on surname alone looked tempting --
    it would have "recovered" more pairs -- but it is unsafe and the data says
    so: among the misses it would have equated `tsitsipas p.` with
    `tsitsipas s.` (Petros and Stefanos, different players), plus `kim d.`/
    `kim g. j.`, `chen s.`/`chen m.`, `sun q.`/`sun f.` and `wang x.`/`wang j.`.
    Silently pointing a market at the wrong player is far worse than not
    resolving it, so the first initial always has to agree.
    """
    parts = key.split()
    if len(parts) < 2:
        return None
    initials = [p for p in parts if p.endswith(".") and len(p) <= 2]
    surname = [p for p in parts if not (p.endswith(".") and len(p) <= 2)]
    if not surname or not initials:
        return None
    return " ".join(surname) + " " + initials[0]


def get_match_states() -> dict[frozenset, dict]:
    """{frozenset({"surname i.", "surname i."}): {"start": datetime, "status": str}}

    Returns {} on any failure -- see the module docstring. A partial result is
    kept when only SOME feeds fail, since a missing day is still better than no
    data and the callers treat "absent" as "no opinion" anyway.
    """
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
                log.debug("flashscore feed %s/%s failed", offset, kind, exc_info=True)
                continue
            for row in rows:
                a, b = _name_key(row.get("AE")), _name_key(row.get("AF"))
                started = row.get("AD")
                if not a or not b or not started or not started.isdigit():
                    continue
                pair = frozenset((a, b))
                state = {
                    "start": datetime.datetime.utcfromtimestamp(int(started)),
                    "status": row.get("AB"),
                    "tournament": row.get("tournament"),
                }
                # Later feeds win only when they carry a DEFINITE status: the
                # live view is fresher than a day list for the same fixture, but
                # a day list must not overwrite a live flag with "scheduled".
                prior = out.get(pair)
                if prior is None or prior.get("status") == STATUS_SCHEDULED:
                    out[pair] = state
    finally:
        client.close()
    return _with_reduced_aliases(out)


def _with_reduced_aliases(states: dict[frozenset, dict]) -> dict[frozenset, dict]:
    """Add second-initial-tolerant aliases, but ONLY where unambiguous.

    An alias is registered only if it does not already exist and no OTHER match
    reduces to the same pair. If two different fixtures collapse onto one
    reduced pair, both aliases are dropped rather than guessed between -- the
    whole point of _reduced_key is to avoid pointing a market at the wrong
    player, and an ambiguous alias would reintroduce exactly that.
    """
    candidates: dict[frozenset, list[frozenset]] = {}
    for pair in states:
        reduced = {_reduced_key(name) or name for name in pair}
        if len(reduced) != 2:
            continue
        key = frozenset(reduced)
        if key == pair:
            continue
        candidates.setdefault(key, []).append(pair)
    for key, origins in candidates.items():
        if key in states or len(origins) != 1:
            continue
        states[key] = states[origins[0]]
    return states


def get_live_pairs() -> set[frozenset]:
    """Just the pairs Flashscore positively reports as IN PLAY.

    Deliberately narrow: only status 2. "Finished" is left out because this app
    already has settled/decided gates fed by real results, and an empty return
    (feed down) must mean "hide nothing" rather than "hide everything"."""
    return {pair for pair, state in get_match_states().items() if state.get("status") == STATUS_LIVE}
