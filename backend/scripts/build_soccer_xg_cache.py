"""Per-match xG for the five big European leagues, from Understat. Free, no key.

WHY. The soccer model (elo_soccer.py / Dixon-Coles) is fitted on GOALS SCORED.
Goals are a small-sample, high-variance realisation of chance creation: a team
that generates 2.1 xG and scores 4 has not become a better team. xG is the
standard, better-documented predictor of FUTURE goals -- the same relationship
that made K-BB% beat ERA for MLB starters, measured 2026-08-14. If it holds
here it is worth far more, because it touches every soccer market rather than
one sport's moneyline.

This script ONLY builds the dataset. It changes no model and ships no constant.
Whether xG actually beats goals out-of-sample is check_soccer_xg_signal.py's
job, and it may well come back negative -- air density and the global goal
scale both did.

HOW THE ENDPOINT WORKS, because it is not obvious and cost real time to find.
Understat used to embed its data as JSON.parse('...') inside the league page's
HTML; it does not any more, so scraping the HTML returns an 18KB shell with no
data at any season. The page now fetches from:

    GET https://understat.com/getLeagueData/{league}/{season}

and that URL returns **404 to a plain request**. The difference is a single
header: it only answers when sent `X-Requested-With: XMLHttpRequest`. A
User-Agent alone is not enough, and neither is a Referer. Found by loading the
page in a real browser and reading its network log, which is the only reliable
way to recover an endpoint a site does not document.

NO KEY, NO BROWSER, NO PAID API -- once the header is right, a plain httpx GET
is sufficient, so this stays inside the free-sources-only constraint. It is the
same class of source as the Liquipedia/gol.gg scrapes already in this app, and
the same caution applies: be gentle, cache locally, and do not re-fetch what is
already stored.

COVERAGE, and its honest limit. Understat carries the big five plus the Russian
Premier League: EPL, La Liga, Bundesliga, Serie A, Ligue 1, RFPL. Five of those
map onto leagues this app rates (E0, SP1, D1, I1, F1); RFPL is NOT rated here
and is skipped rather than collected for nothing. That is 5 of 33 rated
leagues -- so xG can never be a whole-model answer, only an upgrade to the five
best-traded leagues. Seasons 2014-2025 are all complete (~360 matches each).

Team names are Understat's own and will NOT match the football-data pool keys
for every club. Resolution is deliberately left to the consumer, because this
app has been bitten repeatedly by name matching (Liverpool vs Liverpool
Montevideo, the hyphen-deleting canonical key, ESPN vs football-data spellings)
and the right join here is fixture-based -- date + both teams -- not
name-similarity.

Run: backend/.venv/Scripts/python.exe scripts/build_soccer_xg_cache.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402

BASE = "https://understat.com"
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "soccer_xg_cache.json"

# Understat league slug -> this app's league code. RFPL is deliberately absent:
# Understat has it, but this app does not rate the Russian Premier League, so
# collecting it would be dead weight.
LEAGUES = {
    "EPL": "E0",
    "La_liga": "SP1",
    "Bundesliga": "D1",
    "Serie_A": "I1",
    "Ligue_1": "F1",
}
SEASONS = list(range(2014, 2026))

# The one header that matters -- see the module docstring. Without it the
# endpoint 404s no matter what else is sent.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def main() -> None:
    cache: dict = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
        print(f"resuming: {sum(len(v) for v in cache.values())} matches already cached")

    total = skipped = 0
    with httpx.Client(timeout=60, follow_redirects=True, headers=HEADERS) as client:
        client.get(f"{BASE}/league/EPL")  # establish a session the way a browser would
        for slug, code in LEAGUES.items():
            cache.setdefault(code, {})
            for season in SEASONS:
                key = str(season)
                if key in cache[code]:
                    skipped += len(cache[code][key])
                    continue
                try:
                    r = client.get(f"{BASE}/getLeagueData/{slug}/{season}",
                                   headers={**HEADERS, "Referer": f"{BASE}/league/{slug}"})
                    r.raise_for_status()
                    payload = r.json()
                except Exception as exc:
                    print(f"   {code} {season}: FAILED ({type(exc).__name__}) -- left uncached")
                    continue
                rows = []
                for m in payload.get("dates", []):
                    # Unplayed fixtures carry no result and no xG. Skipping them
                    # rather than storing nulls keeps the consumer from having to
                    # guess whether a 0.0 means "no shots" or "not played yet".
                    if not m.get("isResult"):
                        continue
                    try:
                        rows.append({
                            "date": (m.get("datetime") or "")[:10],
                            "home": (m.get("h") or {}).get("title"),
                            "away": (m.get("a") or {}).get("title"),
                            "goals_h": int((m.get("goals") or {}).get("h")),
                            "goals_a": int((m.get("goals") or {}).get("a")),
                            "xg_h": float((m.get("xG") or {}).get("h")),
                            "xg_a": float((m.get("xG") or {}).get("a")),
                        })
                    except (TypeError, ValueError):
                        continue  # malformed row -- drop it rather than guess
                cache[code][key] = rows
                total += len(rows)
                print(f"   {code} {season}: {len(rows)} played matches")
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                CACHE_PATH.write_text(json.dumps(cache))
                time.sleep(1.0)  # be a polite guest on a free source

    n = sum(len(v) for lg in cache.values() for v in lg.values())
    print(f"\nfetched {total} new, {skipped} already cached")
    print(f"wrote {CACHE_PATH} -- {n} matches across {len(cache)} leagues "
          f"({CACHE_PATH.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
