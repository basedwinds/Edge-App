"""Do STYLE-MATCHUP features beat pure Elo at picking MMA winners?

WHY. elo_mma.py states plainly that the moneyline model carries "zero fighter-
feature data (styles, physical...)". Meanwhile mma_features.build_feature_rows()
already computes, per fighter and pre-fight, everything a style matchup needs --
striking volume, takedown volume, KO/submission win rates AND KO/submission LOSS
rates. The distance model consumes them SYMMETRICALLY (mean/max of both
fighters), because "did it go the distance" doesn't care who is who, so the
matchup itself is exactly the information being discarded.

THE REAL MATCHUP TERMS ARE CROSS PRODUCTS, NOT DIFFERENCES. "Striker vs
grappler" is folk shorthand; what actually decides a stylistic mismatch is one
fighter's weapon meeting the other's weakness:

    a_ko_win_rate  x  b_ko_loss_rate    A's power vs B's chin
    a_sub_win_rate x  b_sub_loss_rate   A's sub threat vs B's grappling defence

Each is entered antisymmetrically (A's edge minus B's) so the feature flips sign
when the corner assignment flips, which a winner model requires and the distance
model never did.

ORIENTATION BIAS IS NEUTRALISED by training on both corner assignments: every
fight appears once as (a,b) and once as (b,a) with the label flipped. Without
this a model can learn "fighter_a tends to win" -- an artifact of how the source
lists bouts, not a real signal, and one that would evaporate against a market.

HONEST PRIOR: unfavourable. This app has already tested and rejected a number of
MMA features, and went-the-distance remains the one confirmed edge across nine
markets. Elo is hard to beat here. The point of this script is to get a real
answer cheaply, and reject if the answer is no.

Scored on a held-out LATER slice, never in-sample: the baseline is Elo alone, the
challenger is Elo plus the style block, and the comparison that matters is
log-loss (a probability model, judged on probabilities), with accuracy reported
alongside only because it is easy to read.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.ingestion import ufc_data
from app.models import mma_features

BASE = 1500.0
K = 24.0
TRAIN_FRAC = 0.70


def _f(v, d=0.0):
    return d if v is None else float(v)


def main() -> None:
    fights = ufc_data.load_fights()
    raw = ufc_data.load_raw_fight_rows()
    bios = ufc_data.load_fighter_bios()
    rows = mma_features.build_feature_rows(fights, raw, bios)
    by_id = {f["id"]: f for f in fights}
    print(f"fights={len(fights)} feature_rows={len(rows)}")

    elo: dict[str, float] = {}
    samples = []   # (elo_diff, style_vector, label)
    used = 0

    for r in rows:
        f = by_id.get(r["fight_id"])
        if f is None or f.get("is_draw") or f.get("is_no_contest"):
            continue
        a, b, w = f["fighter_a_id"], f["fighter_b_id"], f.get("winner_id")
        if w not in (a, b):
            continue
        ra, rb = elo.get(a, BASE), elo.get(b, BASE)

        # Style block, antisymmetric in (a,b) -- see module docstring.
        ko = _f(r["a_ko_win_rate"]) * _f(r["b_ko_loss_rate"]) - _f(r["b_ko_win_rate"]) * _f(r["a_ko_loss_rate"])
        sub = _f(r["a_sub_win_rate"]) * _f(r["b_sub_loss_rate"]) - _f(r["b_sub_win_rate"]) * _f(r["a_sub_loss_rate"])
        style = [
            ko, sub,
            _f(r["a_avg_td_landed"]) - _f(r["b_avg_td_landed"]),
            _f(r["a_avg_sig_str_landed"]) - _f(r["b_avg_sig_str_landed"]),
            _f(r["a_finish_rate"]) - _f(r["b_finish_rate"]),
            _f(r["a_reach_in"], 71.0) - _f(r["b_reach_in"], 71.0),
            _f(r["a_age"], 30.0) - _f(r["b_age"], 30.0),
            _f(r["a_experience"]) - _f(r["b_experience"]),
            _f(r["a_win_rate"]) - _f(r["b_win_rate"]),
        ]
        samples.append((ra - rb, style, 1 if w == a else 0))
        used += 1

        # Elo update with the real result, AFTER the row is recorded.
        pa = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
        sa = 1.0 if w == a else 0.0
        elo[a] = ra + K * (sa - pa)
        elo[b] = rb + K * ((1.0 - sa) - (1.0 - pa))

    split = int(used * TRAIN_FRAC)
    print(f"usable={used}  train={split}  holdout={used - split}")

    def build(sl):
        X_elo, X_full, y = [], [], []
        for ed, st, lab in sl:
            # Both corner assignments, label flipped -- kills orientation bias.
            X_elo += [[ed], [-ed]]
            X_full += [[ed] + st, [-ed] + [-v for v in st]]
            y += [lab, 1 - lab]
        return np.array(X_elo), np.array(X_full), np.array(y)

    Xe_tr, Xf_tr, y_tr = build(samples[:split])
    Xe_te, Xf_te, y_te = build(samples[split:])

    print(f"\n{'model':22s} {'log loss':>10s} {'accuracy':>10s}")
    out = {}
    for name, Xtr, Xte in (("Elo only", Xe_tr, Xe_te), ("Elo + style", Xf_tr, Xf_te)):
        m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
        m.fit(Xtr, y_tr)
        p = m.predict_proba(Xte)[:, 1]
        ll, ac = log_loss(y_te, p), accuracy_score(y_te, (p >= 0.5).astype(int))
        out[name] = ll
        print(f"{name:22s} {ll:10.5f} {ac:10.3f}")

    d = out["Elo + style"] - out["Elo only"]
    print(f"\nlog-loss change: {d:+.5f} ({d / out['Elo only'] * 100:+.2f}%)")
    print("VERDICT:", "style HELPS" if d < -0.002 else
          ("no meaningful gain -- REJECT" if d < 0.002 else "style HURTS -- REJECT"))


if __name__ == "__main__":
    main()
