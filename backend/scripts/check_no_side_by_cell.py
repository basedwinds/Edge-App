"""#186 pre-build check: is the NO-side profit compatible with #192?"""
import sys
from collections import defaultdict
from math import sqrt
sys.path.insert(0, ".")
from app.db.database import SessionLocal
from sqlalchemy import text

def wilson(k, n):
    if not n: return (0.0, 1.0)
    p, z = k/n, 1.96
    d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    h = z*sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return (max(0,c-h), min(1,c+h))

s = SessionLocal()
rows = s.execute(text("""
    SELECT sport, market_type, model_prob, market_prob, edge, status
    FROM model_observations
    WHERE status IN ('won','lost') AND model_prob IS NOT NULL
      AND market_prob IS NOT NULL AND market_prob > 0.01 AND market_prob < 0.99
""")).fetchall()
s.close()

def no_roi(v):
    """v = [(market_prob, won)] ; NO bet costs (1-market_prob), pays 1 if NOT won."""
    stake = sum(1.0 - mp for mp, _ in v)
    ret   = sum(1.0 for mp, w in v if not w)
    losses = sum(1 for _, w in v if not w)
    lo, hi = wilson(losses, len(v))
    return (ret/stake - 1.0) if stake else 0.0, len(v), lo, hi

NO = [r for r in rows if r[4] is not None and r[4] <= -0.10]
print(f"NO-side rows (edge <= -10pp): {len(NO)}\n")

print("=== BY SPORT ===")
by = defaultdict(list)
for sp, mt, mp_, mk, e, st in NO:
    by[sp].append((mk, st == "won"))
print(f"{'sport':10}{'n':>6}{'ROI':>10}{'avg mkt':>9}{'avg model':>11}")
for sp, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
    r, n, _, _ = no_roi(v)
    amk = sum(mp for mp, _ in v)/n
    amod = sum(x[2] for x in NO if x[0] == sp)/n
    print(f"{sp:10}{n:>6}{r:>+9.1%}{amk:>9.3f}{amod:>11.3f}")

print("\n=== TENNIS NO, BY MODEL_PROB BUCKET ===")
print("  #192 says tennis p=0.1-0.2 claims 0.156 but DELIVERS 0.315.")
print("  If NO bets live there, they should LOSE. Do they?")
ten = [r for r in NO if r[0] == "tennis"]
tb = defaultdict(list)
for sp, mt, mp_, mk, e, st in ten:
    tb[min(int(mp_*10), 9)].append((mp_, mk, st == "won"))
print(f"\n{'model p':12}{'n':>6}{'claimed':>9}{'ACTUAL':>8}{'avg mkt':>9}{'NO ROI':>9}")
for k in sorted(tb):
    v = tb[k]
    claimed = sum(a for a,_,_ in v)/len(v)
    actual  = sum(1 for _,_,w in v if w)/len(v)
    r, n, _, _ = no_roi([(b, w) for _, b, w in v])
    print(f"{k/10:.1f}-{k/10+0.1:.1f}   {len(v):>6}{claimed:>9.3f}{actual:>8.3f}"
          f"{sum(b for _,b,_ in v)/len(v):>9.3f}{r:>+9.1%}")

print("\n=== TENNIS NO, BY MARKET_TYPE ===")
tm = defaultdict(list)
for sp, mt, mp_, mk, e, st in ten:
    tm[mt].append((mk, st == "won"))
for mt, v in sorted(tm.items(), key=lambda kv: -len(kv[1])):
    r, n, lo, hi = no_roi(v)
    print(f"  {str(mt)[:26]:26} n={n:>5}  ROI {r:>+7.1%}   loss-rate CI [{lo:.3f},{hi:.3f}]")
