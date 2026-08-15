"""Does recent BULLPEN WORKLOAD predict beyond team strength?

MEASURED BEFORE ANY BUILD. The MLB model prices off team Elo plus a
starting-pitcher adjustment and carries no bullpen term at all -- the biggest
named structural hole. But "there is a hole" is not evidence there is signal in
it, so this asks the cheap question first.

DATA IS ALREADY CACHED: mlb_boxscore_cache.json carries relief_ip per side for
all 2,430 games of 2024. No new source, no crawl.

METHOD. Walk 2024 in date order. Before each game, compute each team's relief
innings over the trailing 3 days (the standard fatigue window) from PRIOR games
only. Predict with the app's OWN elo_mlb primitives -- predict_and_update, which
applies SEASON_REGRESSION -- then ask whether the fatigue differential explains
the residual the model leaves behind.

DELIBERATELY TEAM-ONLY (pitcher_adj=0) AS A SCREEN. If bullpen fatigue cannot
beat team strength alone it certainly will not beat team+pitcher, so a null here
closes the question cheaply. A POSITIVE result would NOT be sufficient -- it
would have to be re-tested with the pitcher adjustment in, because starter
quality and bullpen usage are correlated (a short start empties the pen).
"""
import json, sys, statistics as st
from collections import defaultdict, deque
sys.path.insert(0, ".")
from app.models.baseline import elo_mlb

box = json.load(open("../data/mlb_boxscore_cache.json", encoding="utf-8"))
sched = json.load(open("../data/mlb_schedule_cache.json", encoding="utf-8"))
rows = list(sched.values()) if isinstance(sched, dict) else sched
games = [g for g in rows if g.get("home_score") is not None
         and str(g.get("gameday", "")).startswith("2024") and g.get("game_type") == "R"]
games.sort(key=lambda g: (g["gameday"], g.get("gametime") or "", g.get("game_number") or 0))
print(f"2024 regular-season games with a final score: {len(games)}")

# boxscore keyed by game id; index by (gameday, home, away) instead
bx = {}
for v in box.values():
    k = (v.get("gameday"), v.get("home_team"), v.get("away_team"))
    if all(k): bx[k] = v
print(f"boxscores indexable by (date, home, away): {len(bx)}")

state = elo_mlb.EloState()
recent = defaultdict(deque)      # team -> deque of (date, relief_ip)
pts = []                          # (fatigue_diff, residual, pred)
matched = 0
from datetime import date
def d(s): 
    y,m,dd = s.split("-"); return date(int(y),int(m),int(dd))

for g in games:
    h, a, day = g["home_team"], g["away_team"], g["gameday"]
    gd = d(day)
    def fatigue(team):
        q = recent[team]
        while q and (gd - q[0][0]).days > 3:
            q.popleft()
        return sum(ip for _, ip in q)
    fh, fa = fatigue(h), fatigue(a)
    hr, ar = state.get(h), state.get(a)
    pred = elo_mlb.win_prob(hr, ar)
    actual = 1.0 if g["home_score"] > g["away_score"] else 0.0
    b = bx.get((day, h, a))
    if b is not None:
        matched += 1
        # AWAY tired minus HOME tired: positive = away pen more worked
        pts.append((fa - fh, actual - pred, pred))
        for side, team in (("home", h), ("away", a)):
            ip = (b.get(side) or {}).get("relief_ip")
            if ip is not None: recent[team].append((gd, float(ip)))
    elo_mlb.predict_and_update(state, g)

print(f"games with a usable pre-game fatigue window: {matched}\n")
xs = [p[0] for p in pts if abs(p[0]) > 0]
ys = [p[1] for p in pts if abs(p[0]) > 0]
mx, my = st.mean(xs), st.mean(ys)
num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
den = (sum((x-mx)**2 for x in xs)**.5) * (sum((y-my)**2 for y in ys)**.5)
r = num/den if den else 0.0
print(f"corr(away_fatigue - home_fatigue, home residual) = {r:+.4f}   n={len(xs)}")
print("  positive would mean: a more-worked AWAY pen -> home wins more than the model said\n")

print("residual by fatigue-differential bucket (relief IP over trailing 3 days):")
b = defaultdict(list)
for fd, res, _ in pts:
    k = max(-3, min(3, int(round(fd / 2.0))))
    b[k].append(res)
print(f"{'away-home rel IP':>18}{'n':>7}{'mean residual':>16}")
for k in sorted(b):
    v = b[k]
    if len(v) < 60: continue
    print(f"{k*2:>+15} ip{len(v):>7}{st.mean(v):>+16.4f}")
