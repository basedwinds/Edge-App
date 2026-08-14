"""Re-scope the plate selector, assert it, then re-measure and re-fit."""
import sys, json, urllib.request, statistics as st
sys.path.insert(0,".")
from collections import defaultdict
from app.models import racing_sim
from app.models.baseline import racing_ratings as rr

def get(u):
    r=urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r,timeout=30))
SID={'nascar':1,'nascar_xfinity':2,'nascar_truck':3}
MIN_LEN=1.0

meta={}
for series,sid in SID.items():
    for s in (2022,2023,2024,2025,2026):
        try: d=get(f'https://cf.nascar.com/cacher/{s}/{sid}/race_list_basic.json')
        except Exception: continue
        for r in (d if isinstance(d,list) else d.get(f'series_{sid}') or []):
            laps=r.get('scheduled_laps') or 0; dist=r.get('scheduled_distance') or 0
            L=(dist/laps) if laps else None
            flagged=bool(r.get('restrictor_plate'))
            meta[(series,str(r.get('race_date'))[:10])]=(r.get('track_name'), flagged, L,
                                                         bool(flagged and L and L>=MIN_LEN))

# ---- ASSERTION: nothing under 1 mile may enter the bucket ----
inb=defaultdict(int); excl=defaultdict(int)
for (s,d),(t,f,L,pack) in meta.items():
    if pack: inb[t]+=1
    elif f: excl[t]+=1
print("SELECTOR: restrictor_plate AND track_length >= 1.0 mi\n")
print("  IN the pack-racing bucket:")
for t,n in sorted(inb.items(), key=lambda kv:-kv[1]): print(f"     {n:3d}  {t}")
print("  EXCLUDED (flagged but sub-mile):")
for t,n in sorted(excl.items(), key=lambda kv:-kv[1]): print(f"     {n:3d}  {t}")
bad=[t for t,_ in inb.items() if any(L and L<MIN_LEN for (s,d),(tt,f,L,p) in meta.items() if tt==t and p)]
print(f"\n  ASSERTION -- any sub-mile track in the bucket? {'FAIL: '+str(bad) if bad else 'PASS (none)'}")

# ---- walk forward, collect per-race rows ----
races=[]
corr_rows=defaultdict(list)
for series in SID:
    p=rr._DATA_DIR/f"racing_{series}.json"
    if not p.exists(): continue
    drv,con={},{}; cur=None
    for race in sorted(json.loads(p.read_text(encoding='utf-8')).values(),
                       key=lambda r:(r['date'] or '', r['id'])):
        if race['season']!=cur:
            cur=race['season']
            for d in drv: drv[d]=rr.BASE+(1-rr.SEASON_REGRESSION)*(drv[d]-rr.BASE)
            for c in con: con[c]=rr.BASE+(1-rr.SEASON_REGRESSION)*(con[c]-rr.BASE)
        res=race['results']; field=[r['driver_id'] for r in res]
        m=meta.get((series,str(race.get('date'))[:10]))
        if m and len(field)>=8 and all(r.get('start_order') for r in res):
            _t,_f,_L,pack=m
            base={r['driver_id']: drv.get(r['driver_id'],rr.BASE)+rr.PARAMS[series]['con_w']*(con.get(r.get('constructor'),rr.BASE)-rr.BASE) for r in res}
            races.append((race['season'], pack, series,
                          [(r['driver_id'], base[r['driver_id']], r['start_order'], r['order']) for r in res]))
            for r in res: corr_rows[pack].append((r['start_order'], r['order']))
        d_rat={d:drv.get(d,rr.BASE) for d in field}
        order={r['driver_id']:r['order'] for r in res}
        drv.update(rr._pairwise(field,order,d_rat,rr.K_DRIVER))
        best={}
        for r in res:
            c=r.get('constructor')
            if c and (c not in best or r['order']<best[c]): best[c]=r['order']
        if len(best)>1:
            c_rat={c:con.get(c,rr.BASE) for c in best}
            con.update(rr._pairwise(list(best),best,c_rat,rr.K_CON))

