"""Build data/mlb_postseason_cache.json -- real postseason results, so the
season sim's PENNANT and WORLD SERIES legs can finally be scored.

WHY IT WAS MISSING. mlb_schedule_cache.json filters to game_type "R", which is
correct for simulating a regular season but means the validation had no truth
for the two highest-value futures legs. check_mlb_season_sim.py could score
division and playoff berth from final records alone; pennant and championship
are postseason facts and simply were not on hand.

THE GOTCHA THAT COST THE FIRST ATTEMPT. MLB StatsAPI does NOT use gameType "P"
for the postseason, and asking for it returns ZERO games with HTTP 200 -- a
silent empty, not an error. The real codes are:

    F  Wild Card
    D  Division Series
    L  League Championship Series
    W  World Series

And `gameType`/`gameTypes` as a query parameter is ignored on this endpoint;
filtering has to happen client-side over a date range. Confirmed live
2026-08-09: a date-ranged October query returns 43 games typed {F, D, L, W}
while the same range with gameType=P returns nothing.

That failure shape -- 200 OK, empty list, no error -- is the same one that made
the Liquipedia rate-limit look like "this wiki has no data". Hence the explicit
per-season count check below rather than trusting a quiet result.

WHAT IS DERIVED, and why only these two:
    pennant       won the League Championship Series (type L)
    championship  won the World Series (type W)
Both are read from real per-game winners, never from a hardcoded champions
table, so this cannot drift out of step with the schedule it ships beside.
"""
from __future__ import annotations

import collections
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
OUT = DATA_DIR / "mlb_postseason_cache.json"

API = "https://statsapi.mlb.com/api/v1/schedule"
UA = "nfl-edge-app/1.0 (personal research; github.com/basedwinds/Edge-App)"

# The postseason runs October into early November. A generous window costs
# nothing (non-postseason types are filtered out) and covers schedule shifts
# like 2020's expanded, earlier bracket.
WINDOW = ("-09-25", "-11-15")
POSTSEASON_TYPES = {"F", "D", "L", "W"}
SEASONS = list(range(2016, 2026))
DELAY = 1.0

_client = httpx.Client(timeout=45.0, headers={"User-Agent": UA})


def team_abbreviations() -> dict[int, str]:
    """teamId -> abbreviation.

    The schedule endpoint returns team NAMES ("Chicago Cubs") while every other
    MLB cache in this app keys on the ABBREVIATION ("CHC"), so a name-keyed
    cache would silently fail to join and score nothing. Keyed on the numeric
    team id rather than matched by name: ids are stable across relocations and
    rebrands, names are not."""
    r = _client.get("https://statsapi.mlb.com/api/v1/teams", params={"sportId": 1})
    r.raise_for_status()
    out = {}
    for t in r.json().get("teams", []):
        if t.get("id") and t.get("abbreviation"):
            out[int(t["id"])] = t["abbreviation"]
    return out


_ABBREV: dict[int, str] = {}


def fetch_season(season: int) -> list[dict]:
    try:
        r = _client.get(API, params={
            "sportId": 1,
            "startDate": f"{season}{WINDOW[0]}",
            "endDate": f"{season}{WINDOW[1]}",
        })
    except httpx.HTTPError as exc:
        print(f"  {season}: transport error {type(exc).__name__}")
        return []
    finally:
        time.sleep(DELAY)
    if r.status_code != 200:
        print(f"  {season}: HTTP {r.status_code}")
        return []

    rows = []
    for date in r.json().get("dates", []):
        for g in date.get("games", []):
            if g.get("gameType") not in POSTSEASON_TYPES:
                continue
            teams = g.get("teams") or {}
            home, away = teams.get("home") or {}, teams.get("away") or {}
            ht = _ABBREV.get(int((home.get("team") or {}).get("id") or 0))
            at = _ABBREV.get(int((away.get("team") or {}).get("id") or 0))
            if not ht or not at:
                continue
            hs, as_ = home.get("score"), away.get("score")
            if hs is None or as_ is None:
                continue  # scheduled but unplayed
            rows.append({
                "game_pk": g.get("gamePk"),
                "season": season,
                "game_type": g.get("gameType"),
                "series": g.get("seriesDescription"),
                "gameday": (g.get("gameDate") or "")[:10],
                "home_team": ht, "away_team": at,
                "home_score": hs, "away_score": as_,
                "winner": ht if hs > as_ else at,
            })
    return rows


def main() -> None:
    global _ABBREV
    _ABBREV = team_abbreviations()
    print(f"{len(_ABBREV)} team abbreviations loaded")
    all_rows: list[dict] = []
    for season in SEASONS:
        rows = fetch_season(season)
        by_type = collections.Counter(r["game_type"] for r in rows)
        # LOUD on a season that came back empty or without a World Series.
        # A silent zero here is exactly how the gameType="P" mistake survived.
        flag = ""
        if not rows:
            flag = "   <-- NO GAMES, check the query"
        elif by_type.get("W", 0) == 0:
            flag = "   <-- no World Series games found"
        print(f"  {season}: {len(rows):3d} games {dict(by_type)}{flag}")
        all_rows.extend(rows)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(all_rows, indent=1), encoding="utf-8")
    print(f"\nwrote {len(all_rows)} postseason games -> {OUT}")

    # Face validity: name each season's champion. A wrong join shows up here
    # instantly as a team that plainly did not win.
    print("\nWorld Series winners derived from the data:")
    ws = collections.defaultdict(list)
    for r in all_rows:
        if r["game_type"] == "W":
            ws[r["season"]].append(r)
    for season in sorted(ws):
        games = sorted(ws[season], key=lambda r: r["gameday"])
        wins = collections.Counter(g["winner"] for g in games)
        champ, n = wins.most_common(1)[0]
        print(f"   {season}  {champ:4s} ({n} wins in {len(games)} games)")


if __name__ == "__main__":
    main()
