"""Walk-forward validates the round-of-finish model on real ufcstats.com
data -- extends the distance/method work to a per-round distribution.
First checked as a standalone investigation (scripts/check_mma_rounds_
signal.py) before this production version was built -- same "check first"
discipline as every other signal in this app.

Real, but WEAKER/NOISIER signal than distance or method-of-finish: the raw
5-way round-of-finish target only beats a per-scheduled_rounds naive
baseline in 13/17 yearly Brier folds (vs. 17/17 for both other MMA
models), and loses on accuracy most years (the naive per-scheduled_rounds
mode is often a better single point-guess than the model's own argmax).
The market-relevant question -- summed P(ends before round N) for the
"rounds" ladder's actual rungs -- is more robust since per-class errors
partially cancel when summed: wins 10-15/17 yearly Brier folds depending
on which rung, always net Brier-positive overall. Ships with an explicit
"noisier than this app's other MMA signals" caveat everywhere it's shown
(mma_markets.py's reasoning endpoint, Recommended Bets).

Run: backend/.venv/Scripts/python.exe scripts/backtest_mma_rounds.py
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
LADDER_RUNGS = (2, 3, 4, 5)


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
        row = mma_features.to_symmetric_rounds_features(r)
        row["year"] = int(r["event_date"][:4])
        row["round_of_finish"] = round_of_finish
        rows.append(row)

    return pd.DataFrame(rows)


def main():
    df = build_dataset()
    print(f"Total fights with a clean round-of-finish target: {len(df)}")
    print("Round distribution by scheduled_rounds:")
    print(df.groupby("scheduled_rounds")["round_of_finish"].value_counts(normalize=True).sort_index())
    print()

    feature_cols = mma_features.ROUNDS_MODEL_NUMERIC_FEATURES
    test_years = sorted(y for y in df["year"].unique() if y >= FIRST_TEST_YEAR)

    year_rows = []
    ladder_by_rung: dict[int, list[dict]] = {n: [] for n in LADDER_RUNGS}
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

        baseline_by_sched = {sched: grp["round_of_finish"].value_counts(normalize=True).to_dict() for sched, grp in train.groupby("scheduled_rounds")}

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

            for n_cutoff in LADDER_RUNGS:
                if n_cutoff > sched:
                    continue
                ladder_actual = 1.0 if actual < n_cutoff else 0.0
                model_p = sum(p for c, p in zip(classes, proba[row_idx]) if c < n_cutoff)
                baseline_p = sum(v for k, v in baseline_dist.items() if k < n_cutoff)
                ladder_by_rung[n_cutoff].append({
                    "year": year, "model_sq_err": (model_p - ladder_actual) ** 2,
                    "baseline_sq_err": (baseline_p - ladder_actual) ** 2,
                })

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
    print(f"{'Model (5-way round target)':<28}{'Brier':>10}{'Accuracy':>12}")
    print(f"{'Multinomial logreg':<28}{model_brier:>10.4f}{model_acc:>12.4f}")
    print(f"{'Naive (per-sched base rate)':<28}{baseline_brier:>10.4f}{baseline_acc:>12.4f}")
    brier_wins = sum(1 for r in year_rows if r["model_brier"] < r["baseline_brier"])
    acc_wins = sum(1 for r in year_rows if r["model_acc"] > r["baseline_acc"])
    print(f"Model beat baseline Brier in {brier_wins}/{len(year_rows)} yearly folds, accuracy in {acc_wins}/{len(year_rows)}.")
    print("NOTE: weaker than distance/method-of-finish (both 17/17 Brier) -- the market-relevant\n"
          "ladder question below is more robust since per-class errors partially cancel when summed.")

    print("\n--- Market-relevant: P(ends before round N) ladder, walk-forward across all years ---")
    for n_cutoff in LADDER_RUNGS:
        results = ladder_by_rung[n_cutoff]
        if not results:
            continue
        by_year: dict[int, list[dict]] = {}
        for r in results:
            by_year.setdefault(r["year"], []).append(r)
        year_wins = 0
        n_years = 0
        for year, rs in by_year.items():
            m = sum(r["model_sq_err"] for r in rs) / len(rs)
            b = sum(r["baseline_sq_err"] for r in rs) / len(rs)
            n_years += 1
            if m < b:
                year_wins += 1
        model_brier_r = sum(r["model_sq_err"] for r in results) / len(results)
        baseline_brier_r = sum(r["baseline_sq_err"] for r in results) / len(results)
        print(f"  before round {n_cutoff}: model_brier={model_brier_r:.4f}  baseline_brier={baseline_brier_r:.4f}  "
              f"wins={year_wins}/{n_years} yearly folds  n={len(results)}")

    print("\nYear-by-year (5-way target):")
    for r in year_rows:
        print(f"  {r['year']}: model_acc={r['model_acc']:.4f}  baseline_acc={r['baseline_acc']:.4f}  "
              f"model_brier={r['model_brier']:.4f}  baseline_brier={r['baseline_brier']:.4f}  n={r['n']}")


if __name__ == "__main__":
    main()
