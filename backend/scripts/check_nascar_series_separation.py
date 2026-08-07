"""Can a NASCAR race's SERIES be identified from its entrant list alone?

THE PROBLEM. Kalshi files NASCAR Cup, Xfinity ("O'Reilly Auto Parts") and Truck
under ONE series ticker, KXNASCARRACE, with no field saying which is which. Our
ratings were Cup-only, so MIN_FIELD_COVERAGE correctly refused to price the
lower series -- ~148 markets sit dead every Xfinity weekend. To price them we
must first know which rating pool a race belongs to.

WHY NOT THE OBVIOUS SIGNALS, both checked live on 2026-08-07 and rejected:

  * TITLE PARSING. Works sometimes and fails silently. Cup races say "NASCAR Cup
    Series ..." and Xfinity says "NASCAR O'Reilly Auto Parts Series ...", but
    "NASCAR Pennzoil 250 presented by Take 5 Oil Change" (Xfinity) and "NASCAR
    TSport 200 presented by Warn Industries" (Truck) carry no series name at
    all. Misrouting a race to the wrong pool is the worst outcome here, so a
    signal that is absent on some races is not usable as the primary one.

  * ESPN CALENDAR NAME-MATCHING. ESPN names races by VENUE ("NASCAR Cup Series
    at Iowa"); Kalshi names them by SPONSOR ("Iowa Corn 350"). Worse, Cup and
    Xfinity both race at Iowa on ADJACENT DAYS, so the venue token cannot
    separate them, and Kalshi's own date is unreliable -- it had the HyVee
    Perks 250 stored as Aug 23 when the race is Aug 8.

THE PROPOSAL is to score the entrant list against all three rating pools and
route to the best one above MIN_FIELD_COVERAGE -- i.e. run the gate that already
exists three times instead of once. It is immune to sponsor names, missing
series labels and wrong dates, degrades safely (no pool clears the floor -> the
race stays unpriced, today's behaviour), and extends free to any new series.

THIS SCRIPT IS THE HONEST TEST OF THAT. A first pass during scoping showed the
right pool explaining 100% of a field against <=19% for the wrong ones, but that
100% was CIRCULAR -- the pools were built from the very races being scored. Only
the cross-pool number was meaningful. Here the pools are built from races before
a cutoff and scored ONLY on races after it, so a driver's presence in a pool is
never evidence taken from the race being classified.

What matters is not that the correct pool scores high, but the MARGIN between it
and the best wrong pool, and whether that margin ever collapses -- NASCAR
regulars routinely moonlight in the lower series, which is exactly the mechanism
that could blur the boundary.

Run:  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/check_nascar_series_separation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SERIES = {
    "cup": "racing_nascar.json",
    "xfinity": "racing_nascar_xfinity.json",
    "truck": "racing_nascar_truck.json",
}
# Races on/after this date are HELD OUT: they are scored, never trained on.
CUTOFF = "2025-01-01"
# The floor racing_markets.py already applies before it trusts a field.
MIN_FIELD_COVERAGE = 0.80


def load(series: str) -> list[dict]:
    path = DATA_DIR / SERIES[series]
    if not path.exists():
        return []
    races = list(json.loads(path.read_text(encoding="utf-8")).values())
    races.sort(key=lambda r: (r.get("date") or "", r["id"]))
    return races


def driver_ids(race: dict) -> set[str]:
    return {r["driver_id"] for r in race.get("results", []) if r.get("driver_id")}


def run() -> None:
    races = {s: load(s) for s in SERIES}
    for s, rs in races.items():
        if not rs:
            print(f"MISSING CACHE for {s} ({SERIES[s]}) -- run build_racing_cache.py {s}")
            return
        print(f"{s:8s} {len(rs):4d} races  {rs[0].get('date','?')[:10]} .. {rs[-1].get('date','?')[:10]}")

    # Pools from TRAIN races only.
    pools = {s: set().union(*[driver_ids(r) for r in rs if (r.get("date") or "") < CUTOFF] or [set()])
             for s, rs in races.items()}
    print(f"\ntrain pools (races before {CUTOFF}): " +
          "  ".join(f"{s}={len(p)}" for s, p in pools.items()))

    rows = []
    for actual, rs in races.items():
        for race in rs:
            if (race.get("date") or "") < CUTOFF:
                continue
            field = driver_ids(race)
            if len(field) < 5:
                continue
            cov = {s: len(field & p) / len(field) for s, p in pools.items()}
            ranked = sorted(cov.items(), key=lambda kv: -kv[1])
            rows.append({
                "actual": actual, "date": (race.get("date") or "")[:10],
                "name": race.get("name"), "n": len(field),
                "cov": cov, "picked": ranked[0][0],
                "top": ranked[0][1], "runner_up": ranked[1][1],
            })

    if not rows:
        print(f"\nNo held-out races after {CUTOFF}.")
        return

    print(f"\nheld-out races scored: {len(rows)}\n")
    print(f"{'actual':9s}{'races':>6}{'routed ok':>11}{'own':>8}{'best wrong':>12}"
          f"{'margin':>8}{'clears 80%':>12}   verdict")
    for actual in SERIES:
        sub = [r for r in rows if r["actual"] == actual]
        if not sub:
            continue
        right = sum(1 for r in sub if r["picked"] == actual)
        own = sum(r["cov"][actual] for r in sub) / len(sub)
        wrong = sum(max(v for s, v in r["cov"].items() if s != actual) for r in sub) / len(sub)
        clears = sum(1 for r in sub if r["cov"][actual] >= MIN_FIELD_COVERAGE)
        verdict = "SEPARATES" if right == len(sub) else f"MISROUTES {len(sub) - right}"
        print(f"{actual:9s}{len(sub):>6}{f'{right}/{len(sub)}':>11}{own:>8.1%}{wrong:>12.1%}"
              f"{own - wrong:>8.1%}{f'{clears}/{len(sub)}':>12}   {verdict}")

    # The decision-relevant tail: the races where the margin is thinnest.
    print("\nTIGHTEST 8 (smallest gap between the right pool and the best wrong one):")
    for r in sorted(rows, key=lambda r: r["cov"][r["actual"]] - max(v for s, v in r["cov"].items() if s != r["actual"]))[:8]:
        wrong_s, wrong_v = max(((s, v) for s, v in r["cov"].items() if s != r["actual"]), key=lambda kv: kv[1])
        flag = "  <-- MISROUTED" if r["picked"] != r["actual"] else ""
        print(f"   {r['date']}  {str(r['name'])[:38]:40s} n={r['n']:3d} "
              f"{r['actual']}={r['cov'][r['actual']]:.0%} vs {wrong_s}={wrong_v:.0%}{flag}")

    misrouted = [r for r in rows if r["picked"] != r["actual"]]
    below = [r for r in rows if r["cov"][r["actual"]] < MIN_FIELD_COVERAGE]
    print(f"\nmisrouted: {len(misrouted)}/{len(rows)}")
    print(f"correct pool below the {MIN_FIELD_COVERAGE:.0%} floor (would stay UNPRICED): {len(below)}/{len(rows)}")


if __name__ == "__main__":
    run()
