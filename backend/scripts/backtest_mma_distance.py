"""Walk-forward validates a went-the-distance model on real ufcstats.com
data (data/ufc_fight_cache.json, 8,780 fights) -- this app's flagship
differentiator market, per a SEPARATE, standalone research project's
earlier finding (project_ufc_betting_model) that this was the one market
with a real edge across 9 markets tested there. Re-tested fresh here, in
this app's own harness, not ported -- see mma_features.py's docstring for
why (strict point-in-time features, no leakage).

Went-the-distance is symmetric in fighter_a/fighter_b (the label doesn't
care who's "a" -- ufc_data.py's a/b assignment is arbitrary page order, not
favourite/underdog), so features here are ORDER-INVARIANT combinations
(mean/max of both fighters' rolling stats) rather than raw a_X/b_X pairs --
using the raw asymmetric pairs would let the model fit noise from the
meaningless a/b split.

Run: backend/.venv/Scripts/python.exe scripts/backtest_mma_distance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402
from app.models.calibration import brier_score, log_loss  # noqa: E402

FIRST_TEST_YEAR = 2010  # leaves 1993-2009 as rolling-feature warmup + minimum training history
MIN_TRAIN_ROWS = 300


def build_dataset() -> pd.DataFrame:
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    feature_rows = mma_features.build_feature_rows(fights, raw, bios)

    rows = []
    for r in feature_rows:
        row = mma_features.to_symmetric_distance_features(r)
        row["year"] = int(r["event_date"][:4])
        row["went_the_distance"] = r["went_the_distance"]
        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["scheduled_rounds"])  # a handful of very old fights have no parseable round format -- not a base-rate-relevant sample to guess at
    return df


def main():
    df = build_dataset()
    print(f"Total fights with a clean target: {len(df)}")
    print(f"Base rate (went the distance): {df['went_the_distance'].mean():.4f}")
    print(f"Base rate by scheduled_rounds:\n{df.groupby('scheduled_rounds')['went_the_distance'].agg(['mean', 'count'])}\n")

    numeric_cols = mma_features.DISTANCE_MODEL_NUMERIC_FEATURES
    weight_dummies = pd.get_dummies(df["weight_class"], prefix="wc")
    df = pd.concat([df, weight_dummies], axis=1)
    feature_cols = numeric_cols + list(weight_dummies.columns)

    # Median-impute (fit on TRAIN only per fold, not globally -- avoids a
    # subtle look-ahead leak where a later fight's data could shift an
    # earlier fold's imputed value) -- computed inside the fold loop below.

    test_years = sorted(y for y in df["year"].unique() if y >= FIRST_TEST_YEAR)
    all_model_preds, all_baseline_preds, all_outcomes = [], [], []
    year_rows = []

    for year in test_years:
        train = df[df["year"] < year].copy()
        test = df[df["year"] == year].copy()
        if len(train) < MIN_TRAIN_ROWS or len(test) == 0:
            continue

        medians = train[numeric_cols].median()
        train[numeric_cols] = train[numeric_cols].fillna(medians)
        test[numeric_cols] = test[numeric_cols].fillna(medians)

        model = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))
        model.fit(train[feature_cols], train["went_the_distance"])
        model_pred = model.predict_proba(test[feature_cols])[:, 1]

        baseline_rate = train["went_the_distance"].mean()  # naive: this year's prediction = historical base rate up to that point (walk-forward safe)
        baseline_pred = np.full(len(test), baseline_rate)

        all_model_preds.extend(model_pred.tolist())
        all_baseline_preds.extend(baseline_pred.tolist())
        all_outcomes.extend(test["went_the_distance"].tolist())

        year_rows.append({
            "year": year, "n": len(test),
            "model_acc": ((model_pred >= 0.5).astype(int) == test["went_the_distance"].values).mean(),
            "baseline_acc": ((baseline_pred >= 0.5).astype(int) == test["went_the_distance"].values).mean(),
            "model_brier": brier_score(model_pred.tolist(), test["went_the_distance"].tolist()),
            "baseline_brier": brier_score(baseline_pred.tolist(), test["went_the_distance"].tolist()),
        })

    n = len(all_outcomes)
    print(f"Walk-forward test fights ({test_years[0] if year_rows else '-'}-{year_rows[-1]['year'] if year_rows else '-'}): {n}\n")

    model_brier = brier_score(all_model_preds, all_outcomes)
    baseline_brier = brier_score(all_baseline_preds, all_outcomes)
    model_acc = sum((p >= 0.5) == o for p, o in zip(all_model_preds, all_outcomes)) / n
    baseline_acc = sum((p >= 0.5) == o for p, o in zip(all_baseline_preds, all_outcomes)) / n

    print(f"{'Model':<28}{'Brier':>10}{'LogLoss':>10}{'Accuracy':>10}")
    print(f"{'Logistic regression':<28}{model_brier:>10.4f}{log_loss(all_model_preds, all_outcomes):>10.4f}{model_acc:>10.4f}")
    print(f"{'Naive (base rate)':<28}{baseline_brier:>10.4f}{log_loss(all_baseline_preds, all_outcomes):>10.4f}{baseline_acc:>10.4f}")

    # Paired bootstrap CI on accuracy-over-baseline, same rigor bar as the
    # original ufc-model project's own walk-forward validation (this is a
    # SEPARATE, re-derived check, not importing that project's numbers).
    rng = np.random.default_rng(42)
    model_arr = np.array(all_model_preds)
    baseline_arr = np.array(all_baseline_preds)
    outcome_arr = np.array(all_outcomes)
    diffs = []
    for _ in range(2000):
        idx = rng.integers(0, n, n)
        m_acc = ((model_arr[idx] >= 0.5).astype(int) == outcome_arr[idx]).mean()
        b_acc = ((baseline_arr[idx] >= 0.5).astype(int) == outcome_arr[idx]).mean()
        diffs.append(m_acc - b_acc)
    diffs = np.array(diffs)
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    print(f"\nPaired bootstrap (2000 resamples), accuracy diff (model - baseline): "
          f"{(model_acc - baseline_acc) * 100:+.2f}pp, 95% CI [{lo * 100:+.2f}pp, {hi * 100:+.2f}pp]")
    if lo > 0:
        print("=> CI excludes zero: a real, walk-forward-robust signal.")
    else:
        print("=> CI includes zero: not distinguishable from no signal at this sample size.")

    print("\nYear-by-year:")
    for r in year_rows:
        print(f"  {r['year']}: model_acc={r['model_acc']:.4f}  baseline_acc={r['baseline_acc']:.4f}  "
              f"model_brier={r['model_brier']:.4f}  baseline_brier={r['baseline_brier']:.4f}  n={r['n']}")

    wins = sum(1 for r in year_rows if r["model_acc"] > r["baseline_acc"])
    print(f"\nModel beat baseline accuracy in {wins}/{len(year_rows)} yearly folds.")


if __name__ == "__main__":
    main()
