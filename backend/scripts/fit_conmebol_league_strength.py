"""Fit CONMEBOL league-strength offsets -> data/soccer_conmebol_strength.json.

The sibling of scripts/fit_uefa_league_strength.py, and deliberately a SEPARATE
fit rather than an extension of it: the UEFA offsets were estimated against a
UEFA-wide baseline mu with England pinned, and mixing scales is the error class
that produced the CFB margin constants fitted on the NFL's Elo.

THE JOIN. ESPN publishes conmebol.libertadores and conmebol.sudamericana. Both
are fetched A MONTH AT A TIME, not a season at a time, because ESPN caps a
scoreboard response at 100 events and a wide window silently truncates -- the
same cap that broke soccer settlement once already. The script prints a WARN
whenever a month comes back at exactly 100 so a future truncation is visible
rather than quiet.

ONLY CROSS-COUNTRY MATCHES ARE FITTED. A Flamengo-Cruzeiro tie is two BRA1 clubs
and carries no information about the gap between leagues; including such rows
would add noise and pull mu around. 1,513 completed matches were fetched, 609
had both clubs in a rated pool, and 516 of those were cross-country.

RESULT, 2026-08-18 (BRA1 pinned as reference, mu = 1.1039):

    BRA1  +0.000     COL1  -0.311     URU1  -0.423
    ARG1  -0.120     ECU1  -0.313     VEN1  -0.773

The recovered ordering -- Brazil > Argentina >> Colombia/Ecuador > Uruguay >
Venezuela -- is the consensus South American hierarchy, which the fit was never
shown.

HELD OUT BY SEASON, and 4 of 5 rather than 5 of 5:

    held-out    n    no offsets   fitted     gain
        2022   87       2.5787    2.1364   +0.4423
        2023  124       2.5588    2.2060   +0.3528
        2024  101       2.7098    2.2542   +0.4557
        2025  109       2.5530    2.6910   -0.1380   <-- does NOT transfer
        2026   95       2.2476    2.0495   +0.1981

Pooled across all 516 held-out matches, mean Poisson deviance 2.5332 -> 2.2773.
On a sign test alone 4 of 5 is only P=0.19, so the case rests on the pooled
improvement and the recovered ordering, not on the count. Recorded here rather
than smoothed over, because a future re-fit should know that 2025 was the season
that argued against.

Run with --fetch to re-pull ESPN into the cache, then re-run to fit.
"""

# ---- stage 1: fetch ESPN results into the cache ----
import json, sys, time, urllib.request, datetime, collections
sys.path.insert(0, r"C:\Users\awaws\Downloads\nfl-edge-app\backend")
from app.models.baseline import elo_service_soccer as S
from app.ingestion.market_matcher_soccer import canonical_team_key

B = 'https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={a}-{b}'
def get(u):
    for i in range(4):
        try:
            r = urllib.request.Request(u, headers={'User-Agent':'Mozilla/5.0'})
            return json.load(urllib.request.urlopen(r, timeout=45))
        except Exception:
            time.sleep(2)
    return {}

rows = []
for slug in ('conmebol.libertadores', 'conmebol.sudamericana'):
    for year in (2022, 2023, 2024, 2025, 2026):
        for m in range(1, 13):
            a = datetime.date(year, m, 1)
            b = (datetime.date(year + (m == 12), (m % 12) + 1, 1) - datetime.timedelta(days=1))
            d = get(B.format(slug=slug, a=a.strftime('%Y%m%d'), b=b.strftime('%Y%m%d')))
            evs = d.get('events', [])
            if len(evs) >= 100:
                print(f'  WARN {slug} {year}-{m:02d} hit the 100 cap', flush=True)
            for ev in evs:
                try:
                    c = ev['competitions'][0]
                    if not (ev.get('status') or {}).get('type', {}).get('completed'):
                        continue
                    cs = c['competitors']
                    h = next(x for x in cs if x['homeAway'] == 'home')
                    aw = next(x for x in cs if x['homeAway'] == 'away')
                    rows.append({
                        'slug': slug, 'date': ev['date'][:10],
                        'home': h['team']['displayName'], 'away': aw['team']['displayName'],
                        'hg': int(h['score']), 'ag': int(aw['score']),
                    })
                except (KeyError, IndexError, ValueError, StopIteration):
                    continue
            time.sleep(0.12)
    print(f'{slug}: cumulative {len(rows)}', flush=True)

# dedupe
seen, uniq = set(), []
for r in rows:
    k = (r['date'], r['home'], r['away'])
    if k in seen: continue
    seen.add(k); uniq.append(r)
print(f'\n{len(uniq)} unique completed CONMEBOL matches')

S.refresh_ratings()
pools = S._cache['states_by_league']
member = {lg: set(st.attack_log) for lg, st in pools.items()}
SA = ['BRA1','ARG1','COL1','ECU1','URU1','VEN1']
def league_of(name):
    k = canonical_team_key(name)
    hits = [lg for lg in SA if k in member[lg]]
    return hits[0] if len(hits) == 1 else None

both = 0
by_pair = collections.Counter()
for r in uniq:
    lh, la = league_of(r['home']), league_of(r['away'])
    r['lh'], r['la'] = lh, la
    if lh and la:
        both += 1
        by_pair[tuple(sorted((lh, la)))] += 1
