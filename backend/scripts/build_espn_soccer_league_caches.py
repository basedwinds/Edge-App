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
    # ---- CONMEBOL completion, 2026-08-18 -----------------------------------
    # These four are added to finish a competition already shipped, not to widen
    # coverage for its own sake. conmebol_match.py prices 117 of 208 live rows
    # (56%) and the gap is almost entirely these countries: of the 16 live
    # Libertadores/Sudamericana ties, SEVEN are blocked solely because Chile,
    # Paraguay, Bolivia or Peru have no rating pool. With them, 16 of 16 price.
    #
    # Found by probing ESPN for every unrated league with live inventory rather
    # than trusting the "no rating pool" note I had written on them -- that note
    # treated a missing pool as a permanent blocker when the pool was buildable
    # from the feed this script already uses. All four return 100 events for
    # both 2026 and 2024, and 100 is ESPN's per-response CAP, so the windowed
    # fetch below is doing real work rather than being defensive.
    "CHI1": "chi.1",       # Chilean Primera Division
    "PAR1": "par.1",       # Paraguayan Primera Division
    "BOL1": "bol.1",       # Bolivian Liga Profesional
    "PER1": "per.1",       # Peruvian Liga 1
    # ---- Second tiers + one new country, 2026-08-18 -------------------------
    # From the same 37-league ESPN probe that produced the CONMEBOL four. Each
    # carries BOTH current events and multi-season history, so one slug gives
    # ratings and settlement.
    #
    # BRA2 is the reason this batch exists: 160 open Kalshi markets with 30,917
    # traded contracts, the largest single coverage gap found in the whole
    # New Markets audit, and it already lists GAME + SPREAD + TOTAL.
    #
    # SECOND-ORDER VALUE, the same argument that justified D2/E2/SP2: domestic
    # cups pair top-flight clubs with lower-tier ones, so a rated second tier
    # raises CUP coverage too. bra.2 feeds the Copa do Brasil, arg.2 the Copa
    # Argentina, eng.5 the FA Cup and EFL Cup rounds where National League sides
    # appear, ned.2 the KNVB Beker.
    "BRA2": "bra.2",       # Brazilian Serie B
    "ARG2": "arg.2",       # Argentine Primera Nacional
    "E4":   "eng.5",       # English National League (5th tier; E0-E3 are 1-4)
    "N2":   "ned.2",       # Dutch Eerste Divisie
    "MYS1": "mys.1",       # Malaysian Super League
}

START_YEAR = 2019


def cache_path(code: str) -> Path:
    return DATA_DIR / f"espn_soccer_{code.lower()}_matches_cache.json"


def _season_counts(dates: list[str], start_month: int) -> list[int]:
    """Matches per season under a given start month, dropping the first and last
    (always partial -- the crawl window cuts them)."""
    seasons: collections.Counter = collections.Counter()
    for d in dates:
        year, month = int(d[:4]), int(d[5:7])
        seasons[year if month >= start_month else year - 1] += 1
    ordered = [seasons[k] for k in sorted(seasons)]
    return ordered[1:-1] if len(ordered) > 2 else ordered


def _uniformity(counts: list[int]) -> float:
    """Coefficient of variation; lower is a better fit. A league plays roughly
    the same number of matches every season, so the CORRECT boundary is the one
    that makes the per-season counts flat."""
    if not counts or len(counts) < 2:
        return float("inf")
    mean = sum(counts) / len(counts)
    if mean <= 0:
        return float("inf")
    var = sum((c - mean) ** 2 for c in counts) / len(counts)
    return (var ** 0.5) / mean


CALENDAR_BOUNDARY_MONTHS = (1, 2)


def derive_season_shape(dates: list[str]) -> tuple[int, bool]:
    """(season_start_month, is_split_year) from the deepest off-season trough.

    The season starts the month AFTER the league's quietest month, so the
    boundary never falls in a busy period -- which is the only failure that
    really costs anything, because a boundary mid-season fires
    SEASON_REGRESSION while the season is still being played.

    THE BUG THIS FIXES was the calendar-vs-split test, not the trough. The
    first version said "start month != January => split year", which labelled
    ALL FIFTEEN leagues Aug-May, Colombia and NWSL included. A trough at the
    YEAR BOUNDARY means the calendar year already IS the season, and such a
    league's quietest month is December or January, so its start month comes
    out as January or February -- hence CALENDAR_BOUNDARY_MONTHS.

    (A uniformity fit over all twelve boundaries was tried in between and was
    worse: every month inside an off-season gap produces the identical grouping,
    so the fit cannot discriminate among them and picked May for Colombia --
    squarely mid-season.)

    Checked against the real calendars, this agrees for 14 of 15 leagues.
    AUSTRIA is the exception and is left as-is deliberately: it has TWO troughs,
    a June off-season and a deeper Dec-Feb winter break, so it resolves to
    calendar-year and will regress at the winter break rather than in summer.
    That is a January transfer window with real squad turnover, so regressing
    there is defensible rather than merely tolerable -- but it is an
    approximation, and it is written down here rather than special-cased.
    """
    months = collections.Counter(int(d[5:7]) for d in dates if len(d) >= 7)
    if not months:
        return 1, False
    quietest = min(range(1, 13), key=lambda m: months.get(m, 0))
    start_month = quietest % 12 + 1
    if start_month in CALENDAR_BOUNDARY_MONTHS:
        return 1, False
    return start_month, True


