"""One-off local cache builder for LoL historical match results, queried
fresh from Leaguepedia's real Cargo API -- parallel to
build_cs2_match_cache.py/build_valorant_match_cache.py, but a genuinely
different crawl mechanic: no page-by-page HTML scraping, one paginated SQL-
like cargoquery instead (see app/ingestion/lol_data.py).

Scoped to TournamentLevel="Primary" via a real Cargo JOIN (MatchSchedule
joined on Tournaments.OverviewPage) -- same "top-tier only, not every small
regional/amateur league" curation call as CS2's S-Tier-only crawl and
Valorant's curated VCT-hub-only crawl. "Primary" is Leaguepedia's own real
tournament-tier field (confirmed via a real, actively-used open-source
client's own default: github.com/mrtolkien/leaguepedia_parser's
get_tournaments() defaults to tournament_level="Primary"), not a guessed
value -- this covers LCK/LPL/LEC/LCS(LTA)/Worlds/MSI and their real
regional-league equivalents, not smaller amateur circuits (confirmed live:
an unfiltered MatchSchedule query surfaced teams like "0 win 6 loses" and
"Canette E-sport", clearly amateur/hobbyist play, not the pro circuit this
app's live Kalshi/Polymarket markets actually trade).

RATE LIMITING is real and unusually aggressive on this specific endpoint
(see lol_data.py::cargoquery's own docstring -- NOT a simple fixed cooldown,
confirmed live via inconsistent success/failure at 5-40s gaps) -- this
script relies entirely on cargoquery()'s own exponential-backoff retry
logic rather than trying to guess a safe fixed interval, plus an additional
flat pacing delay between successful paginated calls to avoid tripping the
limit as often. Expect this crawl to take a genuinely long time (many
minutes to hours) given how often it may need to back off -- checkpointed
per page (same resume-on-interrupt discipline as the other 2 esports
crawlers) so an interruption never loses progress.

Run: backend/.venv/Scripts/python.exe scripts/build_lol_match_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.lol_data import cargoquery, parse_cargo_row  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "lol_historical_match_cache.json"
PROGRESS_PATH = DATA_DIR / "lol_scrape_progress.json"

FIELDS = "MS.Team1=Team1,MS.Team2=Team2,MS.Team1Score=Team1Score,MS.Team2Score=Team2Score,MS.Winner=Winner,MS.DateTime_UTC=DateTime_UTC,MS.BestOf=BestOf,MS.OverviewPage=OverviewPage"
PAGE_SIZE = 500
START_DATE = "2023-01-01"  # same ~3-year scope as CS2/Valorant's own historical crawls
PACING_DELAY_SECONDS = 45.0  # flat delay between successful paginated calls, ON TOP OF cargoquery()'s own reactive backoff -- raised from 15s after a real live run showed the budget exhausting within 2-3 requests at the shorter spacing
PAGE_FAILURE_COOLDOWN_SECONDS = 240.0  # real cooldown between SCRIPT-level retries of the same page, after cargoquery()'s own (much shorter) retry budget is exhausted -- see main()'s own docstring note


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=None), encoding="utf-8")


def fetch_page(offset: int) -> list[dict]:
    rows = cargoquery({
        "tables": "MatchSchedule=MS, Tournaments=T",
        "join_on": "MS.OverviewPage=T.OverviewPage",
        "fields": FIELDS,
        "where": f'MS.DateTime_UTC >= "{START_DATE}" AND T.TournamentLevel = "Primary"',
        "order_by": "MS.DateTime_UTC",
        "limit": PAGE_SIZE,
        "offset": offset,
    })
    matches = []
    for row in rows:
        parsed = parse_cargo_row(row)
        if parsed is not None:
            matches.append(parsed)
    return matches


def main():
    progress = load_json(PROGRESS_PATH, {"next_offset": 0, "done": False})
    all_matches: dict[str, dict] = {m["source_match_id"]: m for m in load_json(MATCH_CACHE_PATH, [])}

    if progress["done"]:
        print(f"Already complete: {len(all_matches)} matches cached at {MATCH_CACHE_PATH}")
        return

    offset = progress["next_offset"]
    print(f"Resuming from offset {offset}" if offset else "Starting fresh crawl")

    # REAL finding from a live run (2026-07-19): cargoquery()'s own internal
    # retry budget (a handful of exponential-backoff attempts) genuinely
    # exhausted on page 3 after only 2 successful pages -- this endpoint's
    # rate limit is severe enough that "keep retrying the same call for a
    # while" isn't enough; what actually works is backing off for MINUTES
    # at the script level and retrying the SAME page fresh, same "real
    # cooldown, not a tighter retry loop" fix cargoquery()'s own docstring
    # already applies at a smaller scale. consecutive_failures caps total
    # script-level retries so a genuinely broken query (not just rate
    # limiting) doesn't retry forever.
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 15

    page_num = offset // PAGE_SIZE
    while True:
        page_num += 1
        print(f"[page {page_num}, offset {offset}] querying...", end=" ", flush=True)
        try:
            rows = fetch_page(offset)
        except RuntimeError as e:
            consecutive_failures += 1
            if consecutive_failures > MAX_CONSECUTIVE_FAILURES:
                raise
            print(f"FAILED ({e}) -- cooling down {PAGE_FAILURE_COOLDOWN_SECONDS:.0f}s before retrying this same page "
                  f"(failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
            time.sleep(PAGE_FAILURE_COOLDOWN_SECONDS)
            page_num -= 1
            continue
        consecutive_failures = 0
        print(f"{len(rows)} rows")

        for m in rows:
            all_matches[m["source_match_id"]] = m
        save_json(MATCH_CACHE_PATH, list(all_matches.values()))

        if len(rows) < PAGE_SIZE:
            progress["done"] = True
            progress["next_offset"] = offset + len(rows)
            save_json(PROGRESS_PATH, progress)
            break

        offset += PAGE_SIZE
        progress["next_offset"] = offset
        save_json(PROGRESS_PATH, progress)
        time.sleep(PACING_DELAY_SECONDS)

    print(f"\nDone. {len(all_matches)} total real historical matches cached at {MATCH_CACHE_PATH}")


if __name__ == "__main__":
    main()
