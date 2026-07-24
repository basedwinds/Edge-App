"""FEASIBILITY-GATE scraper (not production): pulls gol.gg per-player stats
(KDA/CSM/DPM/WPM) for a recent window of games, to test whether
performance-weighted player updates beat shared-credit BEFORE committing to a
full ~8k-game re-scrape (task #33).

Scoped to game ids 77000-80000 (~recent 2.4k real games) -- the densest,
most-recent window, enough for a self-contained walk-forward test. Reuses the
game ids already known-real from data/lol_game_lineups_cache.json, so it only
fetches pages that exist. page-summary carries the two 5-player stat tables.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
LINEUP_CACHE_PATH = DATA_DIR / "lol_game_lineups_cache.json"
OUTPUT_PATH = DATA_DIR / "lol_player_stats_probe.json"
LO, HI = 70000, 80000  # widened from the 77000-80000 feasibility window to the full lineup-cache range; already-cached ids are skipped
DELAY = 1.3

_client = httpx.Client(timeout=30.0, follow_redirects=True,
                       headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})


def parse_stats(html: str):
    """Two ordered stat tables (blue then red), each 5 rows of
    [Player, KDA, CSM, DPM, WPM]. Returns [{name,k,d,a,csm,dpm,wpm}*5]*2 or
    None. KDA cell is 'K/D/A (ratio)'."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tb in soup.find_all("table"):
        rows = tb.find_all("tr")
        if not rows:
            continue
        hdr = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if hdr[:5] != ["Player", "KDA", "CSM", "DPM", "WPM"]:
            continue
        team = []
        for rw in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in rw.find_all(["td", "th"])]
            if len(cells) < 5:
                continue
            m = re.match(r"(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", cells[1])
            if not m:
                continue
            try:
                team.append({
                    "name": cells[0],
                    "k": int(m.group(1)), "d": int(m.group(2)), "a": int(m.group(3)),
                    "csm": float(cells[2]), "dpm": float(cells[3]), "wpm": float(cells[4]),
                })
            except ValueError:
                continue
        if len(team) == 5:
            out.append(team)
    return out if len(out) == 2 else None


def main():
    real_ids = [int(k) for k, v in json.loads(LINEUP_CACHE_PATH.read_text(encoding="utf-8")).items() if v and LO <= int(k) < HI]
    real_ids.sort()
    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    todo = [g for g in real_ids if str(g) not in cache]
    print(f"{len(real_ids)} real games in {LO}-{HI}; {len(cache)} cached; {len(todo)} to fetch", flush=True)
    ok = 0
    for i, gid in enumerate(todo):
        try:
            resp = _client.get(f"https://gol.gg/game/stats/{gid}/page-summary/")
            parsed = parse_stats(resp.text) if resp.status_code == 200 else None
        except httpx.HTTPError:
            time.sleep(DELAY)
            continue
        cache[str(gid)] = parsed
        if parsed:
            ok += 1
        if (i + 1) % 100 == 0:
            OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
            print(f"  [{i+1}/{len(todo)}] {ok} with stats", flush=True)
        time.sleep(DELAY)
    OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
    print(f"\nDone. {sum(1 for v in cache.values() if v)} games with stats -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
