"""Throwaway experiment (not wired into production): tests whether blending
in a team pair's REAL prior head-to-head SERIES record improves walk-forward
Brier vs. the current, already-shipped per-map-update Elo model. See
test_cs2_h2h_signal.py's own docstring for the full rationale (Elo assumes
transitivity, h2h captures a real non-transitive matchup effect Elo can't).

Mirrors elo_valorant.py::update_ratings exactly for the underlying rating
walk (per-map updates using the real maps_won_a/maps_won_b split, same as
derive_valorant_elo_constants.py) -- h2h blending only touches the final
PREDICTION for a match, not how ratings themselves update."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.valorant_data import infer_best_of_from_score  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_valorant import BASE_RATING, K as SHIPPED_K, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "valorant_historical_match_cache.json"
WARMUP = 500


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


def h2h_key(team_a, team_b):
    return tuple(sorted((team_a, team_b)))


def run_walkforward(matches, prior_weight, min_h2h=1):
    ratings = {}
    h2h = {}
    preds, outcomes = [], []

    def apply_one_map(team_a, team_b, actual_a):
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        p_a = map_win_prob(a_r, b_r)
        delta = SHIPPED_K * (actual_a - p_a)
        ratings[team_a] = a_r + delta
        ratings[team_b] = b_r - delta

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        maps_a, maps_b = m.get("maps_won_a"), m.get("maps_won_b")
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        elo_p = prob_series_win_a(a_r, b_r, best_of)

        key = h2h_key(team_a, team_b)
        first_team = key[0]
        wins_first, total = h2h.get(key, (0, 0))

        if prior_weight is not None and total >= min_h2h:
            wins_a = wins_first if team_a == first_team else (total - wins_first)
            pred = (elo_p * prior_weight + wins_a) / (prior_weight + total)
        else:
            pred = elo_p

        preds.append(pred)
        outcomes.append(1.0 if winner == "team_a" else 0.0)

        actual_a = 1.0 if winner == "team_a" else 0.0
        if maps_a is not None and maps_b is not None and (maps_a + maps_b) > 0:
            for _ in range(maps_a):
                apply_one_map(team_a, team_b, 1.0)
            for _ in range(maps_b):
                apply_one_map(team_a, team_b, 0.0)
        else:
            apply_one_map(team_a, team_b, actual_a)

        a_won_series = winner == "team_a"
        first_won = a_won_series if team_a == first_team else (not a_won_series)
        h2h[key] = (wins_first + (1 if first_won else 0), total + 1)

    return preds, outcomes


def main():
    matches = load_matches()
    print(f"{len(matches)} real matches loaded")

    h2h = {}
    depth_counts = {1: 0, 2: 0, 3: 0, 5: 0}
    for m in matches:
        key = h2h_key(m["team_a"], m["team_b"])
        wins_first, total = h2h.get(key, (0, 0))
        for d in depth_counts:
            if total >= d:
                depth_counts[d] += 1
        first_team = key[0]
        a_won = m["winner"] == "team_a"
        first_won = a_won if m["team_a"] == first_team else (not a_won)
        h2h[key] = (wins_first + (1 if first_won else 0), total + 1)
    print(f"Matches with >=1/>=2/>=3/>=5 prior real h2h meetings for this exact pair: "
          f"{depth_counts[1]}/{depth_counts[2]}/{depth_counts[3]}/{depth_counts[5]} of {len(matches)}")

    baseline_preds, baseline_outcomes = run_walkforward(matches, None)
    baseline_brier = brier_score(baseline_preds[WARMUP:], baseline_outcomes[WARMUP:])
    print(f"\nBaseline (shipped, Elo-only, per-map updates): Brier = {baseline_brier:.5f}")

    print(f"\n{'prior_weight':>12}  {'min_h2h':>8}  {'Brier':>10}  {'vs baseline':>12}")
    for prior_weight in (2, 4, 6, 8, 10, 12, 16, 24, 32, 48, 64):
        for min_h2h in (1, 2, 3):
            preds, outcomes = run_walkforward(matches, prior_weight, min_h2h)
            b = brier_score(preds[WARMUP:], outcomes[WARMUP:])
            diff = b - baseline_brier
            print(f"{prior_weight:>12}  {min_h2h:>8}  {b:>10.5f}  {diff:>+12.5f}")


if __name__ == "__main__":
    main()
