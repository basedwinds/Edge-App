"""Throwaway experiment (not wired into production): tests whether a team's
real REST (days since its own last real series, computed from this app's
own already-cached match dates -- no new scraping needed, same "data already
in hand" shape as test_cs2_h2h_signal.py) predicts anything beyond Elo
alone. Two competing real hypotheses, tested empirically rather than
assumed: short rest could hurt (fatigue, travel, back-to-back series in a
tight group-stage schedule) OR help (momentum/hot streak); long layoffs
could hurt (rust, same category of effect this app's own MMA model
INVESTIGATED AND REJECTED for layoff/ring-rust -- see elo_mma.py's own
docstring) or simply not matter for CS2's own real tournament cadence.

Applied purely at PREDICTION time (a temporary rating-point adjustment for
THIS match's map_win_prob call only), not folded into the Elo state itself
-- rest is a property of the moment a match is played, not a persistent
skill signal like h2h or roster tenure, so it must not carry over into how
ratings themselves update."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.calibration import brier_score  # noqa: E402
from app.models.baseline.elo_cs2 import BASE_RATING, K as SHIPPED_K, map_win_prob, series_score_distribution  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MATCH_CACHE_PATH = DATA_DIR / "cs2_historical_match_cache.json"
WARMUP = 800


def load_matches():
    rows = json.loads(MATCH_CACHE_PATH.read_text(encoding="utf-8"))
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
    """points_per_day=0 reproduces the EXISTING shipped behavior exactly (no
    rest adjustment) -- used as the real baseline. Positive points_per_day
    means MORE rest helps (well-rested teams get a rating bonus, up to
    cap_days); negative means more rest hurts (rust)."""
    ratings = {}
    last_played = {}
    preds, outcomes = [], []

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
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

        map_p = map_win_prob(a_r, b_r)  # ratings themselves update off PURE elo, rest never persists
        actual_a = 1.0 if winner == "team_a" else 0.0
        delta = SHIPPED_K * (actual_a - map_p)
        ratings[team_a] = a_r + delta
        ratings[team_b] = b_r - delta
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
    for points_per_day in (10, 15, 20, 25, 30, 35, 40, 45, 50):
        for cap_days in (1, 2, 3, 4):
            preds, outcomes = run_walkforward(matches, points_per_day, cap_days)
            b = brier_score(preds[WARMUP:], outcomes[WARMUP:])
            diff = b - baseline_brier
            print(f"{points_per_day:>10}  {cap_days:>8}  {b:>10.5f}  {diff:>+12.5f}")


if __name__ == "__main__":
    main()
