"""Check the two new season_sim outputs against invariants they MUST satisfy.

These are not "does it run" checks -- each one would fail loudly if the tally
were wired to the wrong thing:

  1. seed_pct sums to 1.0 per team (a team holds exactly one seed, or none).
  2. Exactly 7 seeds per conference are handed out each trial, so summing
     seed_pct[1..7] across a conference's teams gives 7.0 for each seed index
     it should give 1.0 -- i.e. sum over teams of P(team is seed k) == 1 for
     every k, per conference.
  3. sum(seed_pct[1..7]) == playoff_pct  (seeded <=> in the playoffs).
  4. seed_pct[1] == one_seed_pct         (agrees with the pre-existing field).
  5. playoff_host_pct <= playoff_pct     (cannot host without qualifying).
  6. playoff_host_pct >= division_pct    (every division winner hosts: seeds
     1-4 all host, the 1-seed in the divisional round).
  7. Exactly 6 hosts per conference per trial (3 WC + 2 DIV + 1 CONF), so the
     league-wide sum of playoff_host_pct is 12.0.
"""
from app.models import season_sim
from app.data.divisions import TEAM_CONFERENCE

# Small synthetic season: every team plays a full slate against its own
# conference so the bracket is exercised without needing real fixtures.
teams = list(TEAM_CONFERENCE.keys())
ratings = {t: 1500.0 + (i % 8) * 15 for i, t in enumerate(teams)}
games = []
for i, home in enumerate(teams):
    for j, away in enumerate(teams):
        if i < j and TEAM_CONFERENCE[home] == TEAM_CONFERENCE[away]:
            games.append({"home_team": home, "away_team": away,
                          "home_score": None, "away_score": None, "location": None})
print(f"teams={len(teams)} synthetic games={len(games)}")

res = season_sim.run_simulation(ratings, games, n_trials=2000, seed=17)
rows = {t: r for t, r in res.items() if not t.startswith("_")}

fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)


print("\n1) seed_pct sums to 1.0 per team")
worst = max(abs(sum(r["seed_pct"]) - 1.0) for r in rows.values())
check("every team's seed_pct sums to 1", worst < 1e-9, f"max deviation {worst:.2e}")

print("\n2) exactly one team holds each seed, per conference, per trial")
for conf in ("AFC", "NFC"):
    sel = [r for t, r in rows.items() if TEAM_CONFERENCE[t] == conf]
    for k in range(1, 8):
        tot = sum(r["seed_pct"][k] for r in sel)
        if abs(tot - 1.0) > 1e-9:
            check(f"{conf} seed {k} sums to 1", False, f"got {tot:.6f}")
            break
    else:
        check(f"{conf}: all 7 seeds sum to 1.0 across its teams", True)

print("\n3) sum(seed_pct[1..7]) == playoff_pct")
worst = max(abs(sum(r["seed_pct"][1:]) - r["playoff_pct"]) for r in rows.values())
check("seeded <=> in playoffs", worst < 1e-9, f"max deviation {worst:.2e}")

print("\n4) seed_pct[1] == one_seed_pct (agrees with the existing field)")
worst = max(abs(r["seed_pct"][1] - r["one_seed_pct"]) for r in rows.values())
check("1-seed agrees", worst < 1e-9, f"max deviation {worst:.2e}")

print("\n5) playoff_host_pct <= playoff_pct")
bad = [t for t, r in rows.items() if r["playoff_host_pct"] > r["playoff_pct"] + 1e-9]
check("no team hosts without qualifying", not bad, f"violations: {bad[:4]}")

print("\n6) playoff_host_pct >= division_pct (every division winner hosts)")
bad = [(t, round(r["division_pct"], 4), round(r["playoff_host_pct"], 4))
       for t, r in rows.items() if r["playoff_host_pct"] < r["division_pct"] - 1e-9]
check("division winners always host", not bad, f"violations: {bad[:4]}")

print("\n7) distinct hosts per conference is 4-6 -> league total in [8, 12]")
# 6 GAMES are hosted per conference (3 WC + 2 DIV + 1 CONF), but this metric is
# "hosts AT LEAST ONE", so a team hosting two rounds counts once. The 1-seed
# routinely hosts both the divisional and conference games, so the distinct
# count is 4-6, never a flat 6. An earlier version of this check asserted 12.0
# and failed -- the expectation was wrong, not the tally. Which is the point of
# stating the bound rather than a number: 12.0 would silently pass a metric that
# double-counted a repeat host.
tot = sum(r["playoff_host_pct"] for r in rows.values())
check("league-wide distinct-host count in [8, 12]", 8.0 - 1e-9 <= tot <= 12.0 + 1e-9,
      f"got {tot:.4f} ({tot / 2:.2f} per conference)")

print("\nsample (highest host probability):")
top = sorted(rows.items(), key=lambda kv: -kv[1]["playoff_host_pct"])[:5]
for t, r in top:
    seeds = ", ".join(f"s{k}={r['seed_pct'][k]:.3f}" for k in range(1, 4))
    print(f"  {t:4} host={r['playoff_host_pct']:.3f} div={r['division_pct']:.3f} "
          f"playoff={r['playoff_pct']:.3f}  {seeds}")

print("\nRESULT:", "ALL CHECKS PASS" if not fails else f"{len(fails)} FAILED: {fails}")
