"""One-off local cache builder for home-plate umpire assignments -- checks
whether umpire-specific scoring tendencies carry a real signal for MLB
totals, the other real, promising, NOT-YET-ATTEMPTED candidate flagged after
the weather work (temperature/wind-direction) shipped.

Confirmed live 2026-07-17: MLB Stats API's own `/schedule` endpoint (the
same bulk endpoint this app already uses for the schedule cache) supports
`hydrate=officials` and returns each game's 4 umpire assignments (Home
Plate/1B/2B/3B) in the SAME bulk call -- no per-game boxscore fetch needed
(confirmed: a full 2024 season, 2,469 games, came back in one ~2.5s call,
4.4MB). Far cheaper than build_mlb_boxscore_cache.py's one-call-per-game
pattern.

Scoped to 2016-2025 (10 years, matching the moneyline baseline's own
historical range) since umpire-specific tendencies need a real per-umpire
sample size to be trustworthy (an individual HP umpire works roughly
25-35 games/season) -- a single season alone (~2,430 games / ~90 umpires)
would be too thin.

Run: backend/.venv/Scripts/python.exe scripts/build_mlb_umpire_cache.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import time  # noqa: E402

from app.clients.base import get_json  # noqa: E402

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_umpire_cache.json"
BASE = "https://statsapi.mlb.com/api/v1"
SEASONS = list(range(2016, 2026))


def main():
    cache: dict[str, str] = {}  # gamePk -> home plate umpire full name
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
        print(f"Resuming: {len(cache)} games already cached")

    t0 = time.monotonic()
    for season in SEASONS:
        url = (
            f"{BASE}/schedule?sportId=1&startDate={season}-01-01&endDate={season}-12-31"
            "&gameType=R&hydrate=officials"
        )
        d = get_json(url)
        n_season = 0
        for date_entry in d.get("dates", []):
            for g in date_entry.get("games", []):
                gid = str(g["gamePk"])
                if gid in cache:
                    continue
                hp = next(
                    (o["official"]["fullName"] for o in g.get("officials", []) if o.get("officialType") == "Home Plate"),
                    None,
                )
                if hp:
                    cache[gid] = hp
                    n_season += 1
        print(f"  {season}: +{n_season} games ({time.monotonic() - t0:.0f}s elapsed)")
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=None))

    print(f"Done: {len(cache)} games cached. Wrote {CACHE_PATH} ({CACHE_PATH.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
