"""Throwaway experiment (not wired into production): tests whether blending
in a team pair's REAL prior head-to-head series record improves walk-forward
Brier vs. the current, already-shipped Elo-only model. Elo assumes
transitivity (if A > B and B > C then A > C) -- a real head-to-head signal
can capture a genuine non-transitive matchup effect (e.g. team A's style
just beats team B's, independent of both teams' overall level) that Elo
alone has no way to express.

Methodology: for each match, before scoring it, look up how many real prior
series this exact pair has played (either order) and team_a's real win count
in those. Blend the Elo-implied series-win probability with the real h2h win
rate via Bayesian shrinkage -- treat the Elo probability as a pseudo-prior
worth PRIOR_WEIGHT pseudo-observations, and the real h2h wins/total as actual
observations:

    blended = (elo_prob * PRIOR_WEIGHT + h2h_wins_a) / (PRIOR_WEIGHT + h2h_total)

PRIOR_WEIGHT -> infinity reproduces the existing shipped Elo-only behavior
exactly (h2h has zero influence) -- used as the real baseline. Smaller
PRIOR_WEIGHT lets real head-to-head history pull the prediction away from
Elo more aggressively. Same per-series (not per-map) update rule already
shipped for CS2 specifically (see elo_cs2.py -- per-map was tried and
rejected for this title)."""
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


def prob_series_win_a(a_r, b_r, best_of):
    map_p = map_win_prob(a_r, b_r)
    dist = series_score_distribution(map_p, best_of)
    return sum(p for (a, b), p in dist.items() if a > b)


def h2h_key(team_a, team_b):
    return tuple(sorted((team_a, team_b)))


def run_walkforward(matches, prior_weight, min_h2h=1):
    """prior_weight=None reproduces the EXISTING shipped Elo-only behavior
    exactly (no h2h blending) -- used as the real baseline. min_h2h is the
    real minimum number of prior meetings required before h2h gets ANY
    influence (below that, h2h_wins_a/h2h_total is too noisy to trust, e.g.
    a single prior meeting gives a degenerate 100%/0% "rate")."""
    ratings = {}
    h2h = {}  # key -> [wins for the alphabetically-first team, total]
    preds, outcomes = [], []

    for m in matches:
        team_a, team_b, best_of, winner = m["team_a"], m["team_b"], m["best_of"], m["winner"]
        a_r = ratings.get(team_a, BASE_RATING)
        b_r = ratings.get(team_b, BASE_RATING)
        elo_p = prob_series_win_a(a_r, b_r, best_of)

        key = h2h_key(team_a, team_b)
        first_team = key[0]
        wins_first, total = h2h.get(key, (0, 0))

        if prior_weight is not None and total >= min_h2h:
            # Reorient the real h2h record onto (team_a, team_b) for THIS match.
            wins_a = wins_first if team_a == first_team else (total - wins_first)
            pred = (elo_p * prior_weight + wins_a) / (prior_weight + total)
        else:
            pred = elo_p

        preds.append(pred)
        outcomes.append(1.0 if winner == "team_a" else 0.0)

        map_p = map_win_prob(a_r, b_r)
        actual_a = 1.0 if winner == "team_a" else 0.0
        delta = SHIPPED_K * (actual_a - map_p)
        ratings[team_a] = a_r + delta
        ratings[team_b] = b_r - delta

        a_won_series = winner == "team_a"
        first_won = a_won_series if team_a == first_team else (not a_won_series)
        h2h[key] = (wins_first + (1 if first_won else 0), total + 1)

    return preds, outcomes


def main():
    matches = load_matches()
    print(f"{len(matches)} real matches loaded")

    # How much real h2h depth actually exists? (pairs with >=1/>=2/>=3 prior meetings)
    h2h = {}
    depth_counts = {1: 0, 2: 0, 3: 0, 5: 0}
    for m in matches:
        key = h2h_key(m["team_a"], m["team_b"])
        _, total = h2h.get(key, (0, 0))
        for d in depth_counts:
            if total >= d:
                depth_counts[d] += 1
        wins_first, total = h2h.get(key, (0, 0))
        first_team = key[0]
        a_won = m["winner"] == "team_a"
        first_won = a_won if m["team_a"] == first_team else (not a_won)
        h2h[key] = (wins_first + (1 if first_won else 0), total + 1)
    print(f"Matches with >=1/>=2/>=3/>=5 prior real h2h meetings for this exact pair: "
          f"{depth_counts[1]}/{depth_counts[2]}/{depth_counts[3]}/{depth_counts[5]} of {len(matches)}")

    baseline_preds, baseline_outcomes = run_walkforward(matches, None)
    baseline_brier = brier_score(baseline_preds[WARMUP:], baseline_outcomes[WARMUP:])
    print(f"\nBaseline (shipped, Elo-only): Brier = {baseline_brier:.5f}")

    print(f"\n{'prior_weight':>12}  {'min_h2h':>8}  {'Brier':>10}  {'vs baseline':>12}")
    for prior_weight in (3, 4, 5, 6, 7, 8, 9, 10, 12, 14):
        for min_h2h in (1,):
            preds, outcomes = run_walkforward(matches, prior_weight, min_h2h)
            b = brier_score(preds[WARMUP:], outcomes[WARMUP:])
            diff = b - baseline_brier
            print(f"{prior_weight:>12}  {min_h2h:>8}  {b:>10.5f}  {diff:>+12.5f}")


if __name__ == "__main__":
    main()
