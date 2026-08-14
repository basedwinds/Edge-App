"""Are the SHIPPED top-N params mis-calibrated on PLATE races specifically?

One pass, not a grid search. If the shipped parameters are well calibrated on
non-plate and badly calibrated on plate, that alone establishes plate needs its
own treatment -- and only then is a sweep worth the compute.
"""
import sys, json, urllib.request
sys.path.insert(0,".")
from collections import defaultdict
from app.models import racing_sim
from app.models.baseline import racing_ratings as rr

def get(u):
    r=urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r,timeout=30))
SID={'nascar':1,'nascar_xfinity':2,'nascar_truck':3}
plate={}
for series,sid in SID.items():
    for s in (2022,2023,2024,2025,2026):
        try: d=get(f'https://cf.nascar.com/cacher/{s}/{sid}/race_list_basic.json')
        except Exception: continue
        for r in (d if isinstance(d,list) else d.get(f'series_{sid}') or []):
            plate[(series,str(r.get('race_date'))[:10])]=bool(r.get('restrictor_plate'))

pts=defaultdict(list)   # (is_plate) -> [(predicted_top10, actual)]
for series in SID:
    p=rr._DATA_DIR/f"racing_{series}.json"
    if not p.exists(): continue
    races=sorted(json.loads(p.read_text(encoding='utf-8')).values(),
                 key=lambda r:(r['date'] or '', r['id']))
    drv,con={},{}; cur=None
    for race in races:
        if race['season']!=cur:
            cur=race['season']
            for d in drv: drv[d]=rr.BASE+(1-rr.SEASON_REGRESSION)*(drv[d]-rr.BASE)
            for c in con: con[c]=rr.BASE+(1-rr.SEASON_REGRESSION)*(con[c]-rr.BASE)
        res=race['results']; field=[r['driver_id'] for r in res]
        pl=plate.get((series,str(race.get('date'))[:10]))
        if pl is not None and len(field)>=8 and all(r.get('start_order') for r in res):
            tp=rr.TOPN_PARAMS[series]
            ratings={}
            for r in res:
                s=drv.get(r['driver_id'],rr.BASE)+rr.PARAMS[series]['con_w']*(con.get(r.get('constructor'),rr.BASE)-rr.BASE)
                ratings[r['driver_id']]=s-tp['grid_pts']*(r['start_order']-1)
            sim=racing_sim.simulate(ratings,trials=1200,top_ns=(10,),seed=5,attrition=tp['attrition'])
            for r in res:
                q=(sim.get(r['driver_id']) or {}).get('top10')
                if q is not None: pts[pl].append((q, 1.0 if r['order']<=10 else 0.0))
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

print("SHIPPED top-N parameters, scored separately on plate vs non-plate\n")
print(f"{'':12}{'n':>7}{'claimed':>10}{'actual':>9}{'gap':>9}{'calib err':>11}")
print('-'*58)
for lbl,key in (("non-plate",False),("plate",True)):
    v=pts[key]
    if not v: continue
    claimed=sum(p for p,_ in v)/len(v); actual=sum(y for _,y in v)/len(v)
    b=defaultdict(list)
    for p,y in v: b[min(int(p*10),9)].append((p,y))
    errs=[abs(sum(p for p,_ in q)/len(q)-sum(y for _,y in q)/len(q)) for q in b.values() if len(q)>=25]
    ce=sum(errs)/len(errs) if errs else float('nan')
    print(f"{lbl:12}{len(v):>7}{claimed:>10.3f}{actual:>9.3f}{claimed-actual:>+9.3f}{ce:>11.4f}")
print("\nby predicted-probability decile (plate only):")
v=pts[True]; b=defaultdict(list)
for p,y in v: b[min(int(p*10),9)].append((p,y))
for k in sorted(b):
    q=b[k]
    if len(q)<25: continue
    print(f"   p={k/10:.1f}-{(k+1)/10:.1f}  n={len(q):5d}  claimed {sum(p for p,_ in q)/len(q):.3f}  actual {sum(y for _,y in q)/len(q):.3f}")
