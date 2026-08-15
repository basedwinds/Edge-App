"""Fit a temperature for tennis moneyline, chronological holdout.

Same acceptance rule calibration_temp.py already enforces: a temperature ships
ONLY if it improves BOTH ECE and Brier out of sample. T>1 softens.
"""
import sys, math
from collections import defaultdict
sys.path.insert(0, ".")
from app.db.database import SessionLocal
from sqlalchemy import text

EPS = 1e-6
def cal(p, t):
    p = min(max(p, EPS), 1-EPS)
    return 1.0/(1.0+math.exp(-math.log(p/(1-p))/t))

def ece(rows, t, bins=10):
    b = defaultdict(list)
    for p, y in rows:
        q = cal(p, t)
        b[min(int(q*bins), bins-1)].append((q, y))
    n = len(rows)
    return sum(len(v)/n*abs(sum(q for q,_ in v)/len(v) - sum(y for _,y in v)/len(v))
               for v in b.values() if v)

def brier(rows, t):
    return sum((cal(p,t)-y)**2 for p,y in rows)/len(rows)

def logloss(rows, t):
    s=0.0
    for p,y in rows:
        q=min(max(cal(p,t),EPS),1-EPS)
        s -= y*math.log(q)+(1-y)*math.log(1-q)
    return s/len(rows)

s = SessionLocal()
rows = s.execute(text("""
    SELECT model_prob, status, COALESCE(event_start, settled_at, observed_at) ts
    FROM model_observations
    WHERE sport='tennis' AND market_type='moneyline'
      AND status IN ('won','lost') AND model_prob IS NOT NULL
    ORDER BY ts
""")).fetchall()
s.close()
data = [(r[0], 1.0 if r[1]=="won" else 0.0) for r in rows if r[2]]
print(f"tennis moneyline with a timestamp: {len(data)}")
cut = int(len(data)*0.6)
tr, te = data[:cut], data[cut:]
print(f"  train {len(tr)}  holdout {len(te)}")
print(f"  train window ends {rows[cut][2]}\n")

best = None
for i in range(60, 251):
    t = i/100
    l = logloss(tr, t)
    if best is None or l < best[0]: best = (l, t)
T = best[1]
print(f"fitted on TRAIN by log-loss: T = {T:.2f}\n")
print(f"{'':10}{'ECE':>10}{'Brier':>11}{'logloss':>11}")
print('-'*44)
for lbl, t in (("raw (T=1.0)", 1.0), (f"T={T:.2f}", T)):
    print(f"{lbl:10}{ece(te,t):>10.4f}{brier(te,t):>11.5f}{logloss(te,t):>11.5f}")
ok = ece(te,T) < ece(te,1.0) and brier(te,T) < brier(te,1.0)
print(f"\n  improves BOTH ECE and Brier out of sample: {'YES -- shippable' if ok else 'NO -- reject'}")

print("\nheld-out deciles, raw vs calibrated:")
for lbl, t in (("RAW", 1.0), (f"T={T:.2f}", T)):
    b = defaultdict(list)
    for p, y in te:
        q = cal(p, t); b[min(int(q*10),9)].append((q,y))
    print(f"  {lbl}")
    for k in sorted(b):
        v=b[k]
        if len(v)<20: continue
        print(f"     p={k/10:.1f}  n={len(v):4d}  claimed {sum(q for q,_ in v)/len(v):.3f}  "
              f"actual {sum(y for _,y in v)/len(v):.3f}  {sum(y for _,y in v)/len(v)-sum(q for q,_ in v)/len(v):+.3f}")