def corr(xs,ys):
    mx,my=st.mean(xs),st.mean(ys)
    n=sum((a-mx)*(b-my) for a,b in zip(xs,ys))
    d=(sum((a-mx)**2 for a in xs)**.5)*(sum((b-my)**2 for b in ys)**.5)
    return n/d if d else 0.0
print("\ncorr(grid, finish) under the NEW selector:")
for k,lbl in ((True,'pack'),(False,'other')):
    v=corr_rows[k]
    print(f"   {lbl:6} n={len(v):6d}  corr={corr([x[0] for x in v],[x[1] for x in v]):.3f}")

npack=sum(1 for r in races if r[1])
print(f"\npack races usable: {npack}   other: {len(races)-npack}")

def cerr(pairs):
    b=defaultdict(list)
    for p,y in pairs: b[min(int(p*10),9)].append((p,y))
    e=[abs(sum(p for p,_ in v)/len(v)-sum(y for _,y in v)/len(v)) for v in b.values() if len(v)>=20]
    return sum(e)/len(e) if e else None

GRID=[0.0,2.0,5.0,10.0,20.0]; ATT=[0.20,0.30,0.40,0.50,0.60]
preds=defaultdict(lambda: defaultdict(list))
sub=[r for r in races if r[1]]
for gp in GRID:
    for att in ATT:
        for season,_pack,_s,rows in sub:
            ratings={d: b-gp*(g-1) for d,b,g,_f in rows}
            sim=racing_sim.simulate(ratings,trials=1200,top_ns=(10,),seed=5,attrition=att)
            for d,_b,_g,f in rows:
                q=(sim.get(d) or {}).get('top10')
                if q is not None: preds[(gp,att)][season].append((q,1.0 if f<=10 else 0.0))
# shipped baseline, per series
ship=defaultdict(list)
for season,_pack,series,rows in sub:
    tp=rr.TOPN_PARAMS[series]
    ratings={d: b-tp['grid_pts']*(g-1) for d,b,g,_f in rows}
    sim=racing_sim.simulate(ratings,trials=1200,top_ns=(10,),seed=5,attrition=tp['attrition'])
    for d,_b,_g,f in rows:
        q=(sim.get(d) or {}).get('top10')
        if q is not None: ship[season].append((q,1.0 if f<=10 else 0.0))

seasons=sorted({r[0] for r in sub})
best=None
for combo in preds:
    e=cerr([p for v in preds[combo].values() for p in v])
    if e is not None and (best is None or e<best[0]): best=(e,combo)
CAND=best[1]
print(f"\nfull-sample best: grid_pts={CAND[0]} attrition={CAND[1]}")
print(f"\n{'season':>8}{'shipped':>11}{'candidate':>12}{'better?':>9}")
print('-'*42)
w=t=0
for s in seasons:
    a=cerr(ship.get(s,[])); b=cerr(preds[CAND].get(s,[]))
    if a is None or b is None: continue
    t+=1; w+=(b<a)
    print(f"{s:>8}{a:>11.4f}{b:>12.4f}{('YES' if b<a else 'no'):>9}")
print(f"\n  improved {w}/{t}")
allS=[p for v in ship.values() for p in v]; allC=[p for v in preds[CAND].values() for p in v]
print(f"  pooled calib err: shipped {cerr(allS):.4f} -> candidate {cerr(allC):.4f}")
print("\n  candidate deciles:")
b2=defaultdict(list)
for p,y in allC: b2[min(int(p*10),9)].append((p,y))
for k in sorted(b2):
    v=b2[k]
    if len(v)<25: continue
    print(f"     p={k/10:.1f}-{(k+1)/10:.1f}  n={len(v):5d}  claimed {sum(p for p,_ in v)/len(v):.3f}  actual {sum(y for _,y in v)/len(v):.3f}")
