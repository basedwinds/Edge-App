"""Score models/two_leg_tie.prob_advance against real CONMEBOL knockout results.

WHY ESPN'S OWN WINNER, NOT THE SCORELINES. A tie level on aggregate is settled
by extra time and penalties, and a scoreboard row shows neither -- reconstructing
the winner from goals alone would silently mislabel every shootout, which is
18.7% of CONMEBOL ties. ESPN publishes it directly on the SECOND leg:
competitions[0].series.competitors[].winner, alongside aggregateScore.

THE LABEL IS ON LEG 2 ONLY, and reading it off leg 1 is the trap this script
hit first: leg 1's series block exists but has completed=false and winner=false
for BOTH sides, so the first run scored 131 ties with an actual advance rate of
0.000 in every bucket. A metric that reads 0% everywhere is a wrong key, not a
finding.

RESULT, 2026-08-18 -- 131 ties (both clubs in a rated pool, legs confirmed
reversed, ESPN winner present):

    predicted    n    actual     gap
        0.095   48     0.188   +0.092
        0.304   19     0.158   -0.146
        0.500   29     0.552   +0.052
        0.702   18     0.556   -0.147
        0.905   17     0.882   -0.023

    overall 0.404 predicted vs 0.405 actual
    Brier 0.1856 vs 0.2409 for always predicting the base rate (-23%)

Centred almost exactly, and it beats the base rate by a clear margin, which is
the claim that matters: the model is not merely averaging correctly, it is
sorting ties. The per-bucket wobble is real but the buckets hold 17-29 ties, so
their standard errors are ~0.10-0.12 and the two inversions sit inside that.

WHAT THIS IS NOT. The ratings are CURRENT, so a tie played in 2022 is scored
with what the clubs became -- the same look-ahead the UEFA strength fit
documents. This is CALIBRATION evidence against outcomes, NOT an out-of-sample
backtest and NOT a claim about beating a market price.
"""
from __future__ import annotations

import collections
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingestion.market_matcher_soccer import canonical_team_key  # noqa: E402
from app.models.baseline import elo_service_soccer  # noqa: E402
from app.models.conmebol_match import home_advantage, load_strength  # noqa: E402
from app.models.two_leg_tie import prob_advance  # noqa: E402

TIES = Path(r"C:\Users\awaws\AppData\Local\Temp\conmebol_ties.json")
SA = ["BRA1", "ARG1", "COL1", "ECU1", "URU1", "VEN1"]


def main() -> int:
    if not TIES.exists():
        print(f"no tie cache at {TIES} -- re-run the knockout fetch first")
        return 1
    raw = json.loads(TIES.read_text(encoding="utf-8"))
    elo_service_soccer.refresh_ratings()
    pools = elo_service_soccer._cache["states_by_league"]
    member = {lg: set(pools[lg].attack_log) for lg in SA}
    mu, offsets = load_strength()
    hfa = home_advantage()
    if mu is None:
        print("no CONMEBOL strength file -- nothing to score")
        return 1

    def resolve(name):
        k = canonical_team_key(name)
        hits = [lg for lg in SA if k in member[lg]]
        return (k, hits[0]) if len(hits) == 1 else None

    legs = collections.defaultdict(dict)
    for r in raw:
        legs[(r["slug"], r["round"], frozenset((r["home"], r["away"])))][r["leg"]] = r

    rows = []
    for v in legs.values():
        if 1 not in v or 2 not in v or not v[2].get("series_completed"):
            continue
        l1, l2 = v[1], v[2]
        if l1["hg"] is None or l2["hg"] is None:
            continue
        a_name, b_name = l1["home"], l1["away"]
        # ASSERT the legs really are reversed before trusting position -- the
        # ordered-tuple error this project has shipped twice.
        if not (l2["home"] == b_name and l2["away"] == a_name):
            continue
        ra, rb = resolve(a_name), resolve(b_name)
        if not ra or not rb:
            continue
        (ak, al), (bk, bl) = ra, rb
        won_a = l2.get("away_won_tie")      # A is the AWAY side of leg 2
        if won_a is None:
            continue
        gap = offsets[bl] - offsets[al]
        lam_b = mu * math.exp(pools[bl].attack_log[bk] + pools[al].concede_log[ak] + hfa + gap)
        lam_a = mu * math.exp(pools[al].attack_log[ak] + pools[bl].concede_log[bk] - gap)
        rows.append((prob_advance(lam_a, lam_b, lead=l1["hg"] - l1["ag"]), bool(won_a)))

    if not rows:
        print("no scorable ties")
        return 1
    print(f"{len(rows)} ties scored\n")
    print(f"{'predicted':>10} {'n':>5} {'actual':>8} {'gap':>8}")
    for lo, hi in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)):
        sel = [r for r in rows if lo <= r[0] < hi]
        if not sel:
            continue
        pr = sum(r[0] for r in sel) / len(sel)
        ac = sum(r[1] for r in sel) / len(sel)
        print(f"{pr:>10.3f} {len(sel):>5} {ac:>8.3f} {ac - pr:>+8.3f}")
    pr = sum(r[0] for r in rows) / len(rows)
    ac = sum(r[1] for r in rows) / len(rows)
    brier = sum((r[0] - r[1]) ** 2 for r in rows) / len(rows)
    base = sum((ac - r[1]) ** 2 for r in rows) / len(rows)
    print(f"\noverall {pr:.3f} predicted vs {ac:.3f} actual")
    print(f"Brier {brier:.4f} vs {base:.4f} base-rate  ({100*(brier-base)/base:+.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