# THE TROUGH RULE HAS A KNOWN FAILURE MODE, and this is where it is corrected.
#
# derive_season_shape picks the single quietest month. That breaks for a league
# whose real off-season is SPLIT ACROSS THE YEAR BOUNDARY while a mid-season
# break is concentrated in one month -- the deepest single month is then the
# mid-season break, and the season gets labelled to start in the middle of
# itself. The docstring above already records Austria as one such league.
#
# CHILE (2026-08-18) is the second, found on its first crawl. Its histogram:
#
#     1:80  2:209 3:189 4:203 5:198  6:39  7:144 8:234 9:165 10:188 11:137 12:79
#                                     ^^^ isolated winter break, deepest month
#     ^^ the real off-season is Dec(79)+Jan(80) -- TWO months, neither deepest
#
# The trough rule chose a July start. Measured against the alternatives by
# per-season flatness (coefficient of variation, lower is better):
#
#     calendar, Feb boundary   cv=0.107   [267, 303, 256, 224, 240, 227]
#     calendar, Jan boundary   cv=0.179   [225, 354, 240, 240, 240, 223]
#     Aug-May,  Jul boundary   cv=0.221   [147, 321, 314, 238, 241, 220, 243]
#
# Calendar wins decisively, and it matches the real competition, which runs
# February to December. This is an OVERRIDE rather than a heuristic change on
# purpose: a "longest run of quiet months" rule fixes Chile but mislabels
# Austria (whose Dec-Feb winter break is LONGER than its June off-season), so
# changing the rule would trade one wrong league for another across the
# eighteen that currently derive correctly. Overriding one measured league is
# the smaller, checkable change.
SEASON_SHAPE_OVERRIDES = {
    "CHI1": (2, False),   # calendar year, February boundary -- see above
    # MALAYSIA, 2026-08-18. NOT the same failure as Chile: the deriver is not
    # confused here, the league genuinely CHANGED FORMAT mid-history and no
    # single boundary is right for all of it.
    #
    #   2019-2022  Feb/Mar -> Sep/Oct   (a calendar-year season)
    #   2024-      Aug     -> May       (a European-style split season)
    #
    # The derived December boundary is the dangerous one, and it is dangerous
    # for the CURRENT format specifically: the live season ran Aug 2025 -> May
    # 2026 continuously (Aug 23, Sep 12, Oct 12, Nov 12, Dec 19, Jan 21, Feb 13,
    # Mar 13, Apr 13, May 18 matches), so a 1-December boundary splits it in
    # half and fires SEASON_REGRESSION -- a third of every club's rating pulled
    # back to league average -- in the middle of play, every single year.
    #
    # July is the boundary the modern format wants: May, June and July 2025 were
    # all EMPTY, so the break is real and unambiguous. The price is that the
    # 2019-2022 seasons get split instead, costing about four spurious
    # regressions back then. That trade is clearly worth taking: ~338 of the 956
    # cached matches fall after the format change, which is far more than enough
    # for current ratings to have converged past a 2021 artefact, whereas a
    # mid-season regression corrupts the ratings the app prices with TODAY.
    "MYS1": (7, True),
}


def make_labeller(start_month: int, split: bool):
    def label(match_date: str) -> str:
        year, month = int(match_date[:4]), int(match_date[5:7])
        if not split:
            # start_month lets a CALENDAR league still put its January tail with
            # the previous season -- Chile plays Feb-Dec, so a January fixture
            # belongs to the season that just ended, not the one about to start.
            return str(year if month >= start_month else year - 1)
        season_start = year if month >= start_month else year - 1
        return f"{season_start}-{str(season_start + 1)[-2:]}"
    return label


def main() -> None:
    # --only CODE[,CODE...] crawls just those leagues. Added 2026-08-18 because
    # a full pass is ~25 minutes and adding one league should not mean
    # re-fetching nineteen that already work -- which also means a re-crawl
    # after a season-shape fix is cheap enough to actually do.
    only = None
    for i, arg in enumerate(sys.argv):
        if arg == "--only" and i + 1 < len(sys.argv):
            only = {c.strip().upper() for c in sys.argv[i + 1].split(",") if c.strip()}
    today = dt.date.today()
    start = dt.date(START_YEAR, 1, 1)
    grand_total = 0
    for code, slug in ESPN_ONLY_LEAGUES.items():
        if only and code not in only:
            continue
        path = cache_path(code)
        print(f"\n=== {code} ({slug}) ===", flush=True)
        raw = espn.fetch_season_range_for(slug, start, today)
        # Pass 1: dates only, to derive the season shape before labelling.
        provisional = espn.parse_final_events_for(raw, code, lambda d: "")
        if not provisional:
            print("  NO completed matches returned -- skipped, nothing cached")
            continue
        start_month, split = derive_season_shape([m["match_date"] for m in provisional])
        if code in SEASON_SHAPE_OVERRIDES:
            start_month, split = SEASON_SHAPE_OVERRIDES[code]
            print(f"  season shape OVERRIDDEN (see SEASON_SHAPE_OVERRIDES): "
                  f"start month {start_month}, split={split}")
        else:
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
    # Count what this RUN cached, not the size of the registry -- with --only
    # those differ, and the registry number reads like a full crawl happened.
    scope = f"{len(only)} of {len(ESPN_ONLY_LEAGUES)}" if only else str(len(ESPN_ONLY_LEAGUES))
    print("")
    print(f"TOTAL cached: {grand_total} matches across {scope} leagues")


if __name__ == "__main__":
    main()
