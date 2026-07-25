"""Fetches the FINAL finishing order of a completed race from ESPN's core API
(the same source + shape as scripts/build_racing_cache.py) so racing bets can be
auto-settled. Driver ids returned ARE the ESPN athlete ids -- the exact ids the
racing model / racing_ratings key on -- so grading can compare a resolved bet
driver straight against the finishing order.

result shape: {"order": [athlete_id, ... best->worst], "pole": athlete_id|None}.
Only returns a result once the race is proven done (a competitor carries the
`winner` flag); an upcoming/in-progress race returns None (nothing to grade yet).
"""
import logging

from app.clients.base import get_json

log = logging.getLogger("espn_racing_results")

_SLUG = {"f1": "f1", "nascar": "nascar-premier", "irl": "irl"}
_SITE = "https://site.api.espn.com/apis/site/v2/sports/racing/{slug}"
_CORE = "https://sports.core.api.espn.com/v2/sports/racing/leagues/{slug}"


def _event_ids_for_season(series: str, season: int) -> list[tuple[str, str, str]]:
    """(espn_event_id, event_name, date) for every event on the season scoreboard."""
    slug = _SLUG.get(series)
    if not slug:
        return []
    try:
        d = get_json(f"{_SITE.format(slug=slug)}/scoreboard?dates={season}")
    except Exception:
        log.exception("racing scoreboard fetch failed for %s %s", series, season)
        return []
    out = []
    for ev in d.get("events", []):
        out.append((str(ev.get("id")), ev.get("name") or "", (ev.get("date") or "")[:10]))
    return out


def fetch_race_result(series: str, espn_event_id: str) -> "dict | None":
    """Finishing order + pole for one ESPN race event, or None if not finished."""
    slug = _SLUG.get(series)
    if not slug:
        return None
    core = _CORE.format(slug=slug)
    try:
        ce = get_json(f"{core}/events/{espn_event_id}")
    except Exception:
        return None
    comps = []
    for cp in ce.get("competitions", []):
        comps.append(cp)
    if not comps:
        return None
    # the Race competition = the one with a winner flag + the largest field
    withwin = [cp for cp in comps if any(x.get("winner") for x in cp.get("competitors", []))]
    race = max(withwin, key=lambda cp: len(cp.get("competitors", []))) if withwin else None
    if race is None:
        return None  # not finished yet

    finishers = []  # (order, athlete_id)
    pole = None
    for comp in race.get("competitors", []):
        ath = comp.get("athlete") or {}
        aid = str(ath.get("id")) if ath.get("id") else None
        if not aid:
            ref = ath.get("$ref")
            if ref:
                try:
                    aid = str(get_json(ref).get("id"))
                except Exception:
                    aid = None
        if not aid:
            continue
        order = comp.get("order")
        if order is not None:
            finishers.append((order, aid))
        if comp.get("startOrder") == 1:
            pole = aid
    if not finishers:
        return None
    finishers.sort(key=lambda t: t[0])
    return {"order": [aid for _o, aid in finishers], "pole": pole}
