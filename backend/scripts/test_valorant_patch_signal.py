"""Throwaway experiment (not wired into production): tests whether giving a
team's first few post-patch-change matches a BOOSTED K (their pre-patch
rating is now partially stale, so the first real post-patch results should
count for more) improves walk-forward Brier vs. the current, already-shipped
flat-K per-map model. Real patch history from
liquipedia.net/valorant/Patches (153 patches, 2020-2025, see
valorant_patches.json), matched to each real historical match by date.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.valorant_data import infer_best_of_from_score  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_valorant import BASE_RATING, K as SHIPPED_K, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "valorant_historical_match_cache.json"
PATCHES_PATH = Path(__file__).resolve().parent.parent / "valorant_patches.json"
WARMUP = 500


def load_patches():
    patches = json.loads(PATCHES_PATH.read_text(encoding="utf-8"))
    patches.sort(key=lambda p: p["date"])
    return patches


def patch_era_for_date(date: str, patches: list[dict]) -> str:
    """The LATEST patch whose date <= this match's date."""
    era = patches[0]["patch"]
    for p in patches:
        if p["date"] <= date:
            era = p["patch"]
        else:
            break
    return era


def load_matches():
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r["match_date"] >= "2020-01-01"]
    for r in rows:
        if not r.get("best_of"):
            r["best_of"] = infer_best_of_from_score(r.get("maps_won_a"), r.get("maps_won_b"))
    rows = [r for r in rows if r.get("best_of") and r.get("winner")]
    rows.sort(key=lambda r: r.get("estimated_start_time") or r["match_date"])
    return rows


def prob_series_win_a(a_r, b_r, best_of):
    map_p = map_win_prob(a_r, b_r)
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run_walkforward(matches, patches, k_base, boost_multiplier, boost_games):
    """boost_multiplier=1.0/boost_games=0 reproduces the EXISTING shipped
    behavior exactly (no patch awareness) -- used as the real baseline."""
    ratings = {}
    last_era = {}
    games_since_patch = {}
    preds, outcomes = [], []

    def effective_k(team):
        if boost_games <= 0:
            return k_base
        if games_since_patch.get(team, 999) < boost_games:
            return k_base * boost_multiplier
        return k_base

    def apply_one_map(team_a, team_b, actual_a):
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        p_a = map_win_prob(a_r, b_r)
        k_a, k_b = effective_k(team_a), effective_k(team_b)
        delta_a = k_a * (actual_a - p_a)
        delta_b = k_b * (actual_a - p_a)
        ratings[team_a] = a_r + delta_a
        ratings[team_b] = b_r - delta_b
        games_since_patch[team_a] = games_since_patch.get(team_a, 0) + 1
        games_since_patch[team_b] = games_since_patch.get(team_b, 0) + 1

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        maps_a, maps_b = m.get("maps_won_a"), m.get("maps_won_b")
        date = m.get("match_date", "")
        era = patch_era_for_date(date, patches)
        for team in (team_a, team_b):
            if last_era.get(team) is not None and last_era.get(team) != era:
                games_since_patch[team] = 0  # reset -- this team's next match is its first on the new patch
            last_era[team] = era

        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        preds.append(prob_series_win_a(a_r, b_r, best_of))
        outcomes.append(1.0 if winner == "team_a" else 0.0)

        actual_a = 1.0 if winner == "team_a" else 0.0
        if maps_a is not None and maps_b is not None and (maps_a + maps_b) > 0:
            for _ in range(maps_a):
                apply_one_map(team_a, team_b, 1.0)
            for _ in range(maps_b):
                apply_one_map(team_a, team_b, 0.0)
        else:
            apply_one_map(team_a, team_b, actual_a)

    return preds, outcomes


def main():
    matches = load_matches()
    patches = load_patches()
    print(f"{len(matches)} matches, {len(patches)} patches loaded")

    baseline_preds, baseline_outcomes = run_walkforward(matches, patches, SHIPPED_K, 1.0, 0)
    baseline_brier = brier_score(baseline_preds[WARMUP:], baseline_outcomes[WARMUP:])
    print(f"\nBaseline (shipped, no patch awareness), K={SHIPPED_K}: Brier = {baseline_brier:.5f}")

    print(f"\n{'boost x':>8}  {'boost games':>12}  {'Brier':>10}  {'vs baseline':>12}")
    for boost_mult in (1.25, 1.5, 2.0, 3.0):
        for boost_games in (1, 3, 5):
            preds, outcomes = run_walkforward(matches, patches, SHIPPED_K, boost_mult, boost_games)
            b = brier_score(preds[WARMUP:], outcomes[WARMUP:])
            diff = b - baseline_brier
            print(f"{boost_mult:>8}  {boost_games:>12}  {b:>10.5f}  {diff:>+12.5f}")


if __name__ == "__main__":
    main()
