"""One-off racing results scraper from ESPN's core API (free) for the shared
racing engine (core-5 expansion: NASCAR + IndyCar, plus the fresh in-app F1).

Usage: python build_racing_cache.py <league>   where league in {f1, irl, nascar}
  f1     -> Formula 1
  irl    -> IndyCar
  nascar -> NASCAR Cup (ESPN slug 'nascar-premier')

For each season the scoreboard lists events; for each event the core API event
embeds its 5 competitions (practice/qualifying/race). We take the type-3
(Race) competition, whose competitors carry `order` (true finishing position)
and `winner`. Athlete id->name is resolved once and cached (only a few dozen
drivers per series). Stores race start time (core event `date`) for the
closing-price cutoff in the market backtest.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

SLUG = {"f1": "f1", "irl": "irl", "nascar": "nascar-premier"}
UA = {"User-Agent": "Mozilla/5.0"}
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SEASONS = range(2021, 2027)

_c = httpx.Client(timeout=30.0, headers=UA)
_names = {}  # athlete id -> display name (cached to disk per league)


def get(url, params=None, tries=5):
    for a in range(tries):
        try:
            r = _c.get(url, params=params)
            if r.status_code == 429:
                time.sleep(2 * (a + 1)); continue
            if r.status_code != 200:
                return None
            return r.json()
        except httpx.HTTPError:
            time.sleep(1 + a)
    return None


def driver_name(league, aid, ref):
    if aid in _names:
        return _names[aid]
    d = get(ref)
    nm = (d or {}).get("displayName") or (d or {}).get("fullName") or aid
    _names[aid] = nm
    return nm


def scrape(league):
    slug = SLUG[league]
    site = f"https://site.api.espn.com/apis/site/v2/sports/racing/{slug}"
    core = f"https://sports.core.api.espn.com/v2/sports/racing/leagues/{slug}"
    out_path = DATA_DIR / f"racing_{league}.json"
    names_path = DATA_DIR / f"racing_{league}_names.json"
    if names_path.exists():
        _names.update(json.loads(names_path.read_text(encoding="utf-8")))
    cache = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}

    for season in SEASONS:
        d = get(f"{site}/scoreboard", {"dates": season})
        if not d:
            continue
        events = d.get("events", [])
        got = 0
        for ev in events:
            eid = ev.get("id")
            if not eid:
                continue
            # Re-fetch a cached race if it predates the 2026-07-23 grid/constructor
            # enrichment (no start_order yet); skip only already-enriched ones.
            cached = cache.get(eid)
            if cached and cached.get("results") and cached["results"][0].get("start_order") is not None:
                continue
            ce = get(f"{core}/events/{eid}")
            if not ce:
                continue
            # F1 splits a weekend into 5 competitions (type 3 = Race); IndyCar &
            # NASCAR expose a single race competition with no type. Pick the
            # type-3 race if present, else the competition that actually has a
            # winner flag (the race), preferring the largest field.
            comps = ce.get("competitions", [])
            typed = [cp for cp in comps if str(cp.get("type", {}).get("id")) == "3"]
            if typed:
                race = typed[0]
            else:
                withwin = [cp for cp in comps if any(x.get("winner") for x in cp.get("competitors", []))]
                race = max(withwin, key=lambda cp: len(cp.get("competitors", []))) if withwin else None
            if race is None:
                continue
            # a finished race is proven by a winner flag among the competitors.
            if not any(comp.get("winner") for comp in race.get("competitors", [])):
                continue
            results = []
            for comp in race.get("competitors", []):
                aid = comp.get("id")
                ref = (comp.get("athlete") or {}).get("$ref")
                if not aid or not ref:
                    continue
                # 2026-07-23 enrichment: the race competition already carries the
                # two biggest racing predictors, we just never pulled them --
                # startOrder = STARTING GRID position (pole = 1; enormously
                # predictive, esp. F1/IndyCar) and vehicle.manufacturer =
                # CONSTRUCTOR/car (F1 is ~70% car). Both inline per competitor.
                vehicle = comp.get("vehicle") or {}
                results.append({
                    "driver_id": aid,
                    "driver": driver_name(league, aid, ref),
                    "order": comp.get("order"),
                    "start_order": comp.get("startOrder"),
                    "constructor": vehicle.get("manufacturer"),
                    "winner": bool(comp.get("winner")),
                })
            results = [r for r in results if r["order"] is not None]
            if len(results) < 5:
                continue
            cache[eid] = {
                "id": eid, "season": season,
                "date": race.get("date") or ce.get("date"), "name": ce.get("name"),
                "results": sorted(results, key=lambda r: r["order"]),
            }
            got += 1
            time.sleep(0.1)
        out_path.write_text(json.dumps(cache), encoding="utf-8")
        names_path.write_text(json.dumps(_names), encoding="utf-8")
        print(f"{league} {season}: {got} races (cache {len(cache)})", flush=True)
    print(f"\nDone. {len(cache)} {league} races -> {out_path}")


if __name__ == "__main__":
    lg = sys.argv[1] if len(sys.argv) > 1 else "f1"
    scrape(lg)
