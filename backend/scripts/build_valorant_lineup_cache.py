"""One-off cache builder for Valorant's real PER-MATCH player lineups, for
the player-level rating build (2026-07-21).

Unlike CS2 -- where lineups are approximated from per-EVENT participant
rosters (see build_cs2_event_roster_cache.py) -- vlr.gg publishes the exact
5 players who actually played each individual match, so this is real
per-match ground truth, not an approximation. That data-QUALITY difference
is the reason for doing Valorant second: CS2's own numbers showed extra
lineup COVERAGE hit sharp diminishing returns (+30% training data bought
only -0.0006 Brier), which points at quality, not quantity, as the
remaining lever.

SCOPED to matches since CUTOFF (12 months) rather than all 19,644 real
matches -- a deliberate cost/benefit call, not laziness: Kalshi's own
Valorant markets only start 2026-05-14 (see backtest_valorant_market_odds.py),
so a 12-month window still leaves ~10 months of pure training history before
the first match this app can actually price against a real closing line,
while cutting the crawl from ~6.4h to ~3.6h.

Structure parsed (confirmed live 2026-07-21 on real matches): vlr.gg's
scoreboard is DIV-based, not table-based -- `.ovw-row` per player, with
`.ovw-player-name` and `.ovw-player-tag` (the team's short tag, e.g. "TL").
Grouping by that tag reliably splits the 10 players into the 2 real lineups.
Both the header team NAMES and the lineups are stored, so the consumer can
join on team name rather than trusting vlr.gg's row order to match this
app's own team_a/team_b ordering.

Run: backend/.venv/Scripts/python.exe scripts/build_valorant_lineup_cache.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "valorant_historical_match_cache.json"
OUTPUT_PATH = DATA_DIR / "valorant_match_lineups_cache.json"

CUTOFF = "2025-07-01"
REQUEST_DELAY_SECONDS = 1.5
LINEUP_SIZE = 5

_client = httpx.Client(
    timeout=30.0,
    follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
)


def parse_match(html: str) -> dict | None:
    """{"teams": [name_a, name_b], "lineups": [[...5], [...5]]} or None when
    the page has no real scoreboard (forfeits, un-played or rescheduled
    matches, and vlr ids that simply aren't a match page)."""
    soup = BeautifulSoup(html, "html.parser")
    teams = [t.get_text(strip=True) for t in soup.select(".match-header-link-name .wf-title-med")]

    seen: list[tuple[str, str]] = []
    for row in soup.select(".ovw-row"):
        nm = row.select_one(".ovw-player-name")
        tg = row.select_one(".ovw-player-tag")
        if nm is None or tg is None:
            continue
        pair = (nm.get_text(strip=True), tg.get_text(strip=True))
        if pair and pair[0] and pair not in seen:
            seen.append(pair)  # a real page repeats each player per map -- dedupe, keep first-seen order

    tags: list[str] = []
    for _, tag in seen:
        if tag not in tags:
            tags.append(tag)
    if len(tags) != 2 or len(teams) != 2:
        return None

    lineups = [[n for n, t in seen if t == tags[i]] for i in range(2)]
    if any(len(lu) != LINEUP_SIZE for lu in lineups):
        return None  # off-5 scoreboard -> treat as unknown rather than guess
    return {"teams": teams, "lineups": lineups}


def main():
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("winner") and r.get("match_date", "") >= CUTOFF]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"], reverse=True)

    cache = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else {}
    todo = [r for r in rows if str(r["source_match_id"]) not in cache]
    print(f"{len(rows)} matches since {CUTOFF}; {len(cache)} already cached; {len(todo)} to fetch", flush=True)

    ok = 0
    for i, r in enumerate(todo):
        mid = str(r["source_match_id"])
        try:
            resp = _client.get(f"https://www.vlr.gg/{mid}")
            parsed = parse_match(resp.text) if resp.status_code == 200 else None
        except httpx.HTTPError:
            parsed = None  # transient: leave uncached so a later run retries it
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        cache[mid] = parsed
        if parsed:
            ok += 1
        if (i + 1) % 100 == 0:
            OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
            print(f"  [{i + 1}/{len(todo)}] {ok} real lineups parsed so far", flush=True)
        time.sleep(REQUEST_DELAY_SECONDS)

    OUTPUT_PATH.write_text(json.dumps(cache), encoding="utf-8")
    good = sum(1 for v in cache.values() if v)
    print(f"\nDone. {len(cache)} matches probed, {good} with real 5v5 lineups -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
