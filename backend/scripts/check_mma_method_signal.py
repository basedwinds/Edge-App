"""Checks whether method-of-finish (KO/TKO vs Submission vs Decision) has a
real, checkable signal before building anything -- same discipline as the
went-the-distance investigation this app already ran (and the experience-
diff moneyline check that just failed). Extends _RollingFighterState-style
tracking with method-specific finish rates (how often THIS fighter wins by
KO/TKO vs Submission, and how often they're finished by each, i.e. a real
proxy for chin/durability) as point-in-time features, then walk-forward
validates a multinomial logistic regression against a naive base-rate
baseline.

Run: backend/.venv/Scripts/python.exe scripts/check_mma_method_signal.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402
from app.models.calibration import brier_score  # noqa: E402

FIRST_TEST_YEAR = 2010
MIN_TRAIN_ROWS = 300


class _MethodState:
    __slots__ = ("n_fights", "n_ko_wins", "n_sub_wins", "n_dec_wins", "n_ko_losses", "n_sub_losses")

    def __init__(self):
        self.n_fights = 0
        self.n_ko_wins = 0
        self.n_sub_wins = 0
        self.n_dec_wins = 0
        self.n_ko_losses = 0  # times THIS fighter was finished by KO/TKO -- a real durability/chin proxy
        self.n_sub_losses = 0

    def snapshot(self) -> dict:
        n = self.n_fights
        return {
            "ko_win_rate": (self.n_ko_wins / n) if n > 0 else None,
            "sub_win_rate": (self.n_sub_wins / n) if n > 0 else None,
            "ko_loss_rate": (self.n_ko_losses / n) if n > 0 else None,
            "sub_loss_rate": (self.n_sub_losses / n) if n > 0 else None,
            "experience": n,
        }


def _method_bucket(method: str | None) -> str | None:
    if not method:
        return None
    m = method.strip().lower()
    if m.startswith("decision"):
        return "decision"
    if "ko" in m or "tko" in m:
        return "kotko"
    if "submission" in m:
        return "submission"
    return None  # DQ/other/overturned -- genuinely ambiguous, not one of the 3 real outcomes


def build_dataset() -> pd.DataFrame:
    fights = ufc_data.load_fights()
    state: dict[str, _MethodState] = {}

    def get(fid):
        if fid not in state:
            state[fid] = _MethodState()
        return state[fid]

    rows = []
    for f in fights:
        bucket = _method_bucket(f.get("method"))
        fight_happened = f["winner_id"] is not None or f["is_draw"]

        if bucket is not None and fight_happened and not f["is_no_contest"]:
            a_snap, b_snap = get(f["fighter_a_id"]).snapshot(), get(f["fighter_b_id"]).snapshot()
            rows.append({
                "year": int(f["event_date"][:4]) if f["event_date"] else None,
                "combined_ko_win_rate": _mean(a_snap["ko_win_rate"], b_snap["ko_win_rate"]),
                "combined_sub_win_rate": _mean(a_snap["sub_win_rate"], b_snap["sub_win_rate"]),
                "combined_ko_loss_rate": _mean(a_snap["ko_loss_rate"], b_snap["ko_loss_rate"]),
                "combined_sub_loss_rate": _mean(a_snap["sub_loss_rate"], b_snap["sub_loss_rate"]),
                "max_ko_win_rate": _max(a_snap["ko_win_rate"], b_snap["ko_win_rate"]),
                "max_sub_win_rate": _max(a_snap["sub_win_rate"], b_snap["sub_win_rate"]),
                "combined_experience": _mean(a_snap["experience"], b_snap["experience"]),
                "scheduled_rounds": f.get("scheduled_rounds"),
                "method": bucket,
            })

        if fight_happened and not f["is_no_contest"] and f["winner_id"] is not None:
            winner_id = f["winner_id"]
            loser_id = f["fighter_b_id"] if winner_id == f["fighter_a_id"] else f["fighter_a_id"]
            w, l = get(winner_id), get(loser_id)
            w.n_fights += 1
            l.n_fights += 1
            if bucket == "kotko":
                w.n_ko_wins += 1
                l.n_ko_losses += 1
            elif bucket == "submission":
                w.n_sub_wins += 1
                l.n_sub_losses += 1
            elif bucket == "decision":
                w.n_dec_wins += 1

    df = pd.DataFrame(rows).dropna(subset=["scheduled_rounds", "year"])
    return df


def _mean(a, b):
    vals = [v for v in (a, b) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _max(a, b):
    vals = [v for v in (a, b) if v is not None]
    return max(vals) if vals else None


def main():
    df = build_dataset()
    print(f"Total fights with a clean 3-way method target: {len(df)}")
    print(f"Base rates:\n{df['method'].value_counts(normalize=True)}\n")

    feature_cols = [
        "combined_ko_win_rate", "combined_sub_win_rate", "combined_ko_loss_rate", "combined_sub_loss_rate",
        "max_ko_win_rate", "max_sub_win_rate", "combined_experience", "scheduled_rounds",
    ]

    test_years = sorted(y for y in df["year"].unique() if y >= FIRST_TEST_YEAR)
    all_model_preds, all_baseline_preds, all_outcomes = [], [], []  # preds/outcomes as (p_kotko, p_sub, p_decision) tuples, outcomes as the bucket string

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
        model.fit(train_f, train["method"])
        proba = model.predict_proba(test_f)
        classes = list(model.classes_)

        base_rates = train["method"].value_counts(normalize=True)
        baseline_proba = [base_rates.get(c, 0.0) for c in classes]

        model_brier_terms = []
        baseline_brier_terms = []
        model_correct = 0
        baseline_correct = 0
        for row_idx, actual in enumerate(test["method"].values):
            actual_vec = [1.0 if c == actual else 0.0 for c in classes]
            model_brier_terms.append(sum((p - a) ** 2 for p, a in zip(proba[row_idx], actual_vec)))
            baseline_brier_terms.append(sum((p - a) ** 2 for p, a in zip(baseline_proba, actual_vec)))
            model_pred_class = classes[proba[row_idx].argmax()]
            baseline_pred_class = base_rates.idxmax()
            model_correct += model_pred_class == actual
            baseline_correct += baseline_pred_class == actual

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

    wins = sum(1 for r in year_rows if r["model_acc"] > r["baseline_acc"])
    print(f"\nModel beat baseline accuracy in {wins}/{len(year_rows)} yearly folds.")
    print("\nYear-by-year:")
    for r in year_rows:
        print(f"  {r['year']}: model_acc={r['model_acc']:.4f}  baseline_acc={r['baseline_acc']:.4f}  "
              f"model_brier={r['model_brier']:.4f}  baseline_brier={r['baseline_brier']:.4f}  n={r['n']}")


if __name__ == "__main__":
    main()
