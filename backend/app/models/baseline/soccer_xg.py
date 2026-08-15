"""Attach Understat xG to a football-data fixture, for the rating blend (#202).

WHAT THIS IS FOR. #167 measured that blending xG into the soccer attack/defence
residual beats pure goals: w=0.50, held-out logloss 0.99235 -> 0.98934 and Brier
0.59168 -> 0.58936, better in 4 of 5 leagues. PURE xG is WORSE than production
(train 0.99423 vs 0.99205), so this is a blend and never a swap.

THE JOIN, and why it is not a name match. Understat and football-data disagree on
club names for most of the Bundesliga and much of the Premier League --
"Borussia Dortmund"/"Dortmund", "Eintracht Frankfurt"/"Ein Frankfurt",
"Wolverhampton Wanderers"/"Wolves". Only 61 of 168 names matched directly. The
alias map is built by scripts/build_understat_alias_map.py from
(league, date, scoreline) agreement -- a name-free key -- because fuzzy name
matching produces confident-looking aliases that quietly rate the wrong club
(see project_soccer_team_name_aliases: UNIQUE != SAFE).

Verified at FIXTURE level, not by reading the alias list: 21,433 of 21,589
Understat matches (99.3%) reconcile to a football-data fixture with an identical
scoreline. Of the 156 that do not, 152 are the SAME fixture recorded 1-2 days
apart, 2 are genuine source disagreements over awarded results (D1 2024-12-14
Union Berlin, I1 2016-08-28 Sassuolo -- Understat holds the on-pitch score,
football-data the awarded one), and 2 are absent from football-data entirely.

DATE_SLACK exists for those 152. Understat and football-data occasionally file a
fixture on adjacent days; requiring an exact date would silently drop them.
Matching on (league, both teams, date within +-2) is still unambiguous because a
given pair meets at most twice a season, months apart.

UNKNOWN = NO ADJUSTMENT. Every lookup miss returns None and the caller keeps pure
goals -- the same convention park, weather and the MLB pitcher term already use.
A coverage hole degrades to today's behaviour, never to a guess. That matters
here: only 5 of 33 rated leagues have xG at all.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parents[3].parent / "data"
ALIAS_PATH = DATA / "understat_alias_map.json"
XG_PATH = DATA / "soccer_xg_cache.json"

# Leagues Understat covers. Everything else keeps pure goals, permanently.
XG_LEAGUES = frozenset({"E0", "SP1", "D1", "I1", "F1"})

# Weight on xG in the blend. FITTED, not chosen: swept 0.00-1.00 on seasons
# <=2021 with a clean interior optimum (0.99089 at 0.25, 0.99086 at 0.50,
# 0.99197 at 0.75), then spent once on held-out 2022-2025. See
# scripts/fit_soccer_xg_ratings.py.
XG_BLEND_WEIGHT = 0.50

DATE_SLACK = 2

_cache: dict = {}


def _load() -> dict:
    """{(league, home_fd, away_fd): [(date, xg_h, xg_a)]} keyed on FOOTBALL-DATA
    names, so callers never see an Understat name."""
    if "by_fixture" in _cache:
        return _cache["by_fixture"]
    by: dict = {}
    try:
        alias = json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
        raw = json.loads(XG_PATH.read_text(encoding="utf-8"))
    except Exception:
        # A missing or unreadable cache must NOT break rating refresh for the
        # other 28 leagues -- degrade to "no xG anywhere".
        log.exception("soccer xG cache/alias unreadable -- xG blend disabled")
        _cache["by_fixture"] = {}
        return {}
    for lg, seasons in raw.items():
        if lg not in XG_LEAGUES:
            continue
        for _season, matches in seasons.items():
            for m in matches:
                xh, xa = m.get("xg_h"), m.get("xg_a")
                if xh is None or xa is None:
                    continue
                ah = alias.get(f"{lg}|{m.get('home')}")
                aa = alias.get(f"{lg}|{m.get('away')}")
                if not ah or not aa:
                    continue
                by.setdefault((lg, ah, aa), []).append((m["date"][:10], float(xh), float(xa)))
    _cache["by_fixture"] = by
    log.info("soccer xG loaded: %d distinct fixtures across %d leagues", len(by), len(XG_LEAGUES))
    return by


def lookup(league: str, match_date: str, home: str, away: str):
    """(xg_home, xg_away) for this fixture, or None. Names are FOOTBALL-DATA's."""
    if league not in XG_LEAGUES or not match_date:
        return None
    rows = _load().get((league, home, away))
    if not rows:
        return None
    d = str(match_date)[:10]
    for date, xh, xa in rows:
        if date == d:
            return (xh, xa)
    try:
        target = dt.date.fromisoformat(d)
    except ValueError:
        return None
    for date, xh, xa in rows:
        try:
            if abs((dt.date.fromisoformat(date) - target).days) <= DATE_SLACK:
                return (xh, xa)
        except ValueError:
            continue
    return None


def blended(goals: int | float, xg: float | None) -> float:
    """The value fed to the rating residual. Pure goals when xG is unknown."""
    if xg is None:
        return float(goals)
    return (1.0 - XG_BLEND_WEIGHT) * float(goals) + XG_BLEND_WEIGHT * float(xg)
