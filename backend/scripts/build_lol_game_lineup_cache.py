"""One-off cache builder for LoL's real PER-GAME player lineups, scraped
from gol.gg -- the source that finally UNBLOCKS player-level modelling for
this title.

Background: every LoL enrichment attempted in this session (patch signal,
roster tenure, player lineups) died on Leaguepedia's Cargo rate limit, which
stayed hard-blocked across many hours. gol.gg turns out to publish the same
underlying data with no such gate, confirmed live 2026-07-21.

One page (`/game/stats/{id}/page-game/`) carries EVERYTHING needed, so this
is a single fetch per game rather than two:
  - real date (2025-07-27)
  - full team names WITH result ("T1 - WIN" / "Nongshim RedForce - LOSS"),
    which is what lets this join to this app's own LoL match cache
  - the tournament name, in the page title ("LCK 2025 Rounds 3-5 WEEK10")
  - both 5-player lineups, as `players/player-stats/` anchors in document
    order: the FIRST five are the blue side, the LAST five the red side.
    Verified live against gol.gg's own `page-summary` tables (which render
    the two lineups as separate tables) on multiple real games -- the
    ordering matched exactly, e.g. T1's real Doran/Oner/Faker/Gumayusi/Keria
    then Nongshim's kingen/GIDEON/Calix/Jiwoo/Lehends.

SCOPED to game ids 70000-80000: probed live, the id space is dense and
roughly linear at ~10k games/year (60000 = 2024-07-08, 70000 = 2025-07-27,
80000 = 2026-07-21, and 90000+ does not exist yet), so this range is ~12
months -- the same scoping rationale as the Valorant crawl.

gol.gg indexes EVERY tier (LCK CL, Arabian League, Road of Legends, ...)
while this app's LoL match cache is Leaguepedia-Primary-tier only, so a real
share of these games will never join to anything. That waste is unavoidable:
tier isn't knowable without fetching the page. Games are stored regardless
and the join is left to the consumer.

Ids with no real game (gaps in the id space -- confirmed live at 75000)
store null rather than being retried forever.

Run: backend/.venv/Scripts/python.exe scripts/build_lol_game_lineup_cache.py
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
OUTPUT_PATH = DATA_DIR / "lol_game_lineups_cache.json"

START_ID, END_ID = 70000, 80000
REQUEST_DELAY_SECONDS = 1.4
LINEUP_SIZE = 5

_client = httpx.Client(
    timeout=30.0,
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
)

_PLAYER_HREF = re.compile(r"players/player-stats/")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _team_and_result(text: str):
    """'T1 - WIN' -> ('T1', True). Splits on the LAST ' - ' so team names
    containing a hyphen survive intact."""
    if " - " not in text:
        return None, None
    name, _, res = text.rpartition(" - ")
    res = res.strip().upper()
    if res not in ("WIN", "LOSS"):
        return None, None
    return name.strip(), res == "WIN"


def parse_game(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    blue_el = soup.select_one(".blue-line-header")
    red_el = soup.select_one(".red-line-header")
    if blue_el is None or red_el is None:
        return None  # gap in the id space, or a page with no real game
    blue, blue_won = _team_and_result(blue_el.get_text(" ", strip=True))
    red, red_won = _team_and_result(red_el.get_text(" ", strip=True))
    if not blue or not red or blue_won is None or red_won is None:
        return None

    dates = _DATE.findall(html)
    if not dates:
        return None

    names = [a.get_text(strip=True) for a in soup.find_all("a", href=_PLAYER_HREF)]
    names = [n for n in names if n]
    if len(names) != LINEUP_SIZE * 2:
        return None  # off-10 scoreboard -> unknown rather than a guessed lineup

    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""
    # "T1 vs NS summary - LCK 2025 Rounds 3-5 WEEK10 - Game of Legends"
    tournament = title.split(" - ")[1].strip() if title.count(" - ") >= 2 else None

    return {
        "date": dates[0],
        "teams": [blue, red],
        "blue_won": blue_won,
        "lineups": [names[:LINEUP_SIZE], names[LINEUP_SIZE:]],
        "tournament": tournament,
    }


def main():
    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    todo = [g for g in range(START_ID, END_ID) if str(g) not in cache]
    print(f"ids {START_ID}-{END_ID}: {len(cache)} cached, {len(todo)} to fetch", flush=True)

    ok = 0
    for i, gid in enumerate(todo):
        try:
            resp = _client.get(f"https://gol.gg/game/stats/{gid}/page-game/")
            parsed = parse_game(resp.text) if resp.status_code == 200 else None
        except httpx.HTTPError:
            time.sleep(REQUEST_DELAY_SECONDS)
            continue  # transient -- leave uncached so a later run retries
        cache[str(gid)] = parsed
        if parsed:
            ok += 1
        if (i + 1) % 100 == 0:
            OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
            print(f"  [{i + 1}/{len(todo)}] {ok} real games parsed so far", flush=True)
        time.sleep(REQUEST_DELAY_SECONDS)

    OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
    good = sum(1 for v in cache.values() if v)
    print(f"\nDone. {len(cache)} ids probed, {good} real games -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
