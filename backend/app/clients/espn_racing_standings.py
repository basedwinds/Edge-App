"""Live F1 championship standings from ESPN's core API -- the current-points
input the season-championship sim needs (a driver's title odds depend on how far
back they are, not just their pace). Driver name comes from the athlete $ref;
championship points from the entry's `championshipPts` stat.
"""
import datetime
import logging

from app.clients.base import get_json

log = logging.getLogger("espn_racing_standings")

_STANDINGS_URL = (
    "https://sports.core.api.espn.com/v2/sports/racing/leagues/{league}/seasons/{year}/types/2/standings/0"
)

# ESPN's racing league slugs. IndyCar is "irl" on ESPN (its own historic name for
# the series) -- "indycar" returns HTTP 400. Verified live 2026-08-02: the irl
# endpoint returns 33 entries with real championshipPts (Palou 457, Malukas 374).
# NASCAR is "nascar-premier" (the Cup Series). Verified live 2026-08-06: it
# returns 40 entries with real championshipPts AND a `wins` stat, which the
# playoff model needs because Cup qualification is win-first.
#
# It was simply ABSENT from this map until then, which is worth recording: the
# missing key made fetch_driver_standings("nascar") return {} silently, and that
# empty dict was read downstream as "NASCAR has no rateable drivers" rather than
# "nobody asked ESPN". A missing mapping and a genuinely empty feed are
# indistinguishable once the {} is returned.
_LEAGUE_SLUG = {"f1": "f1", "irl": "irl", "nascar": "nascar-premier"}


def _points(entry: dict) -> float | None:
    recs = entry.get("records") or []
    stats = recs[0].get("stats", []) if recs else entry.get("stats", [])
    for s in stats:
        if s.get("name") == "championshipPts":
            try:
                return float(s.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def _stat(entry: dict, name: str) -> float | None:
    recs = entry.get("records") or []
    stats = recs[0].get("stats", []) if recs else entry.get("stats", [])
    for s in stats:
        if s.get("name") == name:
            try:
                return float(s.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def fetch_driver_wins(series: str = "nascar", year: int | None = None) -> tuple[dict[str, int], float | None, float | None]:
    """({driver: race wins}, currentWeek, totalWeeks) -- what the NASCAR playoff
    model needs on top of points, because Cup qualification is WIN-first: a
    single win essentially books a playoff berth regardless of points, so a
    points-only view would misjudge who is even in contention.

    currentWeek/totalWeeks come from the same payload and are what tell the
    model how many regular-season races are left to still qualify through.
    """
    league = _LEAGUE_SLUG.get(series)
    if not league:
        return {}, None, None
    year = year or datetime.date.today().year
    wins: dict[str, int] = {}
    week = total = None
    try:
        data = get_json(_STANDINGS_URL.format(league=league, year=year))
    except Exception:
        log.exception("%s wins fetch failed", series)
        return wins, None, None
    for entry in data.get("standings", []):
        w = _stat(entry, "wins")
        week = week or _stat(entry, "currentWeek")
        total = total or _stat(entry, "totalWeeks")
        ath = entry.get("athlete")
        ref = ath.get("$ref") if isinstance(ath, dict) else None
        if not ref or w is None:
            continue
        try:
            a = get_json(ref)
            name = a.get("displayName") or a.get("fullName")
        except Exception:
            continue
        if name:
            wins[name] = int(w)
    return wins, week, total


def fetch_driver_standings(series: str = "f1", year: int | None = None) -> dict[str, float]:
    """{driver_display_name: current championship points}. Empty on any failure
    (the sim then treats it as a fresh season, which is the safe fallback)."""
    league = _LEAGUE_SLUG.get(series)
    if not league:
        return {}
    year = year or datetime.date.today().year
    out: dict[str, float] = {}
    try:
        data = get_json(_STANDINGS_URL.format(league=league, year=year))
    except Exception:
        log.exception("%s standings fetch failed", series)
        return out
    for entry in data.get("standings", []):
        pts = _points(entry)
        ath = entry.get("athlete")
        ref = ath.get("$ref") if isinstance(ath, dict) else None
        if not ref or pts is None:
            continue
        try:
            a = get_json(ref)
            name = a.get("displayName") or a.get("fullName")
        except Exception:
            continue
        if name:
            out[name] = pts
    return out


def fetch_f1_driver_standings(year: int | None = None) -> dict[str, float]:
    """Back-compat alias for the original F1-only entry point."""
    return fetch_driver_standings("f1", year)
