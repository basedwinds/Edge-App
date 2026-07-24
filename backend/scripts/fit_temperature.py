"""Fits and OUT-OF-SAMPLE validates a temperature-scaling calibration per sport
(single parameter T: p_cal = sigmoid(logit(p)/T); T<1 sharpens timid probs,
T>1 softens over-confident ones). One parameter is deliberately chosen over
isotonic/Platt so a small, noisy sport (WNBA) can't overfit -- it just learns
T~=1 (a no-op) when there's no consistent direction to the miscalibration.

Chronological split (train on the earlier games, test on the later) so the T we
report is what a real forward-applied calibration would have done -- never fit
and scored on the same games.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.derive_wnba_elo as W  # noqa: E402
import scripts.derive_cbb_elo as C  # noqa: E402
import scripts.derive_cfb_elo as F  # noqa: E402
from scripts.measure_calibration import reliability  # noqa: E402

EPS = 1e-6


def _logit(p):
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def calibrate(p, T):
    return 1.0 / (1.0 + math.exp(-_logit(p) / T))


def _logloss(preds, outs):
    return -sum(o * math.log(max(p, EPS)) + (1 - o) * math.log(max(1 - p, EPS)) for p, o in zip(preds, outs)) / len(preds)


def fit_T(preds, outs):
    """1-D search for the T minimizing train log-loss."""
    best_T, best_ll = 1.0, float("inf")
    T = 0.50
    while T <= 2.01:
        ll = _logloss([calibrate(p, T) for p in preds], outs)
        if ll < best_ll:
            best_ll, best_T = ll, T
        T += 0.01
    return round(best_T, 2)


def brier(preds, outs):
    return sum((p - o) ** 2 for p, o in zip(preds, outs)) / len(preds)


def evaluate(name, preds, outs):
    n = len(preds)
    split = int(n * 0.6)
    tr_p, tr_o = preds[:split], outs[:split]
    te_p, te_o = preds[split:], outs[split:]
    T = fit_T(tr_p, tr_o)
    te_cal = [calibrate(p, T) for p in te_p]
    _, ece_before = reliability(te_p, te_o)
    _, ece_after = reliability(te_cal, te_o)
    print(f"\n=== {name} ===  fitted T={T}  (test n={len(te_o)})")
    print(f"  test ECE  {ece_before:.4f} -> {ece_after:.4f}   ({ece_before-ece_after:+.4f})")
    print(f"  test Brier {brier(te_p, te_o):.5f} -> {brier(te_cal, te_o):.5f}   ({brier(te_p,te_o)-brier(te_cal,te_o):+.5f})")
    verdict = "SHIP" if (ece_after < ece_before - 0.003 and brier(te_cal, te_o) <= brier(te_p, te_o) + 1e-5) else "no-op (keep T=1.0)"
    print(f"  -> {verdict}")
    return T, verdict


def main():
    wr = W.load(); _, wadv = W.measure_home_adv(wr); wp, wo = W.run(wr, 32, wadv)
    cr = C.load(); _, cadv = C.measure_home_adv(cr); cp, co = C.run(cr, 48, cadv)
    fr = F.load(); _, fadv = F.measure_home_adv(fr); fp, fo = F.run(fr, 56, fadv)
    evaluate("WNBA", wp, wo)
    evaluate("CBB", cp, co)
    evaluate("CFB", fp, fo)


if __name__ == "__main__":
    main()
