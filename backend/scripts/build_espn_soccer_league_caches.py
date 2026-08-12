"""Crawl ESPN match history for leagues football-data.co.uk does not cover.

WHY THESE LEAGUES. Polymarket listed title futures for ~50 soccer leagues on
2026-08-12. Judged on football-data coverage alone the answer looked like "we
can only do 9 of them"; probing ESPN -- the client this app already had -- found
real, deep history for fifteen more. Completed matches 2022-2025:

    Colombia col.1     1,787    Ecuador ecu.1      1,038
    USL usa.usl.1      1,693    Costa Rica crc.1   1,036
    Uruguay uru.1      1,195    Venezuela ven.1      961
    Romania rou.1      1,184    Saudi ksa.1          952
    Guatemala gua.1    1,039    South Africa rsa.1   906
    Austria aut.1        774    Switzerland sui.1    768
    A-League aus.1       700    Ireland irl.1        697
    NWSL usa.nwsl        652

That is comparable to leagues already modelled, so these are a coverage gap
rather than a data-availability one.

THE SEASON LABEL IS DERIVED, NOT ASSUMED, and that is the whole reason this
script is more than a loop. `season` is NOT cosmetic: elo_soccer's
start_season_if_new() fires SEASON_REGRESSION (1/3 of the way back to league
average) every time the string changes. Label a split-year European league by
calendar year and every club gets regressed in the middle of every season;
label a calendar-year South American league as split-year and it never regresses
at all. Both are silent -- ratings just quietly get worse.

So each league's season shape is measured from its OWN match-month histogram:
the quietest month is the off-season trough, the season starts the month after,
and a trough in December/January means calendar-year labelling while a trough in
June/July means Aug-May labelling. Same discipline as deriving the Liga MX
Liguilla format from a games-per-pairing histogram rather than from memory.

Run: backend/.venv/Scripts/python.exe scripts/build_espn_soccer_league_caches.py
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.clients import espn_soccer_client as espn  # noqa: E402
from app.ingestion.soccer_data import DATA_DIR  # noqa: E402

# league code -> ESPN slug. Codes follow the app's football-data-style
# convention so they join straight onto SoccerMatch.league.
ESPN_ONLY_LEAGUES = {
    "COL1": "col.1",       # Colombia Primera A
    "USL1": "usa.usl.1",   # USL Championship
    "URU1": "uru.1",       # Uruguayan Primera Division
    "ROU1": "rou.1",       # Romania SuperLiga
    "GUA1": "gua.1",       # Guatemala Liga Nacional
    "ECU1": "ecu.1",       # Ecuador LigaPro Serie A
    "CRC1": "crc.1",       # Costa Rica Liga FPD
    "VEN1": "ven.1",       # Venezuelan Primera Division
    "KSA1": "ksa.1",       # Saudi Professional League
    "RSA1": "rsa.1",       # South Africa Premiership
    "AUT1": "aut.1",       # Austrian Bundesliga
    "SUI1": "sui.1",       # Swiss Super League
    "AUS1": "aus.1",       # A-League
    "IRL1": "irl.1",       # League of Ireland Premier Division
    "NWSL": "usa.nwsl",    # NWSL
}

START_YEAR = 2019


def cache_path(code: str) -> Path:
    return DATA_DIR / f"espn_soccer_{code.lower()}_matches_cache.json"


def derive_season_shape(dates: list[str]) -> tuple[int, bool]:
    """(season_start_month, is_split_year) from the league's own match months.

    The quietest calendar month is the off-season; the season starts the month
    after it. A season starting in January IS the calendar year; anything else
    spans two, which is what the "YYYY-YY" label exists for.
    """
    months = collections.Counter(int(d[5:7]) for d in dates if len(d) >= 7)
    if not months:
        return 1, False
    quietest = min(range(1, 13), key=lambda m: months.get(m, 0))
    start_month = quietest % 12 + 1
    return start_month, start_month != 1


def make_labeller(start_month: int, split: bool):
    def label(match_date: str) -> str:
        year, month = int(match_date[:4]), int(match_date[5:7])
        if not split:
            return str(year)
        season_start = year if month >= start_month else year - 1
        return f"{season_start}-{str(season_start + 1)[-2:]}"
    return label


def main() -> None:
    today = dt.date.today()
    start = dt.date(START_YEAR, 1, 1)
    grand_total = 0
    for code, slug in ESPN_ONLY_LEAGUES.items():
        path = cache_path(code)
        print(f"\n=== {code} ({slug}) ===", flush=True)
        raw = espn.fetch_season_range_for(slug, start, today)
        # Pass 1: dates only, to derive the season shape before labelling.
        provisional = espn.parse_final_events_for(raw, code, lambda d: "")
        if not provisional:
            print("  NO completed matches returned -- skipped, nothing cached")
            continue
        start_month, split = derive_season_shape([m["match_date"] for m in provisional])
        shape = f"Aug-May style, season starts month {start_month}" if split else "calendar year"
        print(f"  season shape DERIVED: {shape}")
        matches = espn.parse_final_events_for(raw, code, make_labeller(start_month, split))
        seen, deduped = set(), []
        for m in matches:
            if m["source_match_id"] in seen:
                continue
            seen.add(m["source_match_id"])
            deduped.append(m)
        deduped.sort(key=lambda m: m["match_date"])
        by_season = collections.Counter(m["season"] for m in deduped)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(deduped), encoding="utf-8")
        teams = {t for m in deduped for t in (m["home_team"], m["away_team"])}
        print(f"  {len(deduped)} matches, {len(teams)} teams, "
              f"{deduped[0]['match_date']} -> {deduped[-1]['match_date']}")
        print(f"  seasons: {', '.join(f'{s}:{n}' for s, n in sorted(by_season.items()))}")
        grand_total += len(deduped)
    print(f"\nTOTAL cached: {grand_total} matches across {len(ESPN_ONLY_LEAGUES)} leagues")


if __name__ == "__main__":
    main()
