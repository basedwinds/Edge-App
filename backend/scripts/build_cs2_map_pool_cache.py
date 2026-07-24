"""One-off cache builder for CS2's real per-map results (map name + winner
per map), for the map-pool-specific rating investigation (2026-07-20,
user-requested model-quality pass). Reuses build_cs2_match_cache.py's own
already-cached tournament list and crawl_tournament() logic entirely --
cs2_data.py::_parse_match_info now captures a "maps" field on every parsed
match (see that module's own _per_map_results docstring), so re-crawling the
SAME tournament pages the historical crawl already visited is enough; no new
scraping mechanism needed.

SCOPED to the last ~12 months (tournaments whose end_date_iso >= CUTOFF),
not the full 94-tournament/8,843-match historical set -- REAL FINDING
(2026-07-20): CS2's own competitive map pool has genuinely rotated (a real
2024 tournament's bracket popups showed {Ancient, Anubis, Inferno, Mirage,
Nuke, Overpass, Vertigo}; 2026's current pool has Dust II instead of
Vertigo, confirmed live) -- training map-specific ratings on maps no longer
in the active pool would be actively misleading, so this deliberately
doesn't re-crawl the full historical window. 32 tournaments / ~2,436
matches fall in this window (checked live against the existing historical
cache before committing to this scrape).

Run: backend/.venv/Scripts/python.exe scripts/build_cs2_map_pool_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.cs2_data import parse_matches_from_html  # noqa: E402
import httpx  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TOURNAMENT_LIST_PATH = DATA_DIR / "cs2_tournament_list_cache.json"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"
OUTPUT_PATH = DATA_DIR / "cs2_map_pool_cache.json"
PROGRESS_PATH = DATA_DIR / "cs2_map_pool_scrape_progress.json"

CUTOFF = "2025-07-01"  # ~12 months back -- see module docstring for why not the full historical window
BASE_URL = "https://liquipedia.net/counterstrike"
API_URL = "https://liquipedia.net/counterstrike/api.php"
REQUEST_DELAY_SECONDS = 2.0  # same politeness delay as build_cs2_match_cache.py

_client = httpx.Client(
    timeout=30.0,
    headers={"User-Agent": "nfl-edge-app/0.1 (personal research project; contact via GitHub)"},
)


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=None), encoding="utf-8")


def list_subpages(slug: str) -> list[str]:
    resp = _client.get(API_URL, params={
        "action": "query", "list": "allpages", "apprefix": f"{slug}/", "aplimit": 50, "format": "json",
    })
    resp.raise_for_status()
    data = resp.json()
    return [p["title"] for p in data.get("query", {}).get("allpages", [])]


def crawl_tournament(name: str, slug: str) -> list[dict]:
    pages_to_fetch = [slug] + list_subpages(slug)
    time.sleep(REQUEST_DELAY_SECONDS)
    matches: dict[str, dict] = {}
    for page_title in pages_to_fetch:
        try:
            resp = _client.get(f"{BASE_URL}/{page_title.replace(' ', '_')}")
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        for row in parse_matches_from_html(resp.text, default_event_name=name, default_tournament_slug=slug):
            matches[row["source_match_id"]] = row
        time.sleep(REQUEST_DELAY_SECONDS)
    return list(matches.values())


def main():
    tournaments = load_json(TOURNAMENT_LIST_PATH, None)
    if tournaments is None:
        print(f"ERROR: {TOURNAMENT_LIST_PATH} not found -- run build_cs2_match_cache.py first")
        return
    recent = [t for t in tournaments if t["end_date_iso"] >= CUTOFF]
    print(f"{len(tournaments)} total tournaments, {len(recent)} within the last ~12 months (>= {CUTOFF})")

    progress = load_json(PROGRESS_PATH, {"done_slugs": []})
    done_slugs = set(progress["done_slugs"])
    all_matches: dict[str, dict] = {m["source_match_id"]: m for m in load_json(OUTPUT_PATH, [])}

    remaining = [t for t in recent if t["slug"] not in done_slugs]
    print(f"{len(done_slugs)} already scraped, {len(remaining)} remaining")

    with_maps = 0
    for i, t in enumerate(remaining):
        print(f"[{i + 1}/{len(remaining)}] {t['name']} ({t['slug']})...", end=" ", flush=True)
        try:
            matches = crawl_tournament(t["name"], t["slug"])
        except httpx.HTTPError as e:
            print(f"FAILED ({e}), will retry on next run")
            continue
        n_with_maps = sum(1 for m in matches if m.get("maps"))
        with_maps += n_with_maps
        for m in matches:
            all_matches[m["source_match_id"]] = m
        print(f"{len(matches)} matches, {n_with_maps} with real per-map data")

        done_slugs.add(t["slug"])
        progress["done_slugs"] = sorted(done_slugs)
        save_json(PROGRESS_PATH, progress)
        save_json(OUTPUT_PATH, list(all_matches.values()))

    print(f"\nDone. {len(all_matches)} total matches cached at {OUTPUT_PATH}, {with_maps} with real per-map results this run")


if __name__ == "__main__":
    main()
