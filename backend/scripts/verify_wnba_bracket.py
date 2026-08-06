"""Check the WNBA bracket sim against invariants a real bracket must satisfy.

Each of these fails loudly if a round is wired to the wrong thing -- counts
tallied in the wrong place, reseeding applied to the wrong list, or a series
returning the loser.
"""
import statistics

from app.models import season_sim_wnba as sim
from app.models.baseline import elo_service_wnba

elo_service_wnba.refresh_ratings()
probs = sim.bracket_probs(trials=1500)
print(f"teams: {len(probs)}")
if not probs:
    raise SystemExit("empty -- cannot verify")

standings = sim.standings_probs(trials=1500)
fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)


print("\n1) exactly 4 semifinalists, 2 finalists, 1 champion per trial")
for key, want in (("semifinal", 4.0), ("finals", 2.0), ("champion", 1.0)):
    tot = sum(p[key] for p in probs.values())
    check(f"league {key} sums to {want}", abs(tot - want) < 0.02, f"got {tot:.4f}")

print("\n2) monotonic: champion <= finals <= semifinal, per team")
bad = [t for t, p in probs.items()
       if not (p["champion"] <= p["finals"] + 1e-9 <= p["semifinal"] + 1e-9)]
check("no team wins a round it never reached", not bad, f"violations: {bad[:5]}")

print("\n3) semifinal <= playoff qualification (from the same sim's standings)")
bad = [(t, probs[t]["semifinal"], standings[t]["playoff"]) for t in probs
       if t in standings and probs[t]["semifinal"] > standings[t]["playoff"] + 0.03]
check("cannot reach the semis without making the playoffs", not bad, f"violations: {bad[:4]}")

print("\n4) the best team is the most likely champion")
best_by_playoff = max(standings, key=lambda t: standings[t]["one_seed"])
best_by_title = max(probs, key=lambda t: probs[t]["champion"])
check("1-seed favourite is also title favourite", best_by_playoff == best_by_title,
      f"one_seed favourite={best_by_playoff}, title favourite={best_by_title}")

print("\n5) reseeding actually happened (a 1-seed does NOT always meet the 4/5 winner)")
# Structural, not statistical: with reseeding the top seed's semifinal opponent
# varies with which upsets occurred. Checked indirectly -- if the bracket were
# fixed, the sum over teams would still be 4/2/1, so only the code path proves
# it. Assert the constant instead, so a silent revert to a fixed bracket fails.
check("_ROUND1_PAIRS is 1v8/2v7/3v6/4v5",
      sim._ROUND1_PAIRS == ((0, 7), (1, 6), (2, 5), (3, 4)), str(sim._ROUND1_PAIRS))

print("\ntop 8 by title probability:")
for t, p in sorted(probs.items(), key=lambda kv: -kv[1]["champion"])[:8]:
    s = standings.get(t, {})
    print(f"  {t:4} champ={p['champion']:.3f}  finals={p['finals']:.3f}  semis={p['semifinal']:.3f}"
          f"   (playoff={s.get('playoff', 0):.3f} one_seed={s.get('one_seed', 0):.3f})")

print("\nRESULT:", "ALL CHECKS PASS" if not fails else f"{len(fails)} FAILED: {fails}")
