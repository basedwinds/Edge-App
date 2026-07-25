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
    "https://sports.core.api.espn.com/v2/sports/racing/leagues/f1/seasons/{year}/types/2/standings/0"
)


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


def fetch_f1_driver_standings(year: int | None = None) -> dict[str, float]:
    """{driver_display_name: current championship points}. Empty on any failure
    (the sim then treats it as a fresh season, which is the safe fallback)."""
    year = year or datetime.date.today().year
    out: dict[str, float] = {}
    try:
        data = get_json(_STANDINGS_URL.format(year=year))
    except Exception:
        log.exception("f1 standings fetch failed")
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
