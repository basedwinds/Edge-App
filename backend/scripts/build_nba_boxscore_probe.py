"""FEASIBILITY-GATE scraper (not production): pulls per-game player minutes
from ESPN box scores for one recent NBA season, to test whether a key player
being OUT predicts a team underperforming its Elo expectation (task #34)
BEFORE committing to a full NBA player-impact model.

Data confirmed live 2026-07-22: ESPN's public /summary endpoint carries a full
box score (per-player MIN/PTS/... with stable athlete ids) for any past game;
/injuries carries live availability. Same source (site.api.espn.com) this
app's NBA ingestion already uses -- no Cloudflare gate, unlike stats.nba.com.

Scoped to season 2025 (2024-10 -> 2025-06, ~1,390 games): one full season is
enough to (a) identify each team's core rotation from season-long minutes and
(b) get a solid sample of star-out games. Iterates game DATES (from this app's
own NbaGame table), fetching one scoreboard per date to map to ESPN event ids,
then one /summary per game.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.db.database import SessionLocal  # noqa: E402
from app.db.models import NbaGame  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUTPUT_PATH = DATA_DIR / "nba_boxscore_probe.json"
SEASON = 2024  # extended: derive penalty on more than one season (2024 + 2023); combined with the existing 2025 cache
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
DELAY = 0.8
UA = {"User-Agent": "Mozilla/5.0"}


def _min_of(athlete):
    """Minutes played for one athlete row, or 0 if DNP/absent. MIN is the
    first stat under the box score's own labels."""
    stats = athlete.get("stats") or []
    if not stats:
        return 0
    try:
        return int(str(stats[0]).split(":")[0])
    except (ValueError, IndexError):
        return 0


def parse_boxscore(summary_json):
    """{team_abbrev: {player_name: minutes}} for the 2 teams, or None."""
    players = summary_json.get("boxscore", {}).get("players", [])
    if len(players) != 2:
        return None
    out = {}
    for grp in players:
        abbr = grp.get("team", {}).get("abbreviation")
        stat_blocks = grp.get("statistics") or []
        if not abbr or not stat_blocks:
            return None
        mins = {}
        for a in stat_blocks[0].get("athletes", []):
            name = a.get("athlete", {}).get("displayName")
            if name:
                mins[name] = _min_of(a)
        out[abbr] = mins
    return out if len(out) == 2 else None


def game_dates():
    s = SessionLocal()
    try:
        rows = s.query(NbaGame.gameday).filter(NbaGame.season == SEASON, NbaGame.home_score.isnot(None)).distinct().all()
    finally:
        s.close()
    return sorted({r[0] for r in rows if r[0]})


def main():
    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    dates = game_dates()
    print(f"season {SEASON}: {len(dates)} game-dates; {len(cache)} games already cached", flush=True)
    client = httpx.Client(timeout=30.0, headers=UA)
    got = sum(1 for v in cache.values() if v)
    for di, day in enumerate(dates):
        try:
            sb = client.get(SCOREBOARD, params={"dates": day.replace("-", ""), "limit": 200})
            events = sb.json().get("events", [])
        except httpx.HTTPError:
            time.sleep(DELAY)
            continue
        for ev in events:
            gid = ev["id"]
            if gid in cache:
                continue
            # date + home/away come from the scoreboard event itself, stored
            # WITH the box score so it can be joined to this app's NbaGame
            # table by (date, home, away) -- the two teams meet 3-4x/season,
            # so the pair alone is ambiguous without the date.
            home = away = None
            comps = (ev.get("competitions") or [{}])[0].get("competitors", [])
            for c in comps:
                ab = c.get("team", {}).get("abbreviation")
                if c.get("homeAway") == "home":
                    home = ab
                elif c.get("homeAway") == "away":
                    away = ab
            try:
                sm = client.get(SUMMARY, params={"event": gid})
                box = parse_boxscore(sm.json()) if sm.status_code == 200 else None
            except httpx.HTTPError:
                continue
            cache[gid] = {"date": day, "home": home, "away": away, "minutes": box} if box else None
            if cache[gid]:
                got += 1
            time.sleep(DELAY)
        if (di + 1) % 20 == 0:
            OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
            print(f"  [{di+1}/{len(dates)} dates] {got} games with box scores", flush=True)
    OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
    print(f"\nDone. {sum(1 for v in cache.values() if v)} games with box scores -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
