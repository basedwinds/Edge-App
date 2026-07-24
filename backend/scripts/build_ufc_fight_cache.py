"""One-off local cache builder for UFC historical fight results + fighter
bios, scraped fresh from ufcstats.com (see app/clients/ufcstats_client.py).
Full crawl is slow (one PoW-gated request per event/fight/fighter, ~0.35s
delay each, ~12k requests total) -- checkpoints to disk every 25 events so
an interruption doesn't lose progress; re-running resumes from the last
checkpoint rather than restarting from event 1.

Run: backend/.venv/Scripts/python.exe scripts/build_ufc_fight_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.ufcstats_client import UfcStatsClient  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
FIGHTS_CACHE_PATH = DATA_DIR / "ufc_fight_cache.json"
BIOS_CACHE_PATH = DATA_DIR / "ufc_fighter_bio_cache.json"
EVENTS_CACHE_PATH = DATA_DIR / "ufc_events_cache.json"
PROGRESS_PATH = DATA_DIR / "ufc_scrape_progress.json"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=None))


def main():
    client = UfcStatsClient()

    events = load_json(EVENTS_CACHE_PATH, None)
    if events is None:
        print("Listing all completed events...")
        t0 = time.monotonic()
        events = client.list_completed_events()
        print(f"  {len(events)} events ({time.monotonic() - t0:.1f}s)")
        save_json(EVENTS_CACHE_PATH, events)
    else:
        print(f"Loaded {len(events)} events from cache")

    progress = load_json(PROGRESS_PATH, {"done_event_ids": []})
    done_event_ids = set(progress["done_event_ids"])
    all_fights = load_json(FIGHTS_CACHE_PATH, [])
    known_fighter_urls: dict[str, str] = {
        row["fighter_id"]: row["fighter_url"] for row in all_fights if row.get("fighter_url")
    }

    remaining = [e for e in events if e["event_id"] not in done_event_ids]
    print(f"{len(done_event_ids)} events already scraped, {len(remaining)} remaining")

    for i, event in enumerate(remaining):
        try:
            fight_urls = client.get_event_fight_urls(event["event_url"])
            time.sleep(0.35)
            for fight_url in fight_urls:
                rows = client.get_fight_details(fight_url)
                time.sleep(0.35)
                if not rows:
                    continue
                for row in rows:
                    row["event_id"] = event["event_id"]
                    row["event_name"] = event["event_name"]
                    row["event_date"] = event["event_date"]
                    all_fights.append(row)
                    known_fighter_urls[row["fighter_id"]] = row["fighter_url"]
            done_event_ids.add(event["event_id"])
        except Exception as e:
            print(f"  FAILED event {event['event_name']} ({event['event_id']}): {e}")
            continue

        if (i + 1) % 25 == 0 or i == len(remaining) - 1:
            save_json(FIGHTS_CACHE_PATH, all_fights)
            save_json(PROGRESS_PATH, {"done_event_ids": sorted(done_event_ids)})
            print(f"  [{i + 1}/{len(remaining)}] checkpoint: {len(all_fights)} fight-rows, "
                  f"{len(done_event_ids)}/{len(events)} events done")

    save_json(FIGHTS_CACHE_PATH, all_fights)
    save_json(PROGRESS_PATH, {"done_event_ids": sorted(done_event_ids)})
    print(f"Fight scrape done: {len(all_fights)} fight-rows across {len(done_event_ids)} events")

    # Phase 2: fighter bios (static physical attributes only) -- one request
    # per UNIQUE fighter encountered above, not per fight-row.
    bios = load_json(BIOS_CACHE_PATH, [])
    done_fighter_ids = {b["fighter_id"] for b in bios}
    remaining_fighters = [
        (fid, url) for fid, url in known_fighter_urls.items() if fid not in done_fighter_ids
    ]
    print(f"{len(done_fighter_ids)} fighter bios already scraped, {len(remaining_fighters)} remaining")

    for i, (fighter_id, fighter_url) in enumerate(remaining_fighters):
        try:
            bio = client.get_fighter_bio(fighter_url)
            time.sleep(0.35)
            if bio:
                bios.append(bio)
        except Exception as e:
            print(f"  FAILED fighter {fighter_id}: {e}")
            continue

        if (i + 1) % 100 == 0 or i == len(remaining_fighters) - 1:
            save_json(BIOS_CACHE_PATH, bios)
            print(f"  [{i + 1}/{len(remaining_fighters)}] checkpoint: {len(bios)} fighter bios")

    save_json(BIOS_CACHE_PATH, bios)
    print(f"DONE. {len(all_fights)} fight-rows, {len(bios)} fighter bios")
    client.close()


if __name__ == "__main__":
    main()
