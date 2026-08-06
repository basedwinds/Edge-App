"""Check the NASCAR playoff sim against invariants an elimination format implies.

Each fails loudly if a round is wired wrong -- eliminations applied to the wrong
list, the championship race scored across the whole field instead of the four,
or the regular-season simulation not feeding qualification.
"""
import statistics

from app.models import racing_playoff_sim as sim

# Synthetic field with a clear strength ordering, so "the best driver wins most"
# is checkable without depending on live ratings.
N = 40
drivers = [f"D{i:02d}" for i in range(N)]
ratings = {d: 1600 - i * 8 for i, d in enumerate(drivers)}
points = {d: 900 - i * 20 for i, d in enumerate(drivers)}
wins = {d: (3 if i < 2 else (1 if i < 10 else 0)) for i, d in enumerate(drivers)}

fails = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)


print("1) title probabilities form a distribution")
p = sim.simulate_nascar_title(ratings, wins, points, regular_races_left=4, trials=3000)
tot = sum(p.values())
# Tolerance is 4-decimal rounding across the whole field, not 1e-6: each value
# is round(x, 4), so ~30 of them can drift a few 1e-4 in aggregate. An earlier
# version asserted 1e-6 and failed at 0.999800 -- the expectation was wrong.
check("sums to 1.0", abs(tot - 1.0) < 5e-3, f"got {tot:.6f}")
check("no negatives", all(v >= 0 for v in p.values()))

print("\n2) at most 16 can win it ONCE THE FIELD IS SET")
# Across trials MORE than 16 drivers legitimately carry title odds, because the
# remaining regular season is simulated and different drivers qualify in
# different trials -- that is exactly what check 4 exists to confirm. The
# 16-driver cap is a PER-TRIAL fact, so it is tested with the field frozen (0
# races left), where a 17th possible champion really would mean eliminations
# were applied to the wrong list. An earlier version asserted the cap on the
# open-field run and failed at 28; the expectation was wrong, not the model.
frozen = sim.simulate_nascar_title(ratings, wins, points, regular_races_left=0, trials=3000)
nz_frozen = [d for d, v in frozen.items() if v > 0]
check("frozen field -> at most 16 possible champions",
      len(nz_frozen) <= sim.PLAYOFF_FIELD,
      f"{len(nz_frozen)} with the field fixed (cap={sim.PLAYOFF_FIELD})")
nonzero = [d for d, v in p.items() if v > 0]
print(f"       (open field gives {len(nonzero)} drivers a path -- expected, see check 4)")

print("\n3) strength ordering is respected at the top")
top = sorted(p.items(), key=lambda kv: -kv[1])[:5]
best_rated = max(p, key=lambda d: ratings[d])
check("best-rated driver is the favourite", top[0][0] == best_rated,
      f"favourite={top[0][0]} best-rated={best_rated}")

print("\n4) a winless driver outside the cut can still qualify (races left > 0)")
p_open = sim.simulate_nascar_title(ratings, wins, points, regular_races_left=6, trials=3000)
p_shut = sim.simulate_nascar_title(ratings, wins, points, regular_races_left=0, trials=3000)
outsiders = [d for i, d in enumerate(drivers) if i >= 20]
open_mass = sum(p_open.get(d, 0) for d in outsiders)
shut_mass = sum(p_shut.get(d, 0) for d in outsiders)
check("races remaining gives outsiders a path", open_mass > shut_mass,
      f"6 races left: {open_mass:.4f} vs 0 left: {shut_mass:.4f}")

print("\n5) the field is genuinely cut to 4 before the finale")
check("ROUND_SURVIVORS ends at a single champion",
      sim.ROUND_SURVIVORS[-1] == 1 and sim.ROUND_SURVIVORS[-2] == sim.CHAMPIONSHIP_ROUND_SIZE,
      str(sim.ROUND_SURVIVORS))
check("rounds and race counts line up",
      len(sim.ROUND_RACES) == len(sim.ROUND_SURVIVORS),
      f"{sim.ROUND_RACES} vs {sim.ROUND_SURVIVORS}")

print("\n6) refuses a field too small to run a playoff")
small = {d: ratings[d] for d in drivers[:8]}
check("returns empty rather than inventing a bracket",
      sim.simulate_nascar_title(small, wins, {d: points[d] for d in small}, 2, trials=50) == {})

print("\n7) deterministic for a fixed seed")
a = sim.simulate_nascar_title(ratings, wins, points, 4, trials=800, seed=11)
b = sim.simulate_nascar_title(ratings, wins, points, 4, trials=800, seed=11)
check("same seed -> same answer", a == b)

print("\ntop 8 title odds:")
for d, v in sorted(p.items(), key=lambda kv: -kv[1])[:8]:
    print(f"  {d}  rating={ratings[d]}  wins={wins[d]}  title={v:.4f}")

print("\nRESULT:", "ALL CHECKS PASS" if not fails else f"{len(fails)} FAILED: {fails}")