print(f'both clubs resolve to a rated South American pool: {both} of {len(uniq)}')
print('by league pair:', by_pair.most_common())
json.dump(uniq, open(r'C:\Users\awaws\AppData\Local\Temp\claude\C--Users-awaws-Downloads-files\78c551e1-57ff-47f1-960c-bc9c5e2ddaab\scratchpad\conmebol.json','w'))


# ---- stage 2: fit the offsets ----
import json, math, sys, collections
sys.path.insert(0, r"C:\Users\awaws\Downloads\nfl-edge-app\backend")
from app.models.baseline.elo_soccer import HOME_ADVANTAGE_LOG
from app.models.baseline import elo_service_soccer as S
from app.ingestion.market_matcher_soccer import canonical_team_key

REFERENCE = "BRA1"          # deepest South American pool -> the natural anchor
STEP, ITERS = 0.5, 4000
SA = ["BRA1", "ARG1", "COL1", "ECU1", "URU1", "VEN1"]

raw = json.load(open(r"C:\Users\awaws\AppData\Local\Temp\claude\C--Users-awaws-Downloads-files\78c551e1-57ff-47f1-960c-bc9c5e2ddaab\scratchpad\conmebol.json"))
S.refresh_ratings()
pools = S._cache["states_by_league"]
member = {lg: set(pools[lg].attack_log) for lg in SA}

def resolve(name):
    k = canonical_team_key(name)
    hits = [lg for lg in SA if k in member[lg]]
    return (k, hits[0]) if len(hits) == 1 else None

by_season = collections.defaultdict(list)
for r in raw:
    h, a = resolve(r["home"]), resolve(r["away"])
    if not h or not a or h[1] == a[1]:
        continue                       # cross-country only: same-league pairs teach no offset
    (hk, hl), (ak, al) = h, a
    sh, sa_ = pools[hl], pools[al]
    by_season[r["date"][:4]].append((
        sh.attack_log[hk], sh.concede_log[hk], hl,
        sa_.attack_log[ak], sa_.concede_log[ak], al,
        r["hg"], r["ag"]))

def loglik_grad(rows, mu_log, s):
    g_mu, g_s = 0.0, {L: 0.0 for L in SA}
    for ah, ch, lh, aa, ca, la, gh, ga in rows:
        d = s[lh] - s[la]
        lam_h = math.exp(mu_log + ah + ca + HOME_ADVANTAGE_LOG + d)
        lam_a = math.exp(mu_log + aa + ch - d)
        rh, ra = gh - lam_h, ga - lam_a
        g_mu += rh + ra
        g_s[lh] += rh - ra
        g_s[la] -= rh - ra
    return g_mu, g_s

def fit(rows):
    mu_log, s = math.log(1.3), {L: 0.0 for L in SA}
    for _ in range(ITERS):
        g_mu, g_s = loglik_grad(rows, mu_log, s)
        n = max(1, len(rows))
        mu_log += STEP * g_mu / n
        for L in SA:
            if L == REFERENCE: continue
            s[L] += STEP * g_s[L] / n
    return mu_log, s

def score(rows, mu_log, s):
    tot = 0.0
    for ah, ch, lh, aa, ca, la, gh, ga in rows:
        d = s.get(lh, 0.0) - s.get(la, 0.0)
        lam_h = math.exp(mu_log + ah + ca + HOME_ADVANTAGE_LOG + d)
        lam_a = math.exp(mu_log + aa + ch - d)
        for g, lam in ((gh, lam_h), (ga, lam_a)):
            tot += 2 * ((g * math.log(g / lam) if g > 0 else 0.0) - (g - lam))
    return tot / max(1, len(rows))

seasons = sorted(by_season)
allrows = [r for k in seasons for r in by_season[k]]
print(f"{len(allrows)} cross-country matches, seasons {seasons}")
print("per season:", {k: len(by_season[k]) for k in seasons})
mu_all, s_all = fit(allrows)
print(f"\nmu = {math.exp(mu_all):.4f}   (reference {REFERENCE} pinned at 0)")
for L in sorted(s_all, key=lambda L: -s_all[L]):
    print(f"   {L:<6} {s_all[L]:+.4f}")

print(f"\n{'held-out':>10} {'n':>5} {'no offsets':>11} {'fitted':>9} {'gain':>9}")
gains = []
for k in seasons:
    test, train = by_season[k], [r for j in seasons if j != k for r in by_season[j]]
    if len(test) < 25 or not train: 
        print(f"{k:>10} {len(test):>5}   skipped (n<25)"); continue
    mu_t, s_t = fit(train)
    base = score(test, mu_t, {L: 0.0 for L in SA})
    fitd = score(test, mu_t, s_t)
    gains.append(base - fitd)
    print(f"{k:>10} {len(test):>5} {base:>11.4f} {fitd:>9.4f} {base-fitd:>+9.4f}")
print(f"\nseasons improved: {sum(1 for g in gains if g > 0)} of {len(gains)}")
json.dump({"mu": math.exp(mu_all), "offsets": s_all, "reference": REFERENCE,
           "n_matches": len(allrows)},
          open(r"C:\Users\awaws\AppData\Local\Temp\claude\C--Users-awaws-Downloads-files\78c551e1-57ff-47f1-960c-bc9c5e2ddaab\scratchpad\conmebol_fit.json", "w"), indent=1)
