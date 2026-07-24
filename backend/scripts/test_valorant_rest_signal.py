"""Throwaway experiment (not wired into production): tests whether a team's
real REST (days since its own last real series) predicts anything beyond
Elo alone. See test_cs2_h2h_signal.py's own docstring for the shared
rationale (data already in hand, no new scraping needed) and
test_cs2_rest_signal.py's own docstring for the two competing real
hypotheses this tests (short rest hurts vs helps; long layoffs hurt vs
don't matter).

Mirrors elo_valorant.py::update_ratings exactly for the underlying rating
walk (per-map updates using the real maps_won_a/maps_won_b split, same as
derive_valorant_elo_constants.py/test_valorant_h2h_signal.py) -- the rest
adjustment only touches the PREDICTION for a match (a temporary rating-point
offset), never the persistent Elo state."""
import datetime as dt
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


def match_date(m) -> dt.date:
    raw = m.get("estimated_start_time") or m["match_date"]
    return dt.date.fromisoformat(raw[:10])


def prob_series_win_a(a_r, b_r, best_of):
    map_p = map_win_prob(a_r, b_r)
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def run_walkforward(matches, points_per_day, cap_days):
    ratings = {}
    last_played = {}
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
        date = match_date(m)
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)

        if points_per_day != 0:
            def rest_bonus(team):
                prior_date = last_played.get(team)
                if prior_date is None:
                    return 0.0
                rest_days = (date - prior_date).days
                return points_per_day * min(max(rest_days, 0), cap_days)

            adj_a_r = a_r + rest_bonus(team_a)
            adj_b_r = b_r + rest_bonus(team_b)
        else:
            adj_a_r, adj_b_r = a_r, b_r

        preds.append(prob_series_win_a(adj_a_r, adj_b_r, best_of))
        outcomes.append(1.0 if winner == "team_a" else 0.0)

        actual_a = 1.0 if winner == "team_a" else 0.0
        if maps_a is not None and maps_b is not None and (maps_a + maps_b) > 0:
            for _ in range(maps_a):
                apply_one_map(team_a, team_b, 1.0)
            for _ in range(maps_b):
                apply_one_map(team_a, team_b, 0.0)
        else:
            apply_one_map(team_a, team_b, actual_a)

        last_played[team_a] = date
        last_played[team_b] = date

    return preds, outcomes


def main():
    matches = load_matches()
    print(f"{len(matches)} real matches loaded")

    baseline_preds, baseline_outcomes = run_walkforward(matches, 0, 0)
    baseline_brier = brier_score(baseline_preds[WARMUP:], baseline_outcomes[WARMUP:])
    print(f"\nBaseline (shipped, no rest adjustment): Brier = {baseline_brier:.5f}")

    print(f"\n{'points/day':>10}  {'cap_days':>8}  {'Brier':>10}  {'vs baseline':>12}")
    for points_per_day in (5, 8, 10, 12, 15, 18, 20, 25):
        for cap_days in (1, 2, 3, 4):
            preds, outcomes = run_walkforward(matches, points_per_day, cap_days)
            b = brier_score(preds[WARMUP:], outcomes[WARMUP:])
            diff = b - baseline_brier
            print(f"{points_per_day:>10}  {cap_days:>8}  {b:>10.5f}  {diff:>+12.5f}")


if __name__ == "__main__":
    main()
