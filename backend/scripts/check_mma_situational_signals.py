"""Investigates real MMA-native situational-layer candidates BEFORE building
anything, same "check first" discipline as MLB's Phase 4 (getaway-day
fatigue/rest days checked and honestly rejected, position-player injuries
the one real candidate found). Correlates the Elo model's own walk-forward
RESIDUAL (actual outcome minus Elo's predicted probability) against each
candidate factor -- if Elo already prices something in correctly, the
residual will show no relationship with it; if Elo is systematically wrong
in a factor's direction, that's a real, addable signal.

Run: backend/.venv/Scripts/python.exe scripts/check_mma_situational_signals.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402
import statistics  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402
from app.models.baseline.elo_mma import BASE_RATING, K, win_prob  # noqa: E402


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


def main():
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()

    raw_by_fight_fighter = {}
    for row in raw:
        fid = row["fight_url"].rstrip("/").rsplit("/", 1)[-1]
        raw_by_fight_fighter[(fid, row["fighter_id"])] = row

    # Per-fighter rolling history (last weight class, age via DOB) -- built
    # the same strict-chronological-shift way as mma_features.py.
    ratings: dict[str, float] = {}
    n_fights: dict[str, int] = {}
    last_fight_date: dict[str, dt.date] = {}
    last_weight_class: dict[str, str] = {}

    layoff_days_list, layoff_resid_list = [], []
    a_age_list, b_age_list = [], []
    a_wc_change_list, b_wc_change_list = [], []

    for f in fights:
        if f["winner_id"] is None and not f["is_draw"]:
            continue
        if f["is_no_contest"]:
            continue

        fight_date = dt.date.fromisoformat(f["event_date"]) if f["event_date"] else None
        a_id, b_id = f["fighter_a_id"], f["fighter_b_id"]
        a_r, b_r = ratings.get(a_id, BASE_RATING), ratings.get(b_id, BASE_RATING)
        p_a = win_prob(a_r, b_r)
        actual_a = 0.5 if f["is_draw"] else (1.0 if f["winner_id"] == a_id else 0.0)
        residual = actual_a - p_a  # positive = fighter_a outperformed Elo's expectation

        a_layoff = (fight_date - last_fight_date[a_id]).days if (fight_date and a_id in last_fight_date) else None
        b_layoff = (fight_date - last_fight_date[b_id]).days if (fight_date and b_id in last_fight_date) else None
        a_bio, b_bio = bios.get(a_id, {}), bios.get(b_id, {})
        a_dob, b_dob = mma_features.parse_dob(a_bio.get("dob")), mma_features.parse_dob(b_bio.get("dob"))
        a_age = (fight_date - a_dob).days / 365.25 if (fight_date and a_dob) else None
        b_age = (fight_date - b_dob).days / 365.25 if (fight_date and b_dob) else None
        a_wc_changed = 1 if (a_id in last_weight_class and f["weight_class"] and last_weight_class[a_id] != f["weight_class"]) else 0
        b_wc_changed = 1 if (b_id in last_weight_class and f["weight_class"] and last_weight_class[b_id] != f["weight_class"]) else 0

        # Pool BOTH fighters' (layoff, residual-from-their-own-perspective)
        # pairs -- same symmetric-pooling approach as the age check below,
        # not the earlier flawed one-sided-only version (which accidentally
        # selected for "opponent is a debut fighter", a confound, not a
        # layoff isolation).
        if a_layoff is not None:
            layoff_days_list.append(a_layoff)
            layoff_resid_list.append(residual)
        if b_layoff is not None:
            layoff_days_list.append(b_layoff)
            layoff_resid_list.append(-residual)

        a_exp, b_exp = n_fights.get(a_id, 0), n_fights.get(b_id, 0)
        if a_age is not None:
            a_age_list.append((a_age, residual, a_exp))
        if b_age is not None:
            b_age_list.append((b_age, -residual, b_exp))

        if a_id in last_weight_class:
            a_wc_change_list.append((a_wc_changed, residual))
        if b_id in last_weight_class:
            b_wc_change_list.append((b_wc_changed, -residual))

        # update state
        if f["is_draw"]:
            delta = K * (0.5 - p_a)
        else:
            actual = 1.0 if f["winner_id"] == a_id else 0.0
            delta = K * (actual - p_a)
        ratings[a_id] = a_r + delta
        ratings[b_id] = b_r - delta
        n_fights[a_id] = n_fights.get(a_id, 0) + 1
        n_fights[b_id] = n_fights.get(b_id, 0) + 1
        if fight_date:
            last_fight_date[a_id] = fight_date
            last_fight_date[b_id] = fight_date
        if f["weight_class"]:
            last_weight_class[a_id] = f["weight_class"]
            last_weight_class[b_id] = f["weight_class"]

    print(f"Total scored fights: {len(a_age_list)}\n")

    # 1. Layoff effect: correlate "days since last fight" with residual
    #    (only using fights where exactly one side had known layoff data,
    #    isolating the effect cleanly)
    if len(layoff_days_list) > 50:
        r = pearson(layoff_days_list, layoff_resid_list)
        print(f"Layoff-days vs. Elo residual: r={r:.4f}, n={len(layoff_days_list)}")
        buckets = [(0, 180), (180, 365), (365, 730), (730, 99999)]
        for lo, hi in buckets:
            vals = [res for days, res in zip(layoff_days_list, layoff_resid_list) if lo <= days < hi]
            if vals:
                print(f"  {lo}-{hi}d: mean residual={statistics.mean(vals):+.4f}, n={len(vals)}")
    print()

    # 2. Age effect
    all_age_rows = a_age_list + b_age_list
    ages = [a for a, _, _ in all_age_rows]
    resids = [r for _, r, _ in all_age_rows]

    def _report_age_effect(rows, label):
        ages_ = [a for a, _, _ in rows]
        resids_ = [r for _, r, _ in rows]
        if len(ages_) < 100:
            print(f"{label}: too few rows ({len(ages_)})")
            return
        r_ = pearson(ages_, resids_)
        mean_age, mean_resid = sum(ages_) / len(ages_), sum(resids_) / len(resids_)
        std_age = (sum((a - mean_age) ** 2 for a in ages_) / len(ages_)) ** 0.5
        std_resid = (sum((v - mean_resid) ** 2 for v in resids_) / len(resids_)) ** 0.5
        slope = r_ * (std_resid / std_age) if std_age > 0 else 0.0
        print(f"{label}: r={r_:.4f}, n={len(ages_)}, OLS slope={slope:+.5f} win-prob/year, mean_age={mean_age:.1f}")

    _report_age_effect(all_age_rows, "Age vs. Elo residual (ALL fighters)")
    age_buckets = [(0, 25), (25, 30), (30, 33), (33, 36), (36, 99)]
    for lo, hi in age_buckets:
        vals = [res for age, res in zip(ages, resids) if lo <= age < hi]
        if vals:
            print(f"  age {lo}-{hi}: mean residual={statistics.mean(vals):+.4f}, n={len(vals)}")
    print()

    # Same check, but restricted to fighters with real UFC experience (>=4
    # prior fights) -- checks whether the "age effect" is actually just
    # "Elo hasn't converged yet for a fighter with few fights" (which would
    # ALSO correlate with age, since younger fighters tend to have fewer
    # fights, but is a genuinely different mechanism) rather than a real,
    # independent age-decline signal.
    experienced_rows = [(a, r, e) for a, r, e in all_age_rows if e >= 4]
    _report_age_effect(experienced_rows, "Age vs. Elo residual (fighters with >=4 prior UFC fights only)")
    print()

    # 3. Weight-class change effect
    changed_resid = [res for changed, res in (a_wc_change_list + b_wc_change_list) if changed == 1]
    same_resid = [res for changed, res in (a_wc_change_list + b_wc_change_list) if changed == 0]
    print(f"Weight-class change: mean residual={statistics.mean(changed_resid):+.4f} (n={len(changed_resid)}) "
          f"vs. no change: mean residual={statistics.mean(same_resid):+.4f} (n={len(same_resid)})")


if __name__ == "__main__":
    main()
