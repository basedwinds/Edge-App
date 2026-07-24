"""One-off/resumable cache builder for Tennis match history.

Two independent sub-jobs:
1. tennis-data.co.uk (ATP/WTA tour-level, xlsx per year) -- fast, no
   checkpointing needed (a few dozen small file downloads).
2. tennisexplorer.com (Challenger/ITF) -- a day-by-day scrape,
   checkpointed to disk every 30 days so an interruption doesn't lose
   progress; re-running resumes from the last checkpoint. ONE request per
   day covers every tier (see app/clients/tennisexplorer_client.py's
   docstring on why `type=all` is used regardless of tier), so this is
   much cheaper than a naive per-tier-per-day crawl would be.

Run: backend/.venv/Scripts/python.exe scripts/build_tennis_match_cache.py [--start-date YYYY-MM-DD] [--tennisdata-only] [--tennisexplorer-only]
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.tennisexplorer_client import TennisExplorerClient  # noqa: E402
from app.ingestion import tennis_data  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TENNISEXPLORER_CACHE_PATH = DATA_DIR / "tennisexplorer_matches_cache.json"
PROGRESS_PATH = DATA_DIR / "tennisexplorer_scrape_progress.json"

REQUEST_DELAY_SECONDS = 0.4
# Confirmed live 2026-07-18: real embedded odds at Challenger/ITF level go
# back to at least 2018 (spot-checked 2018-06-15, real Nottingham Challenger
# odds found) -- default start date chosen to match that confirmed depth
# rather than guessing further back.
DEFAULT_START_DATE = dt.date(2018, 1, 1)


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


BATCH_SIZE_DAYS = 40  # days fetched concurrently per checkpoint
MAX_WORKERS = 10  # confirmed live 2026-07-18: no rate-limit/block hit at this concurrency; each day is an independent request (see tennisexplorer_client.py's `type=all` finding), so this is a real parallel speedup, not just optimistic


def build_tennisexplorer_cache(start_date: dt.date) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    end_date = dt.date.today()
    all_matches = load_json(TENNISEXPLORER_CACHE_PATH, [])
    progress = load_json(PROGRESS_PATH, {"last_done_date": None})
    resume_from = (
        dt.date.fromisoformat(progress["last_done_date"]) + dt.timedelta(days=1)
        if progress["last_done_date"] else start_date
    )
    if resume_from > end_date:
        print(f"tennisexplorer cache already up to date through {progress['last_done_date']}")
        return

    all_days = [resume_from + dt.timedelta(days=i) for i in range((end_date - resume_from).days + 1)]
    print(f"Scraping tennisexplorer.com: {resume_from} -> {end_date} ({len(all_days)} days, "
          f"{MAX_WORKERS} concurrent workers, batches of {BATCH_SIZE_DAYS})")

    client = TennisExplorerClient()
    done_count = 0
    try:
        for batch_start in range(0, len(all_days), BATCH_SIZE_DAYS):
            batch = all_days[batch_start:batch_start + BATCH_SIZE_DAYS]
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(client.get_results_day, d.year, d.month, d.day): d for d in batch
                }
                for fut in as_completed(futures):
                    d = futures[fut]
                    try:
                        all_matches.extend(fut.result())
                    except Exception as e:
                        print(f"  FAILED {d.isoformat()}: {e}")
            done_count += len(batch)
            last_date_in_batch = batch[-1]
            save_json(TENNISEXPLORER_CACHE_PATH, all_matches)
            save_json(PROGRESS_PATH, {"last_done_date": last_date_in_batch.isoformat()})
            print(f"  [{done_count}/{len(all_days)}] checkpoint through {last_date_in_batch.isoformat()}: "
                  f"{len(all_matches)} total match-rows cached")
    finally:
        client.close()

    print(f"DONE. {len(all_matches)} total tennisexplorer match-rows cached ({resume_from} -> {end_date})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", type=str, default=None)
    parser.add_argument("--tennisdata-only", action="store_true")
    parser.add_argument("--tennisexplorer-only", action="store_true")
    args = parser.parse_args()

    if not args.tennisexplorer_only:
        print("Fetching tennis-data.co.uk (ATP 2000-present, WTA 2007-present)...")
        matches = tennis_data.build_tennisdata_cache()
        print(f"  {len(matches)} tennis-data.co.uk matches cached")

    if not args.tennisdata_only:
        start_date = dt.date.fromisoformat(args.start_date) if args.start_date else DEFAULT_START_DATE
        build_tennisexplorer_cache(start_date)


if __name__ == "__main__":
    main()
