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
_LEAGUE_SLUG = {"f1": "f1", "irl": "irl"}


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
