"""Walk-forward validates the method-of-finish (KO/TKO vs Submission vs
Decision) model on real ufcstats.com data -- extends the went-the-distance
work with method-specific finish/loss rates (offensive: how often a
fighter wins by KO/TKO or submission; defensive: how often they're
finished that way, a real durability/chin proxy). First checked as a
standalone investigation (scripts/check_mma_method_signal.py) before this
production version was built -- same "check first" discipline as every
other signal in this app.

Real, validated result: Brier beats a naive base-rate baseline in 17/17
yearly walk-forward folds (base rates: ~47% decision, ~33% KO/TKO, ~20%
submission).

**weight_class added 2026-07-18** (heavier divisions finish more often --
a real, separate improvement, scripts/check_mma_round2_signals.py). REAL
BUG caught while validating it: `pd.get_dummies` on the whole dataframe
before the walk-forward split (the pattern backtest_mma_distance.py
already used successfully) destabilized this model's small early folds --
2011's Brier blew up from 0.64 to 0.77 with weight_class added, WORSE
than without it, even after normalizing the raw 124-string field down to
13 real divisions (mma_features.py::normalize_weight_class). Root cause:
a training slice that predates a category (e.g. no Women's Featherweight
fights exist yet in 2011's own training data) still gets that column in
its design matrix under the global-dummy approach, adding unhelpful
dimensionality to an already-small multinomial fit. Fixed by fitting a
`OneHotEncoder(handle_unknown="ignore")` PER FOLD (via ColumnTransformer)
instead -- each fold's encoder only knows about categories that actually
exist in ITS OWN training data, and gracefully zero-encodes anything
unseen at test time. With that fix: Brier 0.6001 vs the leaner (no-
weight-class) model's 0.6048, 17/17 yearly folds. `method_service_mma.py`
mirrors this exact pipeline shape for live serving (fits once on ALL data,
so this per-fold-vs-global distinction doesn't apply there -- every
category is present in the training set by definition).

Run: backend/.venv/Scripts/python.exe scripts/backtest_mma_method.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from sklearn.compose import ColumnTransformer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import OneHotEncoder, StandardScaler  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402

FIRST_TEST_YEAR = 2010
MIN_TRAIN_ROWS = 300


def build_dataset() -> pd.DataFrame:
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    feature_rows = mma_features.build_feature_rows(fights, raw, bios)

    rows = []
    for r in feature_rows:
        if r["method_bucket"] is None:
            continue  # DQ/overturned/other -- genuinely ambiguous, not one of the 3 real outcomes
        row = mma_features.to_symmetric_method_features(r)
        row["year"] = int(r["event_date"][:4])
        row["method"] = r["method_bucket"]
        rows.append(row)

    df = pd.DataFrame(rows).dropna(subset=["scheduled_rounds"])
    return df


def main():
    df = build_dataset()
    print(f"Total fights with a clean 3-way method target: {len(df)}")
    print(f"Base rates:\n{df['method'].value_counts(normalize=True)}\n")

    numeric_cols = mma_features.METHOD_MODEL_NUMERIC_FEATURES
    all_cols = numeric_cols + ["weight_class"]
    test_years = sorted(y for y in df["year"].unique() if y >= FIRST_TEST_YEAR)

    year_rows = []
    for year in test_years:
        train = df[df["year"] < year].copy()
        test = df[df["year"] == year].copy()
        if len(train) < MIN_TRAIN_ROWS or len(test) == 0:
            continue

        medians = train[numeric_cols].median()
        train[numeric_cols] = train[numeric_cols].fillna(medians)
        test[numeric_cols] = test[numeric_cols].fillna(medians)

        pre = ColumnTransformer([
            ("num", StandardScaler(), numeric_cols),
            ("cat", OneHotEncoder(handle_unknown="ignore"), ["weight_class"]),
        ])
        model = make_pipeline(pre, LogisticRegression(C=0.5, max_iter=2000))
        model.fit(train[all_cols], train["method"])
        proba = model.predict_proba(test[all_cols])
        classes = list(model.classes_)

        base_rates = train["method"].value_counts(normalize=True)
        baseline_proba = [base_rates.get(c, 0.0) for c in classes]

        model_brier_terms, baseline_brier_terms = [], []
        model_correct = baseline_correct = 0
        for row_idx, actual in enumerate(test["method"].values):
            actual_vec = [1.0 if c == actual else 0.0 for c in classes]
            model_brier_terms.append(sum((p - a) ** 2 for p, a in zip(proba[row_idx], actual_vec)))
            baseline_brier_terms.append(sum((p - a) ** 2 for p, a in zip(baseline_proba, actual_vec)))
            model_correct += classes[proba[row_idx].argmax()] == actual
            baseline_correct += base_rates.idxmax() == actual

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
    print(f"{'Model':<28}{'Brier (3-way)':>15}{'Accuracy':>12}")
    print(f"{'Multinomial logreg':<28}{model_brier:>15.4f}{model_acc:>12.4f}")
    print(f"{'Naive (base rate)':<28}{baseline_brier:>15.4f}{baseline_acc:>12.4f}")

    brier_wins = sum(1 for r in year_rows if r["model_brier"] < r["baseline_brier"])
    acc_wins = sum(1 for r in year_rows if r["model_acc"] > r["baseline_acc"])
    print(f"\nModel beat baseline Brier in {brier_wins}/{len(year_rows)} yearly folds, accuracy in {acc_wins}/{len(year_rows)}.")

    print("\nYear-by-year:")
    for r in year_rows:
        print(f"  {r['year']}: model_acc={r['model_acc']:.4f}  baseline_acc={r['baseline_acc']:.4f}  "
              f"model_brier={r['model_brier']:.4f}  baseline_brier={r['baseline_brier']:.4f}  n={r['n']}")


if __name__ == "__main__":
    main()
