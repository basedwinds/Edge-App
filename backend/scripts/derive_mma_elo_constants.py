"""Grid-searches elo_mma.py's K-factor against this app's own walk-forward
Brier score on the full ufcstats.com historical scrape (17,560 fight-rows /
8,780 fights, 1993-2026 -- see scripts/build_ufc_fight_cache.py). Same
methodology as elo_mlb.py's K=5/SEASON_REGRESSION=0.25 derivation, adapted
for MMA's structural difference: no home/away side, no season structure to
regress ratings between (UFC runs continuously, no offseason).

Draws (65 real fights, see ufc_data.py's is_draw fix) update ratings as a
0.5/0.5 outcome, same convention as elo_mlb.py's rare-tie handling.
No-contests (91 fights) and any fight missing a real result are skipped
entirely -- they don't resolve a real skill question, nothing to learn from.

Run: backend/.venv/Scripts/python.exe scripts/derive_mma_elo_constants.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402
from app.models.baseline.elo_mma import BASE_RATING, age_adjustment_elo, win_prob  # noqa: E402

K_GRID = [55, 60, 65, 70, 72, 74, 76, 78, 80, 82, 85, 90, 95]


def run_walkforward(fights: list[dict], k: float) -> tuple[list[float], list[float]]:
    ratings: dict[str, float] = {}
    preds: list[float] = []
    outcomes: list[float] = []

    for f in fights:
        if f["is_no_contest"]:
            continue
        if f["winner_id"] is None and not f["is_draw"]:
            continue  # not yet fought / genuinely unresolved -- shouldn't appear in the historical cache, but defensive

        a_id, b_id = f["fighter_a_id"], f["fighter_b_id"]
        a_r = ratings.get(a_id, BASE_RATING)
        b_r = ratings.get(b_id, BASE_RATING)
        p_a = win_prob(a_r, b_r)

        if f["is_draw"]:
            actual_a = 0.5
        elif f["winner_id"] == a_id:
            actual_a = 1.0
        else:
            actual_a = 0.0

        preds.append(p_a)
        outcomes.append(actual_a)

        delta = k * (actual_a - p_a)
        ratings[a_id] = a_r + delta
        ratings[b_id] = b_r - delta

    return preds, outcomes


def main():
    fights = ufc_data.load_fights()
    print(f"Loaded {len(fights)} fights total")

    # Warm-up window: the first ~500 fights are almost all fresh/unrated
    # fighters (BASE_RATING vs BASE_RATING, p=0.5 uninformative) -- same
    # "let ratings warm up before scoring" convention this app's other Elo
    # grid searches use implicitly via multi-year walk-forward folds.
    # MMA has no natural season boundary, so this uses a fixed fight-count
    # warm-up instead.
    WARMUP = 1500

    print(f"\n{'K':>6}  {'Brier (all)':>12}  {'Brier (post-warmup)':>20}  {'LogLoss':>10}")
    results = []
    for k in K_GRID:
        preds, outcomes = run_walkforward(fights, k)
        b_all = brier_score(preds, outcomes)
        b_post = brier_score(preds[WARMUP:], outcomes[WARMUP:])
        ll_post = log_loss(preds[WARMUP:], outcomes[WARMUP:])
        results.append((k, b_all, b_post, ll_post))
        print(f"{k:>6}  {b_all:>12.5f}  {b_post:>20.5f}  {ll_post:>10.5f}")

    best = min(results, key=lambda r: r[2])
    print(f"\nBest K by post-warmup Brier: K={best[0]} (Brier={best[2]:.5f})")

    # Baseline comparison: naive "always predict 0.5" (no skill signal at
    # all) -- confirms the Elo model is actually learning something, not
    # just close to a coin flip by coincidence.
    preds_best, outcomes_best = run_walkforward(fights, best[0])
    naive_preds = [0.5] * len(outcomes_best[WARMUP:])
    naive_brier = brier_score(naive_preds, outcomes_best[WARMUP:])
    print(f"Naive 0.5 baseline Brier (post-warmup): {naive_brier:.5f}")

    accuracy = sum(
        1 for p, o in zip(preds_best[WARMUP:], outcomes_best[WARMUP:])
        if (p >= 0.5) == (o >= 0.5)
    ) / len(outcomes_best[WARMUP:])
    print(f"Accuracy at best K (post-warmup, draws count as a miss either way): {accuracy:.4f}")

    # Real, validated addition (see elo_mma.py's own docstring for the full
    # derivation/validation of age_adjustment_elo) -- reports the SAME
    # walk-forward Brier check but with the age adjustment applied to the
    # prediction, since that's what's actually shipped and serving live
    # moneyline rows now, not just the pure-Elo number above.
    bios = ufc_data.load_fighter_bios()
    preds_age, outcomes_age = [], []
    ratings_age: dict[str, float] = {}
    for i, f in enumerate(fights):
        if f["is_no_contest"]:
            continue
        if f["winner_id"] is None and not f["is_draw"]:
            continue
        a_id, b_id = f["fighter_a_id"], f["fighter_b_id"]
        a_r, b_r = ratings_age.get(a_id, BASE_RATING), ratings_age.get(b_id, BASE_RATING)
        fight_date = dt.date.fromisoformat(f["event_date"]) if f["event_date"] else None
        a_dob, b_dob = mma_features.parse_dob(bios.get(a_id, {}).get("dob")), mma_features.parse_dob(bios.get(b_id, {}).get("dob"))
        a_age = (fight_date - a_dob).days / 365.25 if (fight_date and a_dob) else None
        b_age = (fight_date - b_dob).days / 365.25 if (fight_date and b_dob) else None
        p_a_age = win_prob(a_r + age_adjustment_elo(a_age), b_r + age_adjustment_elo(b_age))
        actual_a = 0.5 if f["is_draw"] else (1.0 if f["winner_id"] == a_id else 0.0)
        if i >= WARMUP:
            preds_age.append(p_a_age)
            outcomes_age.append(actual_a)
        delta = best[0] * (actual_a - win_prob(a_r, b_r))
        ratings_age[a_id] = a_r + delta
        ratings_age[b_id] = b_r - delta

    print(f"\nWith the validated age adjustment (see elo_mma.py::age_adjustment_elo): "
          f"Brier = {brier_score(preds_age, outcomes_age):.5f} "
          f"(vs. {best[2]:.5f} without -- this is what's actually shipped/live now)")


if __name__ == "__main__":
    main()
