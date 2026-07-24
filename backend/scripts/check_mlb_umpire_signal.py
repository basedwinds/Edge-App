"""Investigation script (not a registered backtest): do individual home-
plate umpires carry a real, PREDICTIVE scoring-environment tendency for MLB
totals? Real phenomenon in sabermetric literature for called-strike-zone
size/K%/BB% -- less established specifically for total RUNS (more noisy
pathways: defense, baserunning, sequencing), so this is a genuine "check
before believing it" case, not a foregone conclusion.

Uses data/mlb_umpire_cache.json (home-plate umpire per game, 2016-2025, see
build_mlb_umpire_cache.py) joined against real final scores. Unlike a raw
correlation check, this tests actual OUT-OF-SAMPLE PREDICTIVE validity: for
each umpire with enough starts, their FIRST-HALF (chronological) average
residual (actual_total - PARK_FACTOR-adjusted expected_total) is used to
predict their SECOND-HALF games' residuals -- the real question for a
betting model isn't "did this umpire's games run hot/cold in the past" (true
by construction) but "does that past pattern predict a FUTURE game with this
same umpire," which is the actual, harder bar a live signal needs to clear.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
from collections import defaultdict  # noqa: E402

import numpy as np  # noqa: E402

from app.models import game_lines_mlb as G  # noqa: E402

SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_schedule_cache.json"
UMPIRE_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlb_umpire_cache.json"
MIN_GAMES_PER_UMPIRE = 40  # needs enough starts split in half to get a stable per-half average


def main():
    games = json.loads(SCHEDULE_PATH.read_text())
    umpires = json.loads(UMPIRE_CACHE_PATH.read_text())
    games = [g for g in games if g["game_type"] == "R" and g.get("home_score") is not None and g["id"] in umpires]
    games.sort(key=lambda g: (g["gameday"], g["game_number"], g["id"]))
    print(f"{len(games)} REG games with both a final score and a known HP umpire")

    by_umpire: dict[str, list[dict]] = defaultdict(list)
    for g in games:
        residual = (g["home_score"] + g["away_score"]) - G.expected_total(g["home_team"])
        by_umpire[umpires[g["id"]]].append({"residual": residual, "gameday": g["gameday"]})

    qualifying = {u: gs for u, gs in by_umpire.items() if len(gs) >= MIN_GAMES_PER_UMPIRE}
    print(f"{len(qualifying)} umpires with >= {MIN_GAMES_PER_UMPIRE} games (of {len(by_umpire)} total umpires seen)")

    first_half_tendency, second_half_actual = [], []
    for ump, glist in qualifying.items():
        glist = sorted(glist, key=lambda r: r["gameday"])
        mid = len(glist) // 2
        first_tendency = np.mean([r["residual"] for r in glist[:mid]])
        for r in glist[mid:]:
            first_half_tendency.append(first_tendency)
            second_half_actual.append(r["residual"])

    first_half_tendency = np.array(first_half_tendency)
    second_half_actual = np.array(second_half_actual)
    n = len(second_half_actual)
    print(f"n={n} second-half games predicted by their umpire's own first-half tendency")
    print(f"Spread of umpire first-half tendencies: std={first_half_tendency.std():.3f} runs "
          f"(range [{first_half_tendency.min():.2f}, {first_half_tendency.max():.2f}])")

    r = np.corrcoef(first_half_tendency, second_half_actual)[0, 1]
    print(f"\nOut-of-sample correlation, umpire's past tendency vs a NEW game's residual: {r:.4f}")
    print("(compare: temperature's residual correlation was r=0.083, out-wind's was r=0.069, both real)")

    # Simple linear check: does regressing second-half residual on first-half
    # tendency give a real (non-zero, sensible-magnitude) slope, or is the
    # relationship swamped by noise?
    if abs(first_half_tendency.std()) > 1e-9:
        slope = float(np.sum(first_half_tendency * second_half_actual) / np.sum(first_half_tendency * first_half_tendency))
        print(f"Through-origin slope (should be ~1.0 if the tendency perfectly predicted, 0.0 if pure noise): {slope:.4f}")

    # Bucketed sanity check
    print()
    print("Second-half actual residual by first-half-tendency bucket:")
    order = np.argsort(first_half_tendency)
    n_buckets = 5
    bucket_size = n // n_buckets
    for i in range(n_buckets):
        idx = order[i * bucket_size: (i + 1) * bucket_size if i < n_buckets - 1 else n]
        print(f"  bucket {i+1}/{n_buckets}: n={len(idx)}  avg_first_half_tendency={first_half_tendency[idx].mean():+.3f}  "
              f"avg_second_half_actual_residual={second_half_actual[idx].mean():+.3f}")


if __name__ == "__main__":
    main()
