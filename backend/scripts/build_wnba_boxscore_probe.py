"""FEASIBILITY-GATE scraper (not production): per-game player minutes from
ESPN box scores for recent WNBA seasons, to CALIBRATE how much a key player
being OUT is worth before shipping a WNBA injury adjustment.

Direct sibling of build_nba_boxscore_probe.py -- same endpoints, same shape,
just the wnba path. The NBA magnitude was calibrated this way rather than
guessed, and copying NBA's 3.0pp to the WNBA would be a guess: WNBA rosters
carry ~11-12 active players against the NBA's 12-15, so one starter is a
LARGER share of team value, and the number should be measured, not assumed.

Reads its game dates from this app's own wnba_game_cache.json (which already
holds 2021-2026), not the DB -- the DB only keeps a 14-day forward window.

Output: data/wnba_boxscore_probe.json, {game_id: {date, home, away,
minutes: {team: {player: minutes}}}}. Re-runnable; already-cached games are
skipped, so an interrupted run resumes.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_PATH = DATA_DIR / "wnba_game_cache.json"
OUTPUT_PATH = DATA_DIR / "wnba_boxscore_probe.json"
SEASONS = {2024, 2025}
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
DELAY = 0.5
UA = {"User-Agent": "Mozilla/5.0"}


def _min_of(athlete):
    """Minutes played for one athlete row, 0 if DNP/absent. MIN is the first
    stat under the box score's own labels (same as the NBA probe)."""
    stats = athlete.get("stats") or []
    if not stats:
        return 0
    try:
        return int(str(stats[0]).split(":")[0])
    except (ValueError, IndexError):
        return 0


def parse_boxscore(summary_json):
    """{team_abbrev: {player_name: minutes}} for the 2 teams, or None."""
    players = (summary_json.get("boxscore") or {}).get("players") or []
    if len(players) != 2:
        return None
    out = {}
    for grp in players:
        abbr = (grp.get("team") or {}).get("abbreviation")
        blocks = grp.get("statistics") or []
        if not abbr or not blocks:
            return None
        mins = {}
        for a in blocks[0].get("athletes", []):
            name = (a.get("athlete") or {}).get("displayName")
            if name:
                mins[name] = _min_of(a)
        out[abbr] = mins
    return out if len(out) == 2 else None


def game_dates():
    raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return sorted({
        g["date"][:10] for g in raw.values()
        if g.get("season") in SEASONS and g.get("home_score") is not None
    })


def main():
    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    dates = game_dates()
    print(f"seasons {sorted(SEASONS)}: {len(dates)} game-dates; {len(cache)} games cached", flush=True)
    client = httpx.Client(timeout=30.0, headers=UA)
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
            home = away = None
            for c in (ev.get("competitions") or [{}])[0].get("competitors", []):
                ab = (c.get("team") or {}).get("abbreviation")
                if c.get("homeAway") == "home":
                    home = ab
                elif c.get("homeAway") == "away":
                    away = ab
            try:
                sm = client.get(SUMMARY, params={"event": gid})
                box = parse_boxscore(sm.json()) if sm.status_code == 200 else None
            except httpx.HTTPError:
                box = None
            cache[gid] = {"date": day, "home": home, "away": away, "minutes": box} if box else None
            time.sleep(DELAY)
        if di % 20 == 0:
            OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
            got = sum(1 for v in cache.values() if v)
            print(f"  {di}/{len(dates)} dates, {got} box scores", flush=True)
    OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
    got = sum(1 for v in cache.values() if v)
    print(f"done: {got} box scores of {len(cache)} games seen", flush=True)


if __name__ == "__main__":
    main()
