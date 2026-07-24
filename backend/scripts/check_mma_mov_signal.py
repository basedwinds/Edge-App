"""Investigates whether a composed method-of-victory model has real signal
for the "method_of_victory" market (7-way fighter x method grid: A-by-KO/
TKO, A-by-submission, A-by-decision, B-by-KO/TKO, B-by-submission,
B-by-decision, plus a rare draw/NC bucket), currently shipping
model_prob=None entirely -- see mma_markets.py's NO_BASELINE_REASON.

Unlike distance/method-of-finish, this target is NOT symmetric in
fighter_a/fighter_b (it depends on WHO wins, not just how the fight ends)
-- so it can't reuse mma_features.py's symmetric feature helpers. Instead
this composes TWO already-validated pieces:
  1. P(fighter wins) -- the same walk-forward Elo + age adjustment as
     elo_mma.py/elo_service_mma.py (K=72, real, validated).
  2. P(method | that fighter wins) -- each fighter's OWN historical
     win-method mix (n_ko_wins/n_wins, n_sub_wins/n_wins, remainder =
     decision), a simple empirical conditional frequency, not a second ML
     fit.
Composed: P(A wins by KO/TKO) = P(A wins) * P(KO/TKO | A's own wins).
Naive baseline: population-wide per-class base rate (P(fighter A wins by
KO/TKO) = population rate of "winner won by KO/TKO" x 0.5, same idea for
every other class, draw/NC gets the population draw/NC rate) -- i.e. "the
composed model knows nothing fighter-specific."

Run: backend/.venv/Scripts/python.exe scripts/check_mma_mov_signal.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402
from app.models.baseline import elo_mma  # noqa: E402

FIRST_TEST_YEAR = 2010
MIN_FIGHTS_FOR_CONDITIONAL = 3  # need a few of THIS fighter's own wins before trusting their own method mix over the population rate


def _method_bucket(method):
    return mma_features._method_bucket(method)


def build_dataset():
    fights = ufc_data.load_fights()
    bios = ufc_data.load_fighter_bios()

    elo_state = elo_mma.MmaEloState()
    fighter_state: dict[str, mma_features._RollingFighterState] = {}

    def get_state(fid):
        if fid not in fighter_state:
            fighter_state[fid] = mma_features._RollingFighterState()
        return fighter_state[fid]

    # Population-wide base rates, computed once up front (naive baseline
    # shouldn't itself be walk-forward -- same convention as this app's
    # other "naive base rate" baselines, which use the FULL population,
    # not a rolling one, to stay a fair, static comparison point).
    class_counts = {"a_kotko": 0, "a_sub": 0, "a_dec": 0, "b_kotko": 0, "b_sub": 0, "b_dec": 0, "other": 0}
    total_clean = 0
    for f in fights:
        if f["is_no_contest"]:
            continue
        total_clean += 1
        if f["is_draw"] or f["winner_id"] is None:
            class_counts["other"] += 1
            continue
        bucket = _method_bucket(f.get("method"))
        if bucket is None:
            class_counts["other"] += 1
            continue
        winner_is_a = f["winner_id"] == f["fighter_a_id"]
        prefix = "a" if winner_is_a else "b"
        key = {"kotko": f"{prefix}_kotko", "submission": f"{prefix}_sub", "decision": f"{prefix}_dec"}[bucket]
        class_counts[key] += 1
    baseline_dist = {k: v / total_clean for k, v in class_counts.items()}
    print(f"Population base rates (n={total_clean}): {baseline_dist}\n")

    rows = []
    for f in fights:
        fight_date = dt.date.fromisoformat(f["event_date"]) if f["event_date"] else None
        a_id, b_id = f["fighter_a_id"], f["fighter_b_id"]
        a_bio, b_bio = bios.get(a_id, {}), bios.get(b_id, {})
        a_dob, b_dob = mma_features.parse_dob(a_bio.get("dob")), mma_features.parse_dob(b_bio.get("dob"))
        a_age = (fight_date - a_dob).days / 365.25 if (fight_date and a_dob) else None
        b_age = (fight_date - b_dob).days / 365.25 if (fight_date and b_dob) else None

        a_r, b_r = elo_state.get(a_id), elo_state.get(b_id)
        p_a_win = elo_mma.win_prob(a_r + elo_mma.age_adjustment_elo(a_age), b_r + elo_mma.age_adjustment_elo(b_age))

        a_st, b_st = get_state(a_id), get_state(b_id)
        a_snap, b_snap = a_st.snapshot(fight_date), b_st.snapshot(fight_date)

        has_target = f["went_the_distance"] is not None and not f["is_no_contest"]
        if has_target:
            def cond_rate(snap, key):
                wins = (snap["win_rate"] or 0) * (snap["experience"] or 0)
                if wins < MIN_FIGHTS_FOR_CONDITIONAL:
                    return None
                return snap[key] / snap["win_rate"] if snap["win_rate"] else None

            a_ko_given_win = cond_rate(a_snap, "ko_win_rate")
            a_sub_given_win = cond_rate(a_snap, "sub_win_rate")
            b_ko_given_win = cond_rate(b_snap, "ko_win_rate")
            b_sub_given_win = cond_rate(b_snap, "sub_win_rate")

            rows.append({
                "year": int(f["event_date"][:4]),
                "p_a_win": p_a_win,
                "a_ko_given_win": a_ko_given_win, "a_sub_given_win": a_sub_given_win,
                "b_ko_given_win": b_ko_given_win, "b_sub_given_win": b_sub_given_win,
                "is_draw": f["is_draw"], "winner_id": f["winner_id"], "fighter_a_id": a_id,
                "method_bucket": _method_bucket(f.get("method")),
            })

        # Update rolling state + Elo AFTER featurizing (walk-forward, no leakage)
        if not f["is_no_contest"]:
            a_raw = b_raw = None  # sig_str/td not needed for this check
            if f["is_draw"]:
                a_st.apply_result(fight_date, None, f.get("method"), f["went_the_distance"], None, None)
                b_st.apply_result(fight_date, None, f.get("method"), f["went_the_distance"], None, None)
            elif f["winner_id"] is not None:
                winner_is_a = f["winner_id"] == a_id
                a_st.apply_result(fight_date, winner_is_a, f.get("method"), f["went_the_distance"], None, None)
                b_st.apply_result(fight_date, not winner_is_a, f.get("method"), f["went_the_distance"], None, None)
            elo_mma.update_ratings(elo_state, a_id, b_id, f["winner_id"], f["is_draw"])

    return rows, baseline_dist


CLASSES = ["a_kotko", "a_sub", "a_dec", "b_kotko", "b_sub", "b_dec", "other"]


def composed_proba(row, baseline_dist) -> dict:
    p_a = row["p_a_win"]
    p_b = 1.0 - p_a

    # Fall back to the population's OWN conditional split (baseline's
    # a_kotko / (a_kotko+a_sub+a_dec) etc.) when a fighter doesn't have
    # enough of their own wins yet -- never a hand-guessed number.
    pop_a_win_total = baseline_dist["a_kotko"] + baseline_dist["a_sub"] + baseline_dist["a_dec"]
    pop_ko_given_win = baseline_dist["a_kotko"] / pop_a_win_total if pop_a_win_total else 1 / 3
    pop_sub_given_win = baseline_dist["a_sub"] / pop_a_win_total if pop_a_win_total else 1 / 3

    a_ko = row["a_ko_given_win"] if row["a_ko_given_win"] is not None else pop_ko_given_win
    a_sub = row["a_sub_given_win"] if row["a_sub_given_win"] is not None else pop_sub_given_win
    a_dec = max(0.0, 1.0 - a_ko - a_sub)

    b_ko = row["b_ko_given_win"] if row["b_ko_given_win"] is not None else pop_ko_given_win
    b_sub = row["b_sub_given_win"] if row["b_sub_given_win"] is not None else pop_sub_given_win
    b_dec = max(0.0, 1.0 - b_ko - b_sub)

    other = baseline_dist["other"]
    scale = 1.0 - other  # a_* and b_* buckets share the remaining probability mass

    return {
        "a_kotko": p_a * a_ko * scale, "a_sub": p_a * a_sub * scale, "a_dec": p_a * a_dec * scale,
        "b_kotko": p_b * b_ko * scale, "b_sub": p_b * b_sub * scale, "b_dec": p_b * b_dec * scale,
        "other": other,
    }


def actual_class(row) -> str:
    if row["is_draw"] or row["winner_id"] is None or row["method_bucket"] is None:
        return "other"
    winner_is_a = row["winner_id"] == row["fighter_a_id"]
    prefix = "a" if winner_is_a else "b"
    return {"kotko": f"{prefix}_kotko", "submission": f"{prefix}_sub", "decision": f"{prefix}_dec"}[row["method_bucket"]]


def main():
    rows, baseline_dist = build_dataset()
    print(f"Total fights: {len(rows)}\n")

    test_years = sorted(set(r["year"] for r in rows if r["year"] >= FIRST_TEST_YEAR))
    year_results = []
    for year in test_years:
        test_rows = [r for r in rows if r["year"] == year]
        if not test_rows:
            continue
        model_terms, baseline_terms = [], []
        model_correct = baseline_correct = 0
        for row in test_rows:
            actual = actual_class(row)
            actual_vec = {c: (1.0 if c == actual else 0.0) for c in CLASSES}
            model_proba = composed_proba(row, baseline_dist)
            model_terms.append(sum((model_proba[c] - actual_vec[c]) ** 2 for c in CLASSES))
            baseline_terms.append(sum((baseline_dist[c] - actual_vec[c]) ** 2 for c in CLASSES))
            model_correct += max(model_proba, key=model_proba.get) == actual
            baseline_correct += max(baseline_dist, key=baseline_dist.get) == actual
        n = len(test_rows)
        year_results.append({
            "year": year, "n": n,
            "model_brier": sum(model_terms) / n, "baseline_brier": sum(baseline_terms) / n,
            "model_acc": model_correct / n, "baseline_acc": baseline_correct / n,
        })

    n_total = sum(r["n"] for r in year_results)
    model_brier = sum(r["model_brier"] * r["n"] for r in year_results) / n_total
    baseline_brier = sum(r["baseline_brier"] * r["n"] for r in year_results) / n_total
    model_acc = sum(r["model_acc"] * r["n"] for r in year_results) / n_total
    baseline_acc = sum(r["baseline_acc"] * r["n"] for r in year_results) / n_total

    print(f"Walk-forward test fights ({test_years[0]}-{test_years[-1]}): {n_total}\n")
    print(f"{'Model':<35}{'Brier (7-way)':>15}{'Accuracy':>12}")
    print(f"{'Elo x own-method-mix (composed)':<35}{model_brier:>15.4f}{model_acc:>12.4f}")
    print(f"{'Naive (population base rate)':<35}{baseline_brier:>15.4f}{baseline_acc:>12.4f}")

    brier_wins = sum(1 for r in year_results if r["model_brier"] < r["baseline_brier"])
    acc_wins = sum(1 for r in year_results if r["model_acc"] > r["baseline_acc"])
    print(f"\nModel beat baseline Brier in {brier_wins}/{len(year_results)} yearly folds, accuracy in {acc_wins}/{len(year_results)}.")

    print("\nYear-by-year:")
    for r in year_results:
        print(f"  {r['year']}: model_acc={r['model_acc']:.4f}  baseline_acc={r['baseline_acc']:.4f}  "
              f"model_brier={r['model_brier']:.4f}  baseline_brier={r['baseline_brier']:.4f}  n={r['n']}")


if __name__ == "__main__":
    main()
