"""NFL schedule ingestion, pulled directly from nflverse's published CSV.

Deliberately NOT using the `nfl_data_py` PyPI wrapper: it pins `pandas<2.0`,
which has no prebuilt wheel for current Python and fails to build from source
(missing pkg_resources in pip's isolated build env) -- confirmed broken on this
machine 2026-07-14. nflverse publishes the same underlying data as plain CSV,
so we read it directly with the stdlib csv module (no pandas dependency at all
for this ingestion path). This also conveniently includes historical
sportsbook closing lines (spread_line/total_line/moneylines) for free, useful
later for Phase 2/7 calibration.
"""
import csv
import io

import httpx

GAMES_CSV_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

_INT_FIELDS = {
    "season", "week", "away_score", "home_score", "home_moneyline", "away_moneyline",
    "away_rest", "home_rest", "div_game",
}
_FLOAT_FIELDS = {"spread_line", "total_line"}


def fetch_games(season: int | None = None) -> list[dict]:
    resp = httpx.get(GAMES_CSV_URL, timeout=30.0)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    games = []
    for row in reader:
        if season is not None and row.get("season") != str(season):
            continue
        games.append(_coerce_row(row))
    return games


def _coerce_row(row: dict) -> dict:
    out = dict(row)
    for field in _INT_FIELDS:
        out[field] = _to_int(row.get(field))
    for field in _FLOAT_FIELDS:
        out[field] = _to_float(row.get(field))
    return out


def _to_int(v):
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def build_opponent_index(games: list[dict]) -> dict[tuple[int, str, int], dict]:
    """Returns {(season, team, week): {"opponent": abbr, "won": bool|None,
    "was_home": bool}} for every REG-season game -- used by
    schedule_spot_rules.py (lookahead/letdown-spot, via `opponent`/`won`) and
    road_trip_rules.py (consecutive-road-game fatigue, via `was_home`).
    `won` is None for games without a final score yet (can't have happened
    yet for a "next week" lookup, and not yet final for an in-progress
    "last week" one)."""
    index: dict[tuple[int, str, int], dict] = {}
    for g in games:
        if g["game_type"] != "REG":
            continue
        season, week = g["season"], g["week"]
        home, away = g["home_team"], g["away_team"]
        home_score, away_score = g.get("home_score"), g.get("away_score")
        won_home = won_away = None
        if home_score is not None and away_score is not None:
            won_home = home_score > away_score
            won_away = away_score > home_score
        index[(season, home, week)] = {"opponent": away, "won": won_home, "was_home": True}
        index[(season, away, week)] = {"opponent": home, "won": won_away, "was_home": False}
    return index
