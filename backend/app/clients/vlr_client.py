"""vlr.gg reader for Valorant event STATE -- group standings and playoff format.

WHY THIS EXISTS. The tournament sim seeds a bracket strongest-to-weakest by Elo
and knows nothing about the event it is pricing. Checked against the real
standings for VCT EMEA Stage 2, that produced this:

    Karmine Corp   Omega 1st (4-1)   model  0.9%   <- won its group
    Eternal Fire   Omega 4th (2-3)   model 16.4%
    Team Liquid    Omega 2nd (3-2)   model 18.7%
    PCIFIC         Alpha 6th (0-5)   model  0.1%

The group stage is OVER -- every team has played its five matches -- and the
model was pricing the tournament as though it had not started. A team that won
its group was the third-least likely title winner on our board. That is not an
approximation, it is ignoring the most informative data available.

WHAT THIS DOES NOT DO. It does not fetch the playoff draw. That genuinely does
not exist yet: every bracket slot holds a group placeholder ("Omega #2",
"Play-In #1-2") with an empty data-team-id, because seeding is decided by the
group stage and the play-in. Standings are the real, available signal.

vlr.gg is plain server-rendered HTML with no gate (unlike Liquipedia, which
403s, and gol.gg, which trails real time by ~6 days and so cannot describe a
live event).
"""
import json
import logging
import re
import time
from html import unescape
from pathlib import Path

import httpx

log = logging.getLogger("vlr_client")

BASE = "https://www.vlr.gg"
_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
_CACHE_FILE = _DATA_DIR / "vlr_event_state.json"
# Event structure changes at most once a day; the sim runs far more often.
_TTL_SECONDS = 6 * 3600

_client = httpx.Client(
    timeout=30.0,
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
)


def _get(path: str) -> str | None:
    try:
        r = _client.get(BASE + path)
        if r.status_code != 200:
            return None
        return r.text
    except httpx.HTTPError:
        return None


def list_events() -> list[tuple[str, str]]:
    """[(event_id, url_path)] for currently listed events."""
    html = _get("/events")
    if not html:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, eid in re.findall(r'href="(/event/(\d+)/[^"]+)"', html):
        if eid not in seen:
            seen.add(eid)
            out.append((eid, href))
    return out


def _clean_team(cell: str) -> str:
    """The team cell carries the name plus a country and a spoiler marker, all
    separated by runs of whitespace -- take the first line only."""
    txt = unescape(re.sub(r"<[^>]+>", "\n", cell))
    for line in (l.strip() for l in txt.split("\n")):
        if line and line.lower() not in ("spoiler hidden",):
            return line
    return ""


def _flat(cell: str) -> str:
    """Tags stripped with NO separator inserted, whitespace collapsed.

    Needed alongside _clean_team because the two cell shapes want opposite
    treatment. A record cell is markup like `<span>4</span>-<span>1</span>`:
    _clean_team splits on tags and would return just "4", silently turning every
    record into an unparseable single number and every standings table into an
    empty dict. Here the pieces have to stay joined.
    """
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", cell))).strip()


def group_standings(event_path: str) -> dict[str, dict]:
    """{team: {"group", "rank", "wins", "losses"}} from an event's group stage.

    Empty when the event has no group stage published, which is a normal state
    (many smaller events are bracket-only) and must not be read as "no teams".
    """
    html = _get(event_path.rstrip("/") + "/group-stage")
    if not html:
        return {}
    out: dict[str, dict] = {}
    for tbl in re.findall(r'<table class="wf-table mod-simple mod-group">(.*?)</table>', html, re.S):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S)
        group = None
        rank = 0
        for row in rows:
            raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
            # Data rows carry a leading EMPTY cell that the header row does not,
            # so the team sits at a different index in each. Dropping blanks
            # first makes both shapes line up: [team, record, ...].
            kept = [(_clean_team(c), _flat(c)) for c in raw]
            kept = [(name, flat) for name, flat in kept if name]
            if not kept:
                continue
            cells = [name for name, _ in kept]
            if group is None:
                group = cells[0] or "?"       # header row carries the group name
                continue
            rec = kept[1][1] if len(kept) > 1 else ""
            m = re.match(r"(\d+)\D+(\d+)", rec)
            if not m:
                continue
            rank += 1
            out[cells[0]] = {"group": group, "rank": rank,
                             "wins": int(m.group(1)), "losses": int(m.group(2))}
    return out


