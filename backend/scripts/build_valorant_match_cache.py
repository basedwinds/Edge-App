"""One-off local cache builder for Valorant historical match results,
scraped fresh from vlr.gg -- parallel to build_cs2_match_cache.py, but a
SIMPLER crawl shape: vlr.gg has dedicated season-hub pages
(vlr.gg/vct-2023..2026, confirmed live 2026-07-19) that each list exactly
the curated top-tier VCT International/regional events for that year (15
events/season for 2024-2026 -- Champions, 2 Masters, and Kickoff/Stage 1/
Stage 2 for each of the 4 regions; 10 for 2023, a different format that
year), same "curated tier list, not every tiny regional qualifier" scoping
call as CS2's S-Tier-only crawl.

Each event's own /event/matches/{id}/{slug}/?series_id=all page uses the
EXACT SAME match-item DOM shape as vlr.gg's live /matches listing (confirmed
live) -- reuses app.ingestion.valorant_data.parse_matches_from_html
directly, no new parser needed, unlike CS2 which needed a generalized
parser for the bracket-popup shape.

Checkpoints to disk after every event (same resume-on-interrupt discipline
as build_cs2_match_cache.py/build_ufc_fight_cache.py).

Run: backend/.venv/Scripts/python.exe scripts/build_valorant_match_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from app.ingestion.valorant_data import parse_matches_from_html  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "valorant_historical_match_cache.json"
PROGRESS_PATH = DATA_DIR / "valorant_scrape_progress.json"

BASE_URL = "https://www.vlr.gg"
SEASON_HUB_SLUGS = [
    "vct-2023", "vct-2024", "vct-2025", "vct-2026",
    # Game Changers (women's division) has its own parallel season hubs --
    # confirmed live 2026-07-19, added after the FIRST crawl pass revealed a
    # real gap: real, live-traded Kalshi markets exist for GC matches (e.g.
    # "Gentle Mates GC vs G2 Gozen"), but those teams never appeared in the
    # main-circuit-only crawl, so their ratings stayed stuck at BASE_RATING
    # even after the rest of the model had real signal. Same "one region,
    # one hub" URL pattern as the main vct-* hubs (gc-2023 through gc-2026),
    # reusing the exact same crawl code -- no new parsing needed.
    "gc-2023", "gc-2024", "gc-2025", "gc-2026",
    # VCT Challengers League (regional tier below VCT International) --
    # added 2026-07-20 to grow the real market-odds backtest sample (see
    # backtest_valorant_market_odds.py): only 9 of 439 real settled Map-1
    # Kalshi events had matched this app's own historical cache, and Kalshi
    # trades far more Challengers-tier matches than the main circuit + GC
    # alone cover. Confirmed live vlr.gg has its own real, curated
    # Challengers hub pages (vcl-2024/2025/2026: 68/62/66 real events each)
    # mirroring the exact same event-item DOM structure as vct-*/gc-* --
    # no new crawl code needed, same reuse as the GC addition above.
    "vcl-2024", "vcl-2025", "vcl-2026",
]
REQUEST_DELAY_SECONDS = 0.75  # polite crawl delay, same discipline as build_cs2_match_cache.py

_client = httpx.Client(
    timeout=30.0,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
)


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=None), encoding="utf-8")


def list_events() -> list[dict]:
    """Returns [{name, event_id, slug}] for every real event listed across
    all season hubs (main circuit + Game Changers + Challengers League) --
    deduped by event_id in case a season hub ever double-lists one (not
    expected, but cheap to guard)."""
    events: dict[str, dict] = {}
    for season_slug in SEASON_HUB_SLUGS:
        resp = _client.get(f"{BASE_URL}/{season_slug}")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for item in soup.find_all("a", class_="event-item"):
            href = item.get("href") or ""
            parts = href.strip("/").split("/")
            if len(parts) < 3 or parts[0] != "event":
                continue
            event_id, slug = parts[1], parts[2]
            title_div = item.find("div", class_="event-item-title")
            name = title_div.get_text(strip=True) if title_div else slug
            events[event_id] = {"name": name, "event_id": event_id, "slug": slug}
    return list(events.values())


def crawl_event(event_id: str, slug: str) -> list[dict]:
    resp = _client.get(f"{BASE_URL}/event/matches/{event_id}/{slug}/", params={"series_id": "all"})
    resp.raise_for_status()
    return parse_matches_from_html(resp.text)


def main():
    events = load_json(DATA_DIR / "valorant_event_list_cache.json", None)
    if events is None:
        print("Listing curated VCT events (2023-2026 season hubs)...")
        events = list_events()
        print(f"  {len(events)} events found")
        save_json(DATA_DIR / "valorant_event_list_cache.json", events)
    else:
        print(f"Loaded {len(events)} events from cache")

    progress = load_json(PROGRESS_PATH, {"done_event_ids": []})
    done_ids = set(progress["done_event_ids"])
    all_matches: dict[str, dict] = {m["source_match_id"]: m for m in load_json(MATCH_CACHE_PATH, [])}

    remaining = [e for e in events if e["event_id"] not in done_ids]
    print(f"{len(done_ids)} events already scraped, {len(remaining)} remaining")

    for i, e in enumerate(remaining):
        print(f"[{i + 1}/{len(remaining)}] {e['name']} ({e['event_id']})...", end=" ", flush=True)
        try:
            matches = crawl_event(e["event_id"], e["slug"])
        except httpx.HTTPError as ex:
            print(f"FAILED ({ex}), will retry on next run")
            continue
        for m in matches:
            all_matches[m["source_match_id"]] = m
        print(f"{len(matches)} matches")

        done_ids.add(e["event_id"])
        progress["done_event_ids"] = sorted(done_ids)
        save_json(PROGRESS_PATH, progress)
        save_json(MATCH_CACHE_PATH, list(all_matches.values()))
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nDone. {len(all_matches)} total real historical matches cached at {MATCH_CACHE_PATH}")


if __name__ == "__main__":
    main()
