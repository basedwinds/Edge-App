"""Round 2 of the MMA signal search (2026-07-18) -- checks four more
candidates against real data before building anything: weight-class as a
feature for method-of-finish/rounds (currently excluded from both),
referee stoppage tendency, stance matchup (orthodox vs southpaw), and a
nonlinear "ring rust" layoff effect. Same "check first" discipline as
every other signal in this app -- see check_mma_moneyline_features.py /
check_mma_mov_signal.py / check_mma_rounds_signal.py for the prior rounds.

RESULTS (final, after follow-up validation beyond what this script alone
shows -- see method_service_mma.py's docstring for the full story):

A) weight_class for method-of-finish: REAL, SHIPPED. This script's own
   quick check (ColumnTransformer + OneHotEncoder, single split-style
   walk-forward) showed a real improvement, but a SEPARATE real bug was
   caught while wiring this into production: using `pd.get_dummies` on
   the whole dataframe before the walk-forward split (matching
   distance's already-shipped pattern) destabilized small early folds --
   2011 blew up to 0.77 Brier, worse than not using weight_class at all,
   even after normalizing the raw 124-string weight_class field down to
   13 real divisions (see mma_features.py::normalize_weight_class, also
   added this round). Root cause: a fold's training slice that predates
   a category (e.g. no Women's Featherweight fights yet in 2011) still
   gets that all-zero column under the global-dummy approach, destabilizing
   an already-small multinomial fit. Fixed by fitting the OneHotEncoder
   PER FOLD (handle_unknown="ignore") instead of globally -- final,
   correct result: Brier 0.6048 -> 0.6001, 17/17 yearly folds (was already
   17/17 without weight_class; the win here is the OVERALL Brier
   improving, not the fold count). Does NOT transfer to the rounds model
   (checked below, made it worse there).
   Did NOT re-test weight_class for method_of_finish AGAINST distance's
   OWN already-shipped global-pd.get_dummies pattern to see if distance
   has a similar latent instability in an early fold nobody's looked at
   closely -- distance's own backtest was re-run after normalizing
   weight_class (see mma_features.py) and showed a small IMPROVEMENT
   (0.2461->0.2450), not a regression, so it's fine as-is, but the
   per-fold-fit lesson from this round is worth remembering if distance's
   own feature set changes again in the future.
B) referee stoppage tendency: REJECTED. Only 4/10 high-volume referees
   show a same-sign deviation across both halves of their own career, and
   even those aren't stable in magnitude (John McCarthy: 78.3% -> 55.1%,
   more likely reflects the sport standardizing rules/officiating over
   his tenure than a fixed personal tendency to bet on). Same "too small
   a sample per individual" conclusion the NFL side of this app already
   reached for referee assignments.
C) stance matchup (orthodox vs southpaw): REJECTED, but the FIRST version
   of this check had a real methodology bug worth remembering. ufcstats
   lists the WINNER first (fighter_a wins 64.2% of decided fights, not
   the ~50% an arbitrary page-order split would give) -- harmless for
   order-invariant targets (distance/method-of-finish don't depend on WHO
   wins), but a naive per-fighter-a correlate check inherits that bias.
   Fixed by symmetrizing (stack BOTH fighters' own signed residual +
   their own stance, same pattern the layoff check below already used
   correctly). Corrected result: r=+0.055 -- same weak (<0.07) territory
   as every already-rejected moneyline feature this session. Not built.
D) nonlinear "ring rust" layoff effect: REJECTED as an independent
   signal, but genuinely instructive. In ISOLATION (added to a bare Elo
   model with no age adjustment), a >365-day layoff penalty IS real and
   improves Brier at a clean smooth optimum (0.24182 -> 0.24156 at
   ~-30 Elo/extra-year beyond 365 days, capped at 3 years). But once
   layered ON TOP of the already-shipped age adjustment, the improvement
   evaporates to noise-level (0.23430 -> 0.23426 best case, net negative
   at higher magnitudes) -- older fighters tend to also have longer
   layoffs (injury recovery time, approaching retirement), so this is
   mostly REDUNDANT with age, not an independent additive signal. Same
   "real signal doesn't automatically survive contact with the already-
   shipped model" pattern as experience-diff and NFL's turnover-margin-
   regression. Not built.

E) reach/height for method-of-finish (checked 2026-07-18, after A-D above):
   REJECTED. Added reach_diff_abs/combined_reach/height_diff_abs on top
   of the already-shipped weight_class-augmented model. Only 12/17 yearly
   folds beat the current shipped model (weaker fold-consistency than
   even the already-caveated rounds model's 13/17), and the overall Brier
   improvement was tiny (0.6001 -> 0.5994, ~7x smaller than weight_class's
   real gain of 0.6048 -> 0.6001) -- right at the "probably noise, not a
   real signal" boundary this app's other checks have used to reject
   things. Not built. Card position (main event vs. prelim) was also
   considered but found NOT cleanly derivable from the current data
   pipeline -- `ufc_data.py::load_fights()` sorts fights within the same
   event by `(event_date, fight_id)`, and fight_id is a content hash with
   no relation to card position, so the real scrape-order signal (ufcstats
   lists cards main-event-first) is destroyed by the sort before it ever
   reaches feature-building. Would need the cache-building pipeline itself
   changed to explicitly capture and preserve a position field -- a real
   re-scrape/infra change, not a quick feature check, so left unattempted
   rather than forced via a weak proxy (is_title_bout is already used and
   would double-count).
   This closes out the MMA signal search for now -- every remaining idea
   either failed a real check or needs new infrastructure this session
   didn't build.

Run: backend/.venv/Scripts/python.exe scripts/check_mma_round2_signals.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402

import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler, OneHotEncoder  # noqa: E402
from sklearn.compose import ColumnTransformer  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402
from app.models.baseline import elo_mma  # noqa: E402

FIRST_TEST_YEAR = 2010
MIN_TRAIN_ROWS = 300


# ---------------------------------------------------------------------------
# A) Weight-class as a feature for method-of-finish (currently excluded --
#    "Deliberately does NOT include weight_class ... only validated the
#    leaner feature set" per mma_features.py's own docstring). Heavier
#    divisions are well-documented to finish more often (more one-shot
#    power); check if that's real signal beyond what's already captured.
# ---------------------------------------------------------------------------
def check_weight_class_for_method():
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    feature_rows = mma_features.build_feature_rows(fights, raw, bios)

    rows = []
    for r in feature_rows:
        if r["method_bucket"] is None:
            continue
        row = mma_features.to_symmetric_method_features(r)
        row["weight_class"] = r["weight_class"] or "Unknown"
        row["year"] = int(r["event_date"][:4])
        row["method"] = r["method_bucket"]
        rows.append(row)
    df = pd.DataFrame(rows).dropna(subset=["scheduled_rounds"])

    numeric_cols = mma_features.METHOD_MODEL_NUMERIC_FEATURES
    test_years = sorted(y for y in df["year"].unique() if y >= FIRST_TEST_YEAR)

    def run(with_weight_class: bool):
        year_rows = []
        for year in test_years:
            train = df[df["year"] < year].copy()
            test = df[df["year"] == year].copy()
            if len(train) < MIN_TRAIN_ROWS or len(test) == 0:
                continue
            medians = train[numeric_cols].median()
            train_f = train.copy()
            test_f = test.copy()
            train_f[numeric_cols] = train_f[numeric_cols].fillna(medians)
            test_f[numeric_cols] = test_f[numeric_cols].fillna(medians)

            if with_weight_class:
                pre = ColumnTransformer([
                    ("num", StandardScaler(), numeric_cols),
                    ("cat", OneHotEncoder(handle_unknown="ignore"), ["weight_class"]),
                ])
                model = make_pipeline(pre, LogisticRegression(C=0.5, max_iter=2000))
            else:
                model = make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=2000))

            X_train = train_f[numeric_cols + ["weight_class"]] if with_weight_class else train_f[numeric_cols]
            X_test = test_f[numeric_cols + ["weight_class"]] if with_weight_class else test_f[numeric_cols]
            model.fit(X_train, train_f["method"])
            classes = list(model.classes_)
            proba = model.predict_proba(X_test)

            terms = []
            test_rows = test_f.reset_index(drop=True)
            for i in range(len(test_rows)):
                actual = test_rows.loc[i, "method"]
                actual_vec = [1.0 if c == actual else 0.0 for c in classes]
                terms.append(sum((p - a) ** 2 for p, a in zip(proba[i], actual_vec)))
            year_rows.append({"year": year, "n": len(test_f), "brier": sum(terms) / len(terms)})
        n_total = sum(r["n"] for r in year_rows)
        brier = sum(r["brier"] * r["n"] for r in year_rows) / n_total
        return brier, year_rows

    brier_without, rows_without = run(False)
    brier_with, rows_with = run(True)
    wins = sum(1 for a, b in zip(rows_with, rows_without) if a["brier"] < b["brier"])

    print("=== A) weight_class added to method-of-finish model ===")
    print(f"Without weight_class: Brier={brier_without:.4f}")
    print(f"With weight_class:    Brier={brier_with:.4f}")
    print(f"Adding weight_class beat the leaner model in {wins}/{len(rows_without)} yearly folds")
    print()


# ---------------------------------------------------------------------------
# B) Referee stoppage tendency -- restricted to high-volume referees
#    (>=200 fights), checked for split-half consistency (a real tendency
#    should be stable across two halves of that referee's own career, not
#    just a single-window artifact).
# ---------------------------------------------------------------------------
def check_referee_tendency():
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    ref_by_fight = {}
    for row in raw:
        fid = row["fight_url"].rstrip("/").rsplit("/", 1)[-1]
        if row.get("referee"):
            ref_by_fight[fid] = row["referee"]

    rows = []
    for f in fights:
        if f["is_no_contest"] or f["went_the_distance"] is None:
            continue
        ref = ref_by_fight.get(f["id"])
        if not ref:
            continue
        rows.append({"ref": ref, "year": int(f["event_date"][:4]), "finished": 1 - f["went_the_distance"]})
    df = pd.DataFrame(rows)

    overall_rate = df["finished"].mean()
    counts = df["ref"].value_counts()
    high_volume_refs = counts[counts >= 200].index.tolist()

    print(f"=== B) referee stoppage tendency (overall finish rate: {overall_rate:.4f}) ===")
    print(f"Referees with >=200 fights: {len(high_volume_refs)}")

    consistent = 0
    checked = 0
    for ref in high_volume_refs:
        ref_df = df[df["ref"] == ref].sort_values("year")
        mid = len(ref_df) // 2
        first_half_rate = ref_df.iloc[:mid]["finished"].mean()
        second_half_rate = ref_df.iloc[mid:]["finished"].mean()
        first_dev = first_half_rate - overall_rate
        second_dev = second_half_rate - overall_rate
        checked += 1
        same_sign = (first_dev > 0) == (second_dev > 0)
        if same_sign and abs(first_dev) > 0.02 and abs(second_dev) > 0.02:
            consistent += 1
        print(f"  {ref:<20} n={len(ref_df):<5} full_rate={ref_df['finished'].mean():.4f}  "
              f"1st_half={first_half_rate:.4f}  2nd_half={second_half_rate:.4f}  "
              f"consistent_deviation={'YES' if same_sign and abs(first_dev)>0.02 and abs(second_dev)>0.02 else 'no'}")
    print(f"\n{consistent}/{checked} high-volume referees show a consistent (>2pp, same-sign both halves) stoppage-rate deviation")
    print()


# ---------------------------------------------------------------------------
# C) Stance matchup (orthodox vs southpaw) -- checked against the Elo
#    walk-forward RESIDUAL (prediction error), same methodology as the
#    validated age-adjustment check, not raw win rate (which would be
#    confounded by whichever stance currently has the better fighters).
# ---------------------------------------------------------------------------
def check_stance_matchup():
    """REAL METHODOLOGY BUG caught and fixed while writing this check:
    ufcstats.com lists the WINNER first on a completed fight's page --
    confirmed live, fighter_a wins 64.2% of decided fights, not the ~50%
    an arbitrary page-order split would produce. That's harmless for
    order-invariant targets (went_the_distance/method_of_finish don't
    depend on WHO wins), but a naive per-fighter-a check like the first
    draft of this function (correlating "is fighter_a the southpaw" with
    the raw actual_a-vs-p_a residual) inherits that bias -- BOTH
    "fighter_a is southpaw" and "fighter_a is orthodox" groups showed a
    suspiciously positive mean residual (+0.12 / +0.08) before this fix,
    which is the a-tends-to-win bias leaking through, not a real stance
    effect. Fixed the same way the layoff check (D) already correctly
    does it: stack BOTH fighters' own perspectives (signed residual +
    their own stance) so the winner-listed-first bias cancels out
    symmetrically instead of contaminating one direction."""
    fights = ufc_data.load_fights()
    bios = ufc_data.load_fighter_bios()

    elo_state = elo_mma.MmaEloState()
    rows = []
    for f in fights:
        a_id, b_id = f["fighter_a_id"], f["fighter_b_id"]
        a_r, b_r = elo_state.get(a_id), elo_state.get(b_id)
        p_a = elo_mma.win_prob(a_r, b_r)

        if not f["is_no_contest"] and (f["winner_id"] is not None or f["is_draw"]):
            actual_a = 0.5 if f["is_draw"] else (1.0 if f["winner_id"] == a_id else 0.0)
            residual = actual_a - p_a

            a_stance = bios.get(a_id, {}).get("stance")
            b_stance = bios.get(b_id, {}).get("stance")
            if a_stance in ("Orthodox", "Southpaw") and b_stance in ("Orthodox", "Southpaw") and a_stance != b_stance:
                rows.append({"is_southpaw": 1 if a_stance == "Southpaw" else 0, "signed_residual": residual})
                rows.append({"is_southpaw": 1 if b_stance == "Southpaw" else 0, "signed_residual": -residual})

        if not f["is_no_contest"]:
            elo_mma.update_ratings(elo_state, a_id, b_id, f.get("winner_id"), f.get("is_draw", False))

    df = pd.DataFrame(rows)
    print("=== C) stance matchup (southpaw vs orthodox) vs Elo residual, symmetrized ===")
    print(f"Cross-stance fighter-perspective rows: {len(df)} ({len(df)//2} fights)")
    southpaw_residual = df[df["is_southpaw"] == 1]["signed_residual"].mean()
    orthodox_residual = df[df["is_southpaw"] == 0]["signed_residual"].mean()
    print(f"Mean signed residual when southpaw:  {southpaw_residual:+.4f}")
    print(f"Mean signed residual when orthodox:  {orthodox_residual:+.4f}")
    corr = df["is_southpaw"].corr(df["signed_residual"])
    print(f"Correlation(is_southpaw, signed_residual): {corr:+.4f}")
    print()


# ---------------------------------------------------------------------------
# D) Nonlinear "ring rust" layoff effect -- bucketed Elo residual by
#    layoff_days, checking for a real inflection beyond what a linear term
#    would capture (the distance model already uses max_layoff_days
#    linearly; moneyline doesn't use layoff at all).
# ---------------------------------------------------------------------------
def check_layoff_nonlinearity():
    fights = ufc_data.load_fights()
    state: dict[str, mma_features._RollingFighterState] = {}

    def get_state(fid):
        if fid not in state:
            state[fid] = mma_features._RollingFighterState()
        return state[fid]

    elo_state = elo_mma.MmaEloState()
    rows = []
    for f in fights:
        fight_date = dt.date.fromisoformat(f["event_date"]) if f["event_date"] else None
        a_id, b_id = f["fighter_a_id"], f["fighter_b_id"]
        a_r, b_r = elo_state.get(a_id), elo_state.get(b_id)
        p_a = elo_mma.win_prob(a_r, b_r)

        a_snap = get_state(a_id).snapshot(fight_date)
        b_snap = get_state(b_id).snapshot(fight_date)

        if not f["is_no_contest"] and (f["winner_id"] is not None or f["is_draw"]):
            actual_a = 0.5 if f["is_draw"] else (1.0 if f["winner_id"] == a_id else 0.0)
            residual = actual_a - p_a
            for prefix, snap in (("a", a_snap), ("b", b_snap)):
                if snap["layoff_days"] is not None:
                    sign = 1 if prefix == "a" else -1
                    rows.append({"layoff_days": snap["layoff_days"], "signed_residual": sign * residual})

        if f["is_draw"]:
            get_state(a_id).apply_result(fight_date, None, f.get("method"), f["went_the_distance"], None, None)
            get_state(b_id).apply_result(fight_date, None, f.get("method"), f["went_the_distance"], None, None)
        elif f["winner_id"] is not None:
            winner_is_a = f["winner_id"] == a_id
            get_state(a_id).apply_result(fight_date, winner_is_a, f.get("method"), f["went_the_distance"], None, None)
            get_state(b_id).apply_result(fight_date, not winner_is_a, f.get("method"), f["went_the_distance"], None, None)
        if not f["is_no_contest"]:
            elo_mma.update_ratings(elo_state, a_id, b_id, f.get("winner_id"), f.get("is_draw", False))

    df = pd.DataFrame(rows)
    print("=== D) nonlinear layoff/ring-rust effect vs Elo residual ===")
    print(f"Rows: {len(df)}")
    buckets = [(0, 90), (90, 180), (180, 270), (270, 365), (365, 545), (545, 730), (730, 10000)]
    for lo, hi in buckets:
        sub = df[(df["layoff_days"] >= lo) & (df["layoff_days"] < hi)]
        if len(sub) == 0:
            continue
        print(f"  {lo:4d}-{hi:<5d} days  n={len(sub):<6d}  mean_signed_residual={sub['signed_residual'].mean():+.4f}")
    corr = df["layoff_days"].corr(df["signed_residual"])
    print(f"Overall linear correlation(layoff_days, signed_residual): {corr:+.4f}")
    print()


if __name__ == "__main__":
    check_weight_class_for_method()
    check_referee_tendency()
    check_stance_matchup()
    check_layoff_nonlinearity()
