"""Does esports Elo get overconfident when two teams share no common opponents?

THE HYPOTHESIS. Esports pools are regional and barely intersect. If two teams'
ratings were built against DISJOINT opponent sets, the Elo difference between
them is not measuring anything -- yet the model prices it with full confidence.

WHY NOW. The calibration report flagged cs2/series_winner (claimed 0.252,
delivered 0.421) and valorant/series_winner (claimed 0.260, delivered 0.500) as
under-confident at the same end. Connectivity is the one mechanism esports has
that tennis does not.

METHOD, copied from the CFB cross-tier measurement that found a real +100 Elo
correction: walk forward, and BEFORE each match count how many opponents the two
teams have in common from prior matches only. Bucket predictions by that count.
A prediction whose teams share nothing is the one the graph cannot support.

The CFB tell was that bias FLIPPED SIGN with orientation and held magnitude --
that is pool drift, not noise. Same check here: score on the favourite's side so
a systematic direction is visible.
"""
import json, sys, math
from collections import defaultdict
sys.path.insert(0, ".")

BASE, K, SEASON_REG = 1500.0, 24.0, 0.0

def run(path, label):
    d = json.load(open(path, encoding="utf-8"))
    rows = list(d.values()) if isinstance(d, dict) else d
    rows = [r for r in rows if r.get("winner") and r.get("team_a") and r.get("team_b")]
    rows.sort(key=lambda r: (r.get("match_date") or "", r.get("source_match_id") or ""))
    rating = defaultdict(lambda: BASE)
    opps = defaultdict(set)          # team -> opponents faced SO FAR
    buckets = defaultdict(list)      # shared-opponent count -> (pred_fav, won_fav)
    for r in rows:
        a, b = r["team_a"], r["team_b"]
        shared = len(opps[a] & opps[b])
        ra, rb = rating[a], rating[b]
        pa = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
        # score on the FAVOURITE's side so a systematic direction shows up
        if pa >= 0.5:
            pred, won = pa, 1.0 if r["winner"] == "team_a" else 0.0
        else:
            pred, won = 1 - pa, 1.0 if r["winner"] == "team_b" else 0.0
        key = 0 if shared == 0 else (1 if shared <= 2 else (2 if shared <= 5 else 3))
        buckets[key].append((pred, won))
        # update
        sa = 1.0 if r["winner"] == "team_a" else 0.0
        rating[a] = ra + K * (sa - pa)
        rating[b] = rb + K * ((1 - sa) - (1 - pa))
        opps[a].add(b); opps[b].add(a)

    names = {0: "0 shared", 1: "1-2 shared", 2: "3-5 shared", 3: "6+ shared"}
    print(f"\n=== {label}  ({len(rows)} matches with a winner) ===")
    print(f"{'shared opponents':18}{'n':>7}{'claimed':>10}{'actual':>9}{'gap':>9}{'overstate':>11}")
    print('-' * 64)
    for k in sorted(buckets):
        v = buckets[k]
        if len(v) < 40: 
            print(f"{names[k]:18}{len(v):>7}   too few"); continue
        c = sum(p for p, _ in v) / len(v)
        a_ = sum(y for _, y in v) / len(v)
        print(f"{names[k]:18}{len(v):>7}{c:>10.3f}{a_:>9.3f}{c-a_:>+9.3f}{c/a_ if a_ else 0:>10.2f}x")

    # CONFIDENT BAND ONLY -- where the staking actually happens
    print(f"  confident band (claimed >= 0.70):")
    for k in sorted(buckets):
        v = [x for x in buckets[k] if x[0] >= 0.70]
        if len(v) < 30: continue
        c = sum(p for p, _ in v) / len(v)
        a_ = sum(y for _, y in v) / len(v)
        print(f"     {names[k]:16}n={len(v):5d}  claimed {c:.3f}  actual {a_:.3f}  {c-a_:+.3f}")

import os
for p, l in (("../data/cs2_historical_match_cache.json", "CS2"),
             ("../data/valorant_historical_match_cache.json", "VALORANT"),
             ("../data/lol_historical_match_cache.json", "LOL")):
    if os.path.exists(p):
        run(p, l)
    else:
        print(f"{l}: no cache -- skipped")