def playoff_format(event_path: str) -> dict:
    """{"double_elim": bool, "slots": int} read off the playoff bracket's own
    column labels. `slots` counts the teams the bracket actually holds, which is
    NOT the size of the market field -- VCT stages sell 12 teams into an 8-team
    playoff, and the four that miss it cannot win the event at all."""
    html = _get(event_path.rstrip("/") + "/playoffs")
    if not html:
        return {}
    cols = [unescape(x).strip() for x in re.findall(r"bracket-col-label[^>]*>\s*([^<]+)", html)]
    if not cols:
        return {}
    upper = [c for c in cols if c.lower().startswith("upper")]
    double = any(c.lower().startswith("lower") for c in cols)
    # An upper bracket of R rounds starts with 2^R teams.
    slots = 2 ** len(upper) if upper else 0
    return {"double_elim": double, "slots": slots, "columns": cols}


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except Exception:
        log.warning("vlr cache not writable at %s", _CACHE_FILE)


def event_state(event_path: str, force: bool = False) -> dict:
    """Cached {"standings", "format"} for one event."""
    cache = _load_cache()
    hit = cache.get(event_path)
    if hit and not force and time.time() - hit.get("fetched_at", 0) < _TTL_SECONDS:
        return hit
    state = {
        "standings": group_standings(event_path),
        "format": playoff_format(event_path),
        "fetched_at": time.time(),
    }
    cache[event_path] = state
    _save_cache(cache)
    return state


# --- LIVE MATCH FLAG -------------------------------------------------------
# WHY THIS EXISTS (2026-08-10). Valorant had NO positive in-play signal at all.
# flashscore_esports_client is the guard the router calls, but its feed (f_36)
# publishes LEAGUE OF LEGENDS only -- sport ids 1-89 were probed and Valorant
# appears nowhere on that host -- so `hides_match` has never once fired for this
# sport. Everything blocking a live Valorant bet was inferred from a stored
# start time, and this app has a recorded case of an esports match beginning
# FOUR HOURS before its recorded start (see valorant_markets.py).
#
# vlr.gg/matches marks in-play games with `mod-live`, which is a positive
# report rather than an inference. Measured when added: 44 pairs on the page,
# 22 of our 40 fixtures-with-markets resolvable, and it correctly flagged the
# two of ours that were live at that moment.
#
# ONE-DIRECTIONAL and FAILS OPEN, exactly like the flashscore guard: it can only
# ever hide a match a real source says is underway, never surface or unhide one.
_LIVE_TTL_SECONDS = 60
_live_cache: dict[str, object] = {"at": 0.0, "pairs": set()}


def _live_pairs_uncached() -> set[frozenset]:
    from app.ingestion.lol_team_aliases import base_key

    html = _get("/matches")
    if not html:
        return set()
    out: set[frozenset] = set()
    # One <a class="match-item"> per match; the live ones carry `mod-live`.
    for block in re.split(r'<a\b', html):
        if "match-item" not in block or "mod-live" not in block:
            continue
        names = re.findall(
            r'match-item-vs-team-name[^>]*>(.*?)</div>', block, re.S)
        cleaned = [unescape(re.sub(r"<[^>]+>", " ", n)).strip() for n in names]
        cleaned = [c for c in cleaned if c]
        if len(cleaned) != 2:
            continue
        a, b = base_key(cleaned[0]), base_key(cleaned[1])
        if a and b and a != b:
            out.add(frozenset((a, b)))
    return out


def live_pairs() -> set[frozenset]:
    """{frozenset({team_key, team_key})} for matches vlr.gg reports as LIVE.

    Empty on any failure -- a scrape that breaks must degrade to today's
    behaviour, never to hiding a board. Cached for 60s because this is a SAFETY
    read on the request path: a match going live is the event to react to
    quickly, but one page fetch per minute is the most vlr.gg should ever see.
    """
    now = time.time()
    if now - float(_live_cache["at"]) < _LIVE_TTL_SECONDS:
        return _live_cache["pairs"]  # type: ignore[return-value]
    try:
        pairs = _live_pairs_uncached()
    except Exception:
        log.debug("vlr live scrape failed", exc_info=True)
        pairs = set()
    _live_cache["at"] = now
    _live_cache["pairs"] = pairs
    return pairs
