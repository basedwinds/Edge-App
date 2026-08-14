"""Does grid predictiveness / attrition vary by TRACK TYPE, pooled across series?"""
import sys, json, urllib.request, re
sys.path.insert(0,".")
from collections import defaultdict
from app.models.baseline import racing_ratings as rr

def get(u):
    r=urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r,timeout=30))

SID={'nascar':1,'nascar_xfinity':2,'nascar_truck':3}
ROAD=re.compile(r'road|circuit|glen|sonoma|mid-ohio|street|grand prix|lime rock|americas', re.I)

# NASCAR metadata by (series, date)
meta={}
for series,sid in SID.items():
    for season in (2022,2023,2024,2025,2026):
        try: d=get(f'https://cf.nascar.com/cacher/{season}/{sid}/race_list_basic.json')
        except Exception: continue
        for r in (d if isinstance(d,list) else d.get(f'series_{sid}') or []):
            date=str(r.get('race_date'))[:10]
            laps=r.get('scheduled_laps') or 0
            dist=r.get('scheduled_distance') or 0
            length=(dist/laps) if laps else None
            meta[(series,date)]=(r.get('track_name'), bool(r.get('restrictor_plate')), length)

def classify(track, plate, length):
    if ROAD.search(track or ''): return 'road'
    if plate: return 'plate'
    if length is None: return None
    if length < 1.0: return 'short'
    if length < 2.0: return 'intermediate'
    return 'superspeedway'

rows=defaultdict(list)   # type -> (grid, finish, field_size, dnf)
matched=unmatched=0
for series in SID:
    path=rr._DATA_DIR/f"racing_{series}.json"
    if not path.exists(): continue
    for race in json.loads(path.read_text(encoding='utf-8')).values():
        date=str(race.get('date'))[:10]
        m=meta.get((series,date))
        if not m: unmatched+=1; continue
        t=classify(*m)
        if not t: unmatched+=1; continue
        matched+=1
        res=race['results']
        if not all(r.get('start_order') for r in res): continue
        n=len(res)
        for r in res:
            rows[t].append((r['start_order'], r['order'], n))

print(f"races matched to track metadata: {matched}   unmatched: {unmatched}\n")
print(f"{'track type':16}{'races':>7}{'driver-races':>14}{'corr(grid,finish)':>19}{'top10|P1-5':>12}{'top10|P16+':>12}")
print('-'*82)
import statistics as st
for t in ('plate','short','intermediate','superspeedway','road'):
    v=rows.get(t) or []
    if len(v)<200: 
        print(f"{t:16}{'-':>7}{len(v):>14}   too few"); continue
    g=[x[0] for x in v]; f=[x[1] for x in v]
    mg,mf=st.mean(g),st.mean(f)
    num=sum((a-mg)*(b-mf) for a,b in zip(g,f))
    den=(sum((a-mg)**2 for a in g)**.5)*(sum((b-mf)**2 for b in f)**.5)
    corr=num/den if den else 0
    front=[x for x in v if x[0]<=5]; back=[x for x in v if x[0]>=16]
    t10f=sum(1 for x in front if x[1]<=10)/len(front) if front else 0
    t10b=sum(1 for x in back if x[1]<=10)/len(back) if back else 0
    nraces=len({(x[2]) for x in v})
    print(f"{t:16}{'':>7}{len(v):>14}{corr:>19.3f}{t10f:>11.1%}{t10b:>12.1%}")
