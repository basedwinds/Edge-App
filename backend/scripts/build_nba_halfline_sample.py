"""One-off sample builder for NBA half-line (1st/2nd half spread+total)
constants. Unlike NFL (nflverse's cached PBP parquet has an exact `game_half`
column, zero extra network cost), ESPN's per-game summary endpoint is the
only free source of quarter-by-quarter NBA scores, and it's ONE CALL PER
GAME (confirmed live 2026-07-16: ~0.5s/call) -- pulling all 15,000+ cached
games would take over 2 hours. This samples ~600 REG games across recent
seasons instead, a smaller but still real sample (NFL's own half-line
constants were derived from 3,663 games -- this is smaller, documented as
such, not silently presented as equally robust).

Run: backend/.venv/Scripts/python.exe scripts/build_nba_halfline_sample.py
"""
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "nba_schedule_cache.json"
OUT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "nba_halfline_sample.json"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
SAMPLE_SIZE_PER_SEASON = 200
SEASONS = [2024, 2025, 2026]  # 3 recent completed seasons, ~600 games total


def fetch_linescores(event_id: str, retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            resp = httpx.get(SUMMARY_URL, params={"event": event_id}, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            comp = data["header"]["competitions"][0]
            out = {}
            for c in comp["competitors"]:
                scores = [int(ls["displayValue"]) for ls in c.get("linescores", [])]
                out[c["homeAway"]] = scores
            if "home" not in out or "away" not in out:
                return None
            return out
        except (httpx.HTTPStatusError, httpx.TransportError):
            if attempt == retries - 1:
                return None
            time.sleep(2 * (attempt + 1))
        except (KeyError, ValueError, TypeError):
            return None
    return None


def main():
    games = json.loads(CACHE_PATH.read_text())
    by_season = {}
    for g in games:
        if g["game_type"] == "REG" and g.get("home_score") is not None:
            by_season.setdefault(g["season"], []).append(g)

    sample = []
    for season in SEASONS:
        season_games = by_season.get(season, [])
        # Evenly spaced sample across the season rather than the first N
        # games, to avoid any early/late-season bias.
        step = max(1, len(season_games) // SAMPLE_SIZE_PER_SEASON)
        sample.extend(season_games[::step][:SAMPLE_SIZE_PER_SEASON])

    print(f"Sampling {len(sample)} games across seasons {SEASONS}...")
    out = []
    for i, g in enumerate(sample):
        ls = fetch_linescores(g["id"])
        if ls is not None:
            out.append({"id": g["id"], "home_score": g["home_score"], "away_score": g["away_score"], "linescores": ls})
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(sample)} fetched, {len(out)} usable")

    OUT_PATH.write_text(json.dumps(out))
    print(f"Wrote {len(out)} usable games to {OUT_PATH}")


if __name__ == "__main__":
    main()
