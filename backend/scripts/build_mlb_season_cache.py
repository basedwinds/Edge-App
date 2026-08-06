"""Fetch prior MLB regular seasons into <settings.data_dir>/mlb_game_cache.json.

NOTE ON LOCATION: settings.data_dir resolves to %LOCALAPPDATA%/nfl-edge-app,
NOT the repo's data/ folder where the soccer and WNBA caches sit. Both are
gitignored, so neither is in version control -- but look in the LOCALAPPDATA one
for this file.

WHY THIS EXISTS. `mlb_games` holds only the CURRENT season (2,430 rows for
2026), so MLB's season simulator -- which prices win totals, division winners,
playoff qualifiers, best record and conference champion -- could never be
calibration-backtested the way CFB/NFL/NBA/WNBA and soccer were. It was the
single largest untested block of live futures (8 of 21 rows on the cross-sport
list, 2026-08-05).

Same shape and role as data/wnba_game_cache.json and data/cfb_game_cache.json:
a flat list of finished regular-season games, enough to rebuild Elo from
scratch for any prior season. Uses the free MLB StatsAPI (no key), one call per
season.

Run:  python -m scripts.build_mlb_season_cache [first_season] [last_season]
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.clients.statsapi_mlb_client import get_schedule  # noqa: E402
from app.config import settings  # noqa: E402

CACHE_NAME = "mlb_game_cache.json"
# 2020 is deliberately fetchable but noted: it was a 60-game COVID season, so a
# win-total backtest that treats it like a 162-game year would be nonsense. The
# backtest filters on games-per-team rather than hard-coding the year.
DEFAULT_FIRST, DEFAULT_LAST = 2015, 2025


def fetch_season(season: int) -> list[dict]:
    rows = get_schedule(f"{season}-01-01", f"{season}-12-31",
                        game_type="R", hydrate_pitchers=False)
    out = []
    for g in rows:
        st = (g.get("status") or {}).get("abstractGameState")
        if st != "Final":
            continue
        teams = g.get("teams") or {}
        home, away = teams.get("home") or {}, teams.get("away") or {}
        hs, as_ = home.get("score"), away.get("score")
        if hs is None or as_ is None:
            continue
        ht = ((home.get("team") or {}).get("name"))
        at = ((away.get("team") or {}).get("name"))
        if not ht or not at:
            continue
        out.append({
            "game_pk": g.get("gamePk"),
            "season": int(g.get("season") or season),
            "date": g.get("officialDate") or (g.get("gameDate") or "")[:10],
            "home": ht,
            "away": at,
            "home_score": hs,
            "away_score": as_,
        })
    return out


def main() -> None:
    first = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIRST
    last = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LAST
    all_rows: list[dict] = []
    for season in range(first, last + 1):
        rows = fetch_season(season)
        teams = {r["home"] for r in rows} | {r["away"] for r in rows}
        print(f"  {season}: {len(rows):>5} final games, {len(teams)} teams")
        all_rows.extend(rows)
    # de-dupe on game_pk; a doubleheader has two distinct gamePks so this is safe
    seen, deduped = set(), []
    for r in all_rows:
        if r["game_pk"] in seen:
            continue
        seen.add(r["game_pk"])
        deduped.append(r)
    deduped.sort(key=lambda r: (r["date"], r["game_pk"]))
    path = pathlib.Path(settings.data_dir) / CACHE_NAME
    path.write_text(json.dumps(deduped), encoding="utf-8")
    print(f"wrote {len(deduped)} games ({first}-{last}) -> {path}")


if __name__ == "__main__":
    main()
