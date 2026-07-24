"""Investigates whether a round-of-finish model (extending the validated
went-the-distance/method-of-finish work) has real signal for the "rounds"
market ("ends before round N?" ladder), currently shipping model_prob=None
entirely -- see mma_markets.py's NO_BASELINE_REASON.

Target: round_of_finish (the round the fight actually ended in -- 1..
scheduled_rounds, where a decision's round IS scheduled_rounds, same field
ufcstats always records regardless of method). Reuses the SAME rolling
KO/sub finish/loss-rate features that already validated for method-of-
finish (mma_features.py's METHOD_MODEL_NUMERIC_FEATURES), since the two
questions are closely related ("how does this fight end" vs "when does it
end"). Multinomial logreg, one class per round number (1..5), trained
across BOTH 3-round and 5-round fights together (scheduled_rounds is
itself a feature, same as the distance/method models -- lets the model
learn the different distributions rather than needing two separate fits).

Naive baseline: per-scheduled_rounds base rate (P(round=k) computed
separately for 3-round vs 5-round fights from the training set), same
"naive base rate, not a coin flip" discipline as every other check in this
app.

Downstream use if real: P(ends before round N) = sum of P(round=k) for
k < N -- directly answers the "rounds" market's actual question.

Run: backend/.venv/Scripts/python.exe scripts/check_mma_rounds_signal.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402

FIRST_TEST_YEAR = 2010
MIN_TRAIN_ROWS = 300


def build_dataset() -> pd.DataFrame:
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    feature_rows = mma_features.build_feature_rows(fights, raw, bios)

    fight_round_by_id = {f["id"]: f.get("round") for f in fights}

    rows = []
    for r in feature_rows:
        round_of_finish = fight_round_by_id.get(r["fight_id"])
        if round_of_finish is None or r["scheduled_rounds"] is None:
            continue
        if round_of_finish > r["scheduled_rounds"]:
            continue  # data glitch guard -- round can't exceed the scheduled cap
        row = mma_features.to_symmetric_method_features(r)
        row["year"] = int(r["event_date"][:4])
        row["round_of_finish"] = round_of_finish
        rows.append(row)

    return pd.DataFrame(rows)


def main():
    df = build_dataset()
    print(f"Total fights with a clean round-of-finish target: {len(df)}")
    print(f"Round distribution by scheduled_rounds:")
    print(df.groupby("scheduled_rounds")["round_of_finish"].value_counts(normalize=True).sort_index())
    print()

    feature_cols = mma_features.METHOD_MODEL_NUMERIC_FEATURES
    test_years = sorted(y for y in df["year"].unique() if y >= FIRST_TEST_YEAR)

    year_rows = []
    for year in test_years:
        train = df[df["year"] < year].copy()
        test = df[df["year"] == year].copy()
        if len(train) < MIN_TRAIN_ROWS or len(test) == 0:
            continue

        medians = train[feature_cols].median()
        train_f = train[feature_cols].fillna(medians)
        test_f = test[feature_cols].fillna(medians)

        model = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))
        model.fit(train_f, train["round_of_finish"])
        classes = list(model.classes_)
        proba = model.predict_proba(test_f)

        # Naive baseline: per-scheduled_rounds base rate from TRAIN only.
        baseline_by_sched: dict[float, dict[int, float]] = {}
        for sched, grp in train.groupby("scheduled_rounds"):
            baseline_by_sched[sched] = grp["round_of_finish"].value_counts(normalize=True).to_dict()

        model_brier_terms, baseline_brier_terms = [], []
        model_correct = baseline_correct = 0
        test_rows = test.reset_index(drop=True)
        for row_idx in range(len(test_rows)):
            actual = test_rows.loc[row_idx, "round_of_finish"]
            sched = test_rows.loc[row_idx, "scheduled_rounds"]
            baseline_dist = baseline_by_sched.get(sched, {})

            actual_vec = [1.0 if c == actual else 0.0 for c in classes]
            model_brier_terms.append(sum((p - a) ** 2 for p, a in zip(proba[row_idx], actual_vec)))
            baseline_proba = [baseline_dist.get(c, 0.0) for c in classes]
            baseline_brier_terms.append(sum((p - a) ** 2 for p, a in zip(baseline_proba, actual_vec)))

            model_correct += classes[proba[row_idx].argmax()] == actual
            baseline_correct += (max(baseline_dist, key=baseline_dist.get) if baseline_dist else None) == actual

        n = len(test)
        year_rows.append({
            "year": year, "n": n,
            "model_brier": sum(model_brier_terms) / n,
            "baseline_brier": sum(baseline_brier_terms) / n,
            "model_acc": model_correct / n,
            "baseline_acc": baseline_correct / n,
        })

    n_total = sum(r["n"] for r in year_rows)
    model_brier = sum(r["model_brier"] * r["n"] for r in year_rows) / n_total
    baseline_brier = sum(r["baseline_brier"] * r["n"] for r in year_rows) / n_total
    model_acc = sum(r["model_acc"] * r["n"] for r in year_rows) / n_total
    baseline_acc = sum(r["baseline_acc"] * r["n"] for r in year_rows) / n_total

    print(f"Walk-forward test fights ({test_years[0] if year_rows else '-'}-{year_rows[-1]['year'] if year_rows else '-'}): {n_total}\n")
    print(f"{'Model':<28}{'Brier (multi)':>15}{'Accuracy':>12}")
    print(f"{'Multinomial logreg':<28}{model_brier:>15.4f}{model_acc:>12.4f}")
    print(f"{'Naive (per-sched base rate)':<28}{baseline_brier:>15.4f}{baseline_acc:>12.4f}")

    brier_wins = sum(1 for r in year_rows if r["model_brier"] < r["baseline_brier"])
    acc_wins = sum(1 for r in year_rows if r["model_acc"] > r["baseline_acc"])
    print(f"\nModel beat baseline Brier in {brier_wins}/{len(year_rows)} yearly folds, accuracy in {acc_wins}/{len(year_rows)}.")

    print("\nYear-by-year:")
    for r in year_rows:
        print(f"  {r['year']}: model_acc={r['model_acc']:.4f}  baseline_acc={r['baseline_acc']:.4f}  "
              f"model_brier={r['model_brier']:.4f}  baseline_brier={r['baseline_brier']:.4f}  n={r['n']}")

    # Downstream check: does the composed "ends before round N" ladder
    # ALSO beat naive at that specific, market-relevant question -- the
    # multinomial's own Brier improving doesn't automatically mean the
    # SUMMED ladder probability is well-calibrated (errors could offset).
    print("\n--- Downstream check: P(ends before round N) ladder, final-year holdout ---")
    last_year = test_years[-1]
    train = df[df["year"] < last_year].copy()
    test = df[df["year"] == last_year].copy()
    medians = train[feature_cols].median()
    model = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))
    model.fit(train[feature_cols].fillna(medians), train["round_of_finish"])
    classes = list(model.classes_)
    test_f = test[feature_cols].fillna(medians)
    proba = model.predict_proba(test_f)
    baseline_by_sched = {sched: grp["round_of_finish"].value_counts(normalize=True).to_dict() for sched, grp in train.groupby("scheduled_rounds")}

    for n_cutoff in (2, 3, 4, 5):
        model_terms, baseline_terms = [], []
        test_rows = test.reset_index(drop=True)
        for row_idx in range(len(test_rows)):
            sched = test_rows.loc[row_idx, "scheduled_rounds"]
            if n_cutoff > sched:
                continue  # not a real rung for this fight's own scheduled_rounds
            actual = 1.0 if test_rows.loc[row_idx, "round_of_finish"] < n_cutoff else 0.0
            model_p = sum(p for c, p in zip(classes, proba[row_idx]) if c < n_cutoff)
            baseline_dist = baseline_by_sched.get(sched, {})
            baseline_p = sum(v for k, v in baseline_dist.items() if k < n_cutoff)
            model_terms.append((model_p - actual) ** 2)
            baseline_terms.append((baseline_p - actual) ** 2)
        if not model_terms:
            continue
        print(f"  before round {n_cutoff}: model_brier={sum(model_terms)/len(model_terms):.4f}  "
              f"baseline_brier={sum(baseline_terms)/len(baseline_terms):.4f}  n={len(model_terms)}")


if __name__ == "__main__":
    main()
