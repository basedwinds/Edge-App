"""When does each racing series' board actually fill up, relative to the race?

The question (user, 2026-08-06): racing lines may not all be listed at the same
time during a race weekend, so a line that appears late could be starved of
stake by lines that merely appeared FIRST -- the pool is finite and committed as
markets show up.

METHOD. Market.created_at is when this app first persisted the market, joined to
RaceEvent.start_time. Deliberately NOT the earliest MarketSnapshot: pruning
deletes every non-latest snapshot older than the retention window
(snapshot_maintenance.prune_market_snapshots), so for anything older than that
the earliest surviving snapshot is an artefact of the prune, not a first
sighting. created_at is never pruned.

CAVEAT stated up front: created_at is when WE saw it, which is bounded by the
poller's own cadence and by how long this app has been running. It cannot see a
market that Kalshi listed before the app started tracking that series. Races
whose markets predate the series' own ingestion going live will look
"instantly listed" and are excluded by the --min-lead filter.
"""
import datetime
import statistics
import sys
from collections import defaultdict

from app.db.database import SessionLocal
from app.db.models import Market, RaceEvent

s = SessionLocal()
events = {e.id: e for e in s.query(RaceEvent).all() if e.start_time}
rows = [
    m for m in s.query(Market).filter(Market.sport.in_(("f1", "nascar", "irl"))).all()
    if m.race_event_id in events and m.created_at
]
print(f"racing markets joined to a dated race event: {len(rows)}")
if not rows:
    raise SystemExit("no rows -- nothing to measure")

# hours before the race that we first saw each market
def lead_h(m):
    return (events[m.race_event_id].start_time - m.created_at).total_seconds() / 3600.0


# GROUP BY THE REAL RACE, not by RaceEvent row. One race has SEVERAL RaceEvent
# rows -- "Hungarian Grand Prix: Driver Winner", ": Head-to-Head", ": Driver
# Podium Finish" are all the same Sunday. The first version of this script
# grouped per RaceEvent and every spread came out 0.0h, because within one
# Kalshi event all outcomes are listed at the same instant. That measured
# nothing: the question is whether the market TYPES arrive together, which only
# shows up once the event rows for one race are pooled. Keyed on
# (series, start_time) since every event for a race shares its start.
by_series_type = defaultdict(list)
by_race = defaultdict(list)
for m in rows:
    ev = events[m.race_event_id]
    by_series_type[(ev.series, m.market_type)].append(lead_h(m))
    by_race[(ev.series, ev.start_time)].append((lead_h(m), m.market_type, ev.name))


def fmt(vals):
    v = sorted(vals)
    return (f"n={len(v):4}  first seen (h before race)  "
            f"earliest={v[-1]:7.1f}  median={statistics.median(v):7.1f}  latest={v[0]:7.1f}")


print("\n=== WHEN EACH MARKET TYPE FIRST APPEARS, per series ===")
print("(positive = hours BEFORE the race; negative = we only saw it after the race started)")
for series in ("f1", "nascar", "irl"):
    keys = sorted(k for k in by_series_type if k[0] == series)
    if not keys:
        continue
    print(f"\n  {series.upper()}")
    for k in keys:
        print(f"    {k[1]:16} {fmt(by_series_type[k])}")

print("\n=== DOES THE BOARD FILL AT ONCE, OR IN WAVES? ===")
print("Per race: the spread between the FIRST and LAST market to appear.")
print("A large spread is the user's concern -- late lines competing for a pool")
print("that earlier lines have already drawn from.\n")
print(f"  {'series':7} {'race start':17} {'mkts':>5} {'types':>6} {'first(h)':>9} {'last(h)':>8} {'spread(h)':>10}")
spreads = defaultdict(list)
detail = {}
for (series, start), entries in sorted(by_race.items(), key=lambda kv: (kv[0][0], kv[0][1])):
    leads = [e[0] for e in entries]
    if len(set(e[1] for e in entries)) < 2:
        continue  # a race with only one market type says nothing about staggering
    first, last = max(leads), min(leads)
    spreads[series].append(first - last)
    detail[(series, start)] = entries
    print(f"  {series:7} {str(start)[:16]:17} {len(leads):5} {len(set(e[1] for e in entries)):6} "
          f"{first:9.1f} {last:8.1f} {first - last:10.1f}")

print("\n  ORDER OF ARRIVAL for the most staggered race in each series:")
for series in ("f1", "nascar", "irl"):
    cands = [(k, v) for k, v in detail.items() if k[0] == series]
    if not cands:
        continue
    key, entries = max(cands, key=lambda kv: max(e[0] for e in kv[1]) - min(e[0] for e in kv[1]))
    print(f"\n    {series.upper()} race starting {key[1]}")
    seen = {}
    for lead, mtype, _name in entries:
        seen[mtype] = max(seen.get(mtype, -1e9), lead)
    for mtype, lead in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"      {lead:8.1f}h before race   {mtype}")

print("\n=== SUMMARY: how staggered is each series? ===")
for series, vals in sorted(spreads.items()):
    v = sorted(vals)
    print(f"  {series:7} races={len(v):3}  spread between first and last listing: "
          f"median={statistics.median(v):6.1f}h  max={v[-1]:6.1f}h")

print("\n=== HOW MUCH OF THE BOARD EXISTS AT A GIVEN LEAD TIME? ===")
print("Share of a race's eventual markets already listed N hours before the race.")
print("This is what a 'wait until the board is full' rule would key off.\n")
checkpoints = (168, 96, 72, 48, 24, 12, 6, 3, 1, 0)
print(f"  {'series':7} " + " ".join(f"{c:>5}h" for c in checkpoints))
for series in ("f1", "nascar", "irl"):
    evs = [(k, [e[0] for e in v]) for k, v in by_race.items()
           if k[0] == series and len(set(x[1] for x in v)) >= 2]
    if not evs:
        continue
    cells = []
    for c in checkpoints:
        shares = [sum(1 for x in leads if x >= c) / len(leads) for _k, leads in evs]
        cells.append(f"{100 * statistics.mean(shares):5.0f}%")
    print(f"  {series:7} " + " ".join(cells))
