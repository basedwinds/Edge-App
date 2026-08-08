"""Does the game patch matter for LoL match prediction? (Task #12, throwaway
experiment -- NOT wired into production.)

THE HYPOTHESIS. LoL ships a balance patch every ~2 weeks. If those patches move
the meta enough to change which teams are strong, then a team's Elo -- built
entirely on pre-patch results -- is partially stale the moment a patch lands,
and the model should be worse right after a patch than late in a patch cycle.
If that's true, the fix is to trust fresh post-patch results more (a boosted K
for a team's first few games on a new patch), which is exactly the adjustment
tested for Valorant in test_valorant_patch_signal.py.

THIS RUNS TWO TESTS, DELIBERATELY, because they answer different questions and
the second one alone would be misleading.

  TEST 1 -- DESCRIPTIVE. Does the shipped model actually get WORSE just after a
  patch? Bucket post-warmup matches by days (and games) since the patch landed
  and compare Brier within buckets. This asks whether the EFFECT EXISTS AT ALL,
  independent of any proposed fix.

  TEST 2 -- INTERVENTIONAL. Does the K-boost adjustment improve walk-forward
  Brier vs the shipped flat-K model? This asks whether ONE PARTICULAR FIX helps.

Why both: a null on Test 2 alone is ambiguous -- it cannot distinguish "patches
don't matter" from "patches matter but a K-boost is the wrong lever." A null on
BOTH is a real, informative null. A split verdict (Test 1 positive, Test 2 null)
would mean the effect is real and worth a different adjustment, and that is a
materially different conclusion to report.

BASELINE IS WHAT SHIPS. boost_multiplier=1.0/boost_games=0 reproduces the
shipped per-map update rule exactly (elo_lol.update_ratings), at the shipped
K=24, on the real 5,604-match gol.gg/Leaguepedia crawl. This matters: benchmarking
against a hand-rolled stand-in inflated a result twice in this project already.

SCOPE. This measures the TEAM Elo core, which is what predict_series consumes
(state.get() is pure team rating -- the 16.4%-coverage player blend is applied
above this in elo_service_lol). If the team core shows nothing, the blend on top
of it will not manufacture a patch effect, so a null here is a null overall. A
POSITIVE result here would need re-testing with the blend before shipping.

PATCH DATES come from data/lol_patches.json (86 patches, 2023-01 -> 2026-07),
built by fetch_lol_patches.py from Riot's Data Dragon CDN. They are accurate to
+/- 1 day; see that script for the verification against known release dates.

===========================================================================
RESULT, 2026-08-08: REJECTED. Patch version does not measurably matter for
LoL match prediction. Both tests are null, and they fail in ways that rule out
different explanations, which is why both were run.

BASELINE FIDELITY FIRST -- this reimplementation reproduces the shipped model
to 4 decimal places: Brier 0.20734 here vs the 0.20727 recorded in
elo_lol.update_ratings' own docstring, on the same 5,604-match cache. The gap
(0.00007) is 3% of one standard error. So the thing being compared against is
genuinely what ships, not a stand-in.

TEST 1 -- the effect does not exist, and what structure there is runs BACKWARDS:

     days since patch      n     Brier    +/- SE   accuracy
                 0-3d   1491   0.20677   0.00500     67.87%
                 4-7d   1129   0.20274   0.00549     69.62%
                8-14d   2178   0.20936   0.00394     67.31%
               15-99d    306   0.21277   0.01113     66.34%

The model is at its BEST in the days right after a patch and at its WORST late
in a cycle -- the reverse of the hypothesis, and every bucket sits within ~1-2
SE of the others. The weak 15-99d bucket is not patch staleness: LoL's max gap
is 36 days (the year-end break), so that bucket is mostly offseason matches
around roster churn, a different effect entirely. Games-since-patch is likewise
non-monotonic (0 games 0.20349, 1-2 games 0.21781, 6+ games 0.19943): if
ratings were stale at a patch boundary the FIRST game would be worst and it
would improve monotonically. It doesn't, so there is no mechanism to fit.

TEST 2 -- the intervention is a dose-response curve pointing the wrong way:

    boost x1.25 -> -0.00019    boost x1.5 -> +0.00009
    boost x2.0  -> +0.00154    boost x3.0  -> +0.00639

Every boost at or above 1.5x makes the model WORSE, monotonically in the size
of the boost. Trusting fresh post-patch results more actively destroys accuracy.
The nominal "best" config (x1.25 over 5 games) gains 0.00019 Brier against a
baseline standard error of 0.00262 -- 7% of one SE, i.e. indistinguishable from
zero -- and it sits at the SMALLEST boost on the grid, improving as the boost
shrinks toward 1.0, which is just the shipped model. That is the grid-edge
non-convergence that got the racing attrition fit rejected; the optimum is "no
adjustment."

WHY THIS IS PLAUSIBLE rather than a measurement failure. Pro teams play multiple
matches per week, so a 14-day patch cycle already gives Elo several observations
to re-converge on its own; K=24 per map over a Bo3 moves a rating meaningfully
within one series. Patch churn is real for solo-queue win rates but pro teams
adapt inside the same window the ratings do. Note also that LoL's team Elo is
already the strongest of this app's three esports titles, which is the same
reason player-level ratings added so little here (see elo_lol.K_PLAYER).

SCOPE OF THE NULL: measured on the team Elo core. Since a null here cannot be
rescued by the 16.4%-coverage player blend layered above it, this closes the
question for LoL. Do not re-run without a materially better patch-impact
measure (e.g. real champion/meta-shift magnitude per patch), which this app has
no free source for.
===========================================================================
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.baseline.elo_lol import (  # noqa: E402
    BASE_RATING, K as SHIPPED_K, RATING_CLAMP, map_win_prob, series_score_distribution,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
MATCH_CACHE = DATA_DIR / "lol_historical_match_cache.json"
PATCHES = DATA_DIR / "lol_patches.json"
WARMUP = 500

# Days-since-patch buckets. LoL's median gap is 14d, so ">14" is "late cycle".
DAY_BUCKETS = [(0, 3), (4, 7), (8, 14), (15, 99)]


def load_patches() -> list[dict]:
    p = json.loads(PATCHES.read_text(encoding="utf-8"))
    p.sort(key=lambda x: x["date"])
    return p


def patch_for_date(date: str, patches: list[dict]) -> tuple[str, str]:
    """(patch label, that patch's release date) for the LATEST patch on or
    before this match's date."""
    era, era_date = patches[0]["patch"], patches[0]["date"]
    for p in patches:
        if p["date"] <= date:
            era, era_date = p["patch"], p["date"]
        else:
            break
    return era, era_date


def days_between(a: str, b: str) -> int:
    import datetime
    return (datetime.date.fromisoformat(a) - datetime.date.fromisoformat(b)).days


def load_matches() -> list[dict]:
    rows = json.loads(MATCH_CACHE.read_text(encoding="utf-8"))
    rows = [r for r in rows
            if r.get("match_date") and r.get("best_of") and r.get("winner") in ("team_a", "team_b")]
    rows.sort(key=lambda r: (r.get("estimated_start_time") or r["match_date"], r.get("source_match_id") or ""))
    return rows


def prob_series_win_a(a_r: float, b_r: float, best_of: int) -> float:
    dist = series_score_distribution(map_win_prob(a_r, b_r), best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def clamp(x: float) -> float:
    return max(-RATING_CLAMP, min(BASE_RATING + RATING_CLAMP, x))


def run_walkforward(matches, patches, k_base, boost_multiplier, boost_games):
    """Mirrors elo_lol.update_ratings' per-map rule exactly. With
    boost_multiplier=1.0/boost_games=0 the effective K is k_base for every
    update, i.e. the shipped model."""
    ratings: dict[str, float] = {}
    last_era: dict[str, str] = {}
    games_since_patch: dict[str, int] = {}
    rows = []  # (pred, outcome, days_since_patch, games_since_patch_at_predict)

    def effective_k(team: str) -> float:
        if boost_games <= 0:
            return k_base
        return k_base * boost_multiplier if games_since_patch.get(team, 10**9) < boost_games else k_base

    def apply_one_map(team_a, team_b, actual_a):
        a_r, b_r = ratings.get(team_a, BASE_RATING), ratings.get(team_b, BASE_RATING)
        p_a = map_win_prob(a_r, b_r)
        ratings[team_a] = clamp(a_r + effective_k(team_a) * (actual_a - p_a))
        ratings[team_b] = clamp(b_r - effective_k(team_b) * (actual_a - p_a))
        games_since_patch[team_a] = games_since_patch.get(team_a, 0) + 1
        games_since_patch[team_b] = games_since_patch.get(team_b, 0) + 1

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        date = m["match_date"]
        era, era_date = patch_for_date(date, patches)
        for team in (team_a, team_b):
            if last_era.get(team) is not None and last_era[team] != era:
                games_since_patch[team] = 0
            last_era[team] = era

        a_r, b_r = ratings.get(team_a, BASE_RATING), ratings.get(team_b, BASE_RATING)
        gsp = min(games_since_patch.get(team_a, 10**9), games_since_patch.get(team_b, 10**9))
        rows.append((prob_series_win_a(a_r, b_r, best_of),
                     1.0 if winner == "team_a" else 0.0,
                     days_between(date, era_date),
                     gsp))

        actual_a = 1.0 if winner == "team_a" else 0.0
        ma, mb = m.get("maps_won_a"), m.get("maps_won_b")
        if ma is not None and mb is not None and (ma + mb) > 0:
            for _ in range(ma):
                apply_one_map(team_a, team_b, 1.0)
            for _ in range(mb):
                apply_one_map(team_a, team_b, 0.0)
        else:
            apply_one_map(team_a, team_b, actual_a)

    return rows


def brier_with_se(rows) -> tuple[float, float, float, int]:
    """Brier, its standard error, accuracy, n. The SE is what stops a 0.002
    difference across buckets being read as a finding."""
    if not rows:
        return float("nan"), float("nan"), float("nan"), 0
    terms = [(p - o) ** 2 for p, o, *_ in rows]
    n = len(terms)
    mean = sum(terms) / n
    var = sum((t - mean) ** 2 for t in terms) / (n - 1) if n > 1 else 0.0
    acc = sum(1 for p, o, *_ in rows if (p >= 0.5) == (o == 1.0)) / n
    return mean, math.sqrt(var / n), acc, n


def main() -> None:
    matches = load_matches()
    patches = load_patches()
    print(f"{len(matches)} matches ({matches[0]['match_date']} -> {matches[-1]['match_date']}), "
          f"{len(patches)} patches ({patches[0]['date']} -> {patches[-1]['date']})")

    base_rows = run_walkforward(matches, patches, SHIPPED_K, 1.0, 0)[WARMUP:]
    b, se, acc, n = brier_with_se(base_rows)
    print(f"\nShipped model (K={SHIPPED_K}, no patch awareness): "
          f"Brier {b:.5f} +/- {se:.5f}, acc {acc:.2%}, n={n}")

    print("\n--- TEST 1: does the model get worse right after a patch? ---")
    print(f"{'days since patch':>18} {'n':>6} {'Brier':>9} {'+/- SE':>9} {'accuracy':>9}")
    for lo, hi in DAY_BUCKETS:
        sub = [r for r in base_rows if lo <= r[2] <= hi]
        bb, bse, bacc, bn = brier_with_se(sub)
        print(f"{f'{lo}-{hi}d':>18} {bn:>6} {bb:>9.5f} {bse:>9.5f} {bacc:>9.2%}")

    print(f"\n{'games since patch':>18} {'n':>6} {'Brier':>9} {'+/- SE':>9} {'accuracy':>9}")
    for lo, hi in [(0, 0), (1, 2), (3, 5), (6, 10**9)]:
        sub = [r for r in base_rows if lo <= r[3] <= hi]
        bb, bse, bacc, bn = brier_with_se(sub)
        label = f"{lo}" if lo == hi else (f"{lo}+" if hi > 10**8 else f"{lo}-{hi}")
        print(f"{label:>18} {bn:>6} {bb:>9.5f} {bse:>9.5f} {bacc:>9.2%}")

    print("\n--- TEST 2: does a post-patch K boost help? ---")
    print(f"{'boost x':>8} {'boost games':>12} {'Brier':>10} {'vs shipped':>12}")
    best = None
    for mult in (1.25, 1.5, 2.0, 3.0):
        for g in (1, 3, 5):
            rows = run_walkforward(matches, patches, SHIPPED_K, mult, g)[WARMUP:]
            bb, _, _, _ = brier_with_se(rows)
            diff = bb - b
            if best is None or bb < best[0]:
                best = (bb, mult, g)
            print(f"{mult:>8} {g:>12} {bb:>10.5f} {diff:>+12.5f}")
    print(f"\nbest config: boost x{best[1]} for {best[2]} games -> {best[0]:.5f} "
          f"({best[0] - b:+.5f} vs shipped, shipped SE {se:.5f})")


if __name__ == "__main__":
    main()
