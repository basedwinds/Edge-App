"""Checks whether reach/height/stance/finish-rate/striking-and-takedown-
volume/experience explain the CURRENT moneyline model's (Elo + validated
age adjustment) walk-forward residual -- same "check before building"
discipline as check_mma_situational_signals.py's age/layoff/weight-class
check. If a feature shows no relationship with the residual, Elo+age
already captures what it would add (or it's genuinely not predictive of
who wins); if it does, that's a real, checkable candidate to fold into the
moneyline model.

Run: backend/.venv/Scripts/python.exe scripts/check_mma_moneyline_features.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt  # noqa: E402

from app.ingestion import ufc_data  # noqa: E402
from app.models import mma_features  # noqa: E402
from app.models.baseline.elo_mma import BASE_RATING, age_adjustment_elo, win_prob  # noqa: E402


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


def ols_slope(xs: list[float], ys: list[float]) -> float:
    r = pearson(xs, ys)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = (sum((x - mx) ** 2 for x in xs) / len(xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / len(ys)) ** 0.5
    return r * (sy / sx) if sx > 0 else 0.0


def main():
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    rows = mma_features.build_feature_rows(fights, raw, bios)
    print(f"{len(rows)} fights with usable pre-fight features\n")

    # Walk forward with the SAME model actually shipped (Elo + age
    # adjustment) to get each fight's real residual, keyed by fight_id so
    # it can be joined back to the feature rows above.
    ratings: dict[str, float] = {}
    residual_by_fight: dict[str, float] = {}
    WARMUP = 1500
    for i, f in enumerate(fights):
        if f["is_no_contest"]:
            continue
        if f["winner_id"] is None and not f["is_draw"]:
            continue
        a_id, b_id = f["fighter_a_id"], f["fighter_b_id"]
        a_r, b_r = ratings.get(a_id, BASE_RATING), ratings.get(b_id, BASE_RATING)
        fight_date = dt.date.fromisoformat(f["event_date"]) if f["event_date"] else None
        a_dob, b_dob = mma_features.parse_dob(bios.get(a_id, {}).get("dob")), mma_features.parse_dob(bios.get(b_id, {}).get("dob"))
        a_age = (fight_date - a_dob).days / 365.25 if (fight_date and a_dob) else None
        b_age = (fight_date - b_dob).days / 365.25 if (fight_date and b_dob) else None
        p_a = win_prob(a_r + age_adjustment_elo(a_age), b_r + age_adjustment_elo(b_age))
        actual_a = 0.5 if f["is_draw"] else (1.0 if f["winner_id"] == a_id else 0.0)
        if i >= WARMUP:
            residual_by_fight[f["id"]] = actual_a - p_a
        delta = 72.0 * (actual_a - win_prob(a_r, b_r))
        ratings[a_id] = a_r + delta
        ratings[b_id] = b_r - delta

    print(f"{len(residual_by_fight)} post-warmup fights with a real residual\n")

    def check(name: str, extractor):
        xs, ys = [], []
        for r in rows:
            resid = residual_by_fight.get(r["fight_id"])
            if resid is None:
                continue
            v = extractor(r)
            if v is None:
                continue
            xs.append(v)
            ys.append(resid)
        if len(xs) < 100:
            print(f"{name}: too few rows ({len(xs)})")
            return
        r_ = pearson(xs, ys)
        slope = ols_slope(xs, ys)
        print(f"{name}: r={r_:.4f}, n={len(xs)}, OLS slope={slope:+.5f} win-prob per unit")

    check("Reach diff (a - b, inches)", lambda r: (r["a_reach_in"] - r["b_reach_in"]) if (r["a_reach_in"] is not None and r["b_reach_in"] is not None) else None)
    check("Height diff (a - b, inches)", lambda r: (r["a_height_in"] - r["b_height_in"]) if (r["a_height_in"] is not None and r["b_height_in"] is not None) else None)
    check("Finish-rate diff (a - b)", lambda r: (r["a_finish_rate"] - r["b_finish_rate"]) if (r["a_finish_rate"] is not None and r["b_finish_rate"] is not None) else None)
    check("Sig-str-landed diff (a - b)", lambda r: (r["a_avg_sig_str_landed"] - r["b_avg_sig_str_landed"]) if (r["a_avg_sig_str_landed"] is not None and r["b_avg_sig_str_landed"] is not None) else None)
    check("TD-landed diff (a - b)", lambda r: (r["a_avg_td_landed"] - r["b_avg_td_landed"]) if (r["a_avg_td_landed"] is not None and r["b_avg_td_landed"] is not None) else None)
    check("Experience diff (a - b)", lambda r: (r["a_experience"] - r["b_experience"]) if (r["a_experience"] is not None and r["b_experience"] is not None) else None)
    check("Win-rate diff (a - b)", lambda r: (r["a_win_rate"] - r["b_win_rate"]) if (r["a_win_rate"] is not None and r["b_win_rate"] is not None) else None)
    check("Layoff diff (a - b, days)", lambda r: (r["a_layoff_days"] - r["b_layoff_days"]) if (r["a_layoff_days"] is not None and r["b_layoff_days"] is not None) else None)

    # Stance matchup: categorical, not a diff -- check via bio lookup directly.
    print()
    fights_by_id = {f["id"]: f for f in fights}
    orthodox_vs_southpaw_resid = []
    same_stance_resid, diff_stance_resid = [], []
    for r in rows:
        resid = residual_by_fight.get(r["fight_id"])
        if resid is None:
            continue
        f = fights_by_id.get(r["fight_id"])
        if f is None:
            continue
        a_stance = (bios.get(f["fighter_a_id"], {}).get("stance") or "").strip()
        b_stance = (bios.get(f["fighter_b_id"], {}).get("stance") or "").strip()
        if not a_stance or not b_stance:
            continue
        if a_stance == b_stance:
            same_stance_resid.append(abs(resid))
        else:
            diff_stance_resid.append(abs(resid))
        if {a_stance, b_stance} == {"Orthodox", "Southpaw"}:
            # signed from the SOUTHPAW fighter's perspective (folk wisdom: southpaws overperform)
            southpaw_is_a = a_stance == "Southpaw"
            orthodox_vs_southpaw_resid.append(resid if southpaw_is_a else -resid)

    if same_stance_resid and diff_stance_resid:
        print(f"Same-stance abs(residual) mean: {sum(same_stance_resid)/len(same_stance_resid):.4f} (n={len(same_stance_resid)})")
        print(f"Diff-stance abs(residual) mean: {sum(diff_stance_resid)/len(diff_stance_resid):.4f} (n={len(diff_stance_resid)})")
    if orthodox_vs_southpaw_resid:
        print(f"Southpaw-vs-orthodox: mean residual from southpaw's perspective = {sum(orthodox_vs_southpaw_resid)/len(orthodox_vs_southpaw_resid):+.4f} (n={len(orthodox_vs_southpaw_resid)})")


if __name__ == "__main__":
    main()
