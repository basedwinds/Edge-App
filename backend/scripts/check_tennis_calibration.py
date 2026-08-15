"""Is tennis under-confidence a whole-curve steepness problem, or longshot-only?

If favourites are correspondingly OVER-delivered, one steepness fix handles both
ends. If the high end is fine, this is specific to longshots and shrinking the
whole curve would break what currently works.
"""
import sys
from collections import defaultdict
from math import sqrt
sys.path.insert(0, ".")
from app.db.database import SessionLocal
from sqlalchemy import text

def wilson(k, n):
    if not n: return (0.0, 1.0)
    p, z = k / n, 1.96
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    h = z * sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return (max(0.0, c-h), min(1.0, c+h))

def table(label, rows, width=0.1):
    if len(rows) < 60:
        print(f"  {label:28} n={len(rows):5d}  too few"); return
    b = defaultdict(list)
    for p, y in rows:
        b[min(int(p/width), int(1/width)-1)].append((p, y))
    print(f"  {label}  (n={len(rows)})")
    for k in sorted(b):
        v = b[k]
        if len(v) < 25: continue
        c = sum(p for p,_ in v)/len(v)
        w = sum(1 for _,y in v if y)
        lo, hi = wilson(w, len(v))
        sig = "  SIG" if not (lo <= c <= hi) else ""
        print(f"     p={k*width:.1f}-{(k+1)*width:.1f}  n={len(v):5d}  claimed {c:.3f}  "
              f"actual {w/len(v):.3f} [{lo:.3f},{hi:.3f}]  {w/len(v)-c:+.3f}{sig}")

s = SessionLocal()
rows = s.execute(text("""
    SELECT o.model_prob, o.status, t.tour, t.tier, t.surface
    FROM model_observations o
    LEFT JOIN tennis_matches t ON t.id = o.tennis_match_id
    WHERE o.sport='tennis' AND o.market_type='moneyline'
      AND o.status IN ('won','lost') AND o.model_prob IS NOT NULL
""")).fetchall()
s.close()
print(f"tennis moneyline settled observations: {len(rows)}\n")

allr = [(r[0], r[1] == "won") for r in rows]
print("=== FULL CURVE -- is the high end mis-calibrated too? ===")
table("all tennis moneyline", allr)

print("\n=== BY TOUR (a mix effect could manufacture the whole thing) ===")
by = defaultdict(list)
for mp, st, tour, tier, surf in rows:
    by[(tour or "?")].append((mp, st == "won"))
for k in sorted(by, key=lambda k: -len(by[k])):
    table(f"tour={k}", by[k])

print("\n=== BY TIER ===")
by = defaultdict(list)
for mp, st, tour, tier, surf in rows:
    by[(tier or "?")].append((mp, st == "won"))
for k in sorted(by, key=lambda k: -len(by[k]))[:5]:
    table(f"tier={k}", by[k])

print("\n=== BY SURFACE ===")
by = defaultdict(list)
for mp, st, tour, tier, surf in rows:
    by[(surf or "?")].append((mp, st == "won"))
for k in sorted(by, key=lambda k: -len(by[k]))[:5]:
    table(f"surface={k}", by[k])
